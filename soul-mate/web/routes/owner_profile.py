"""Owner 资料、人物知情状态与重要目标管理 API。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from core.owner_profile import get_owner_profile_store


class OwnerProfileEntryUpdate(BaseModel):
    category: Literal[
        "explicit_fact",
        "conditional_rule",
        "stable_preference",
        "sensitive_self_report",
        "model_inference",
        "rejected_inference",
    ]
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=40_000)
    raw_quote: str = Field(default="", max_length=300_000)
    source: str = Field(min_length=1, max_length=500)
    confidence: float = Field(default=1.0, ge=0, le=1)
    sensitivity: Literal["normal", "private", "high"] = "normal"
    topic_tags: list[str] = Field(default_factory=list, max_length=30)
    valid_from: str | None = Field(default=None, max_length=64)
    valid_until: str | None = Field(default=None, max_length=64)


class DisclosureUpdate(BaseModel):
    state: Literal["unknown", "context_retrieved", "confirmed_known"]
    evidence: str = Field(default="", max_length=2_000)


class GoalUpdate(BaseModel):
    id: str | None = Field(default=None, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=5_000)
    status: Literal["active", "paused", "completed", "cancelled"] = "active"
    due_at: str | None = Field(default=None, max_length=64)
    next_step: str = Field(default="", max_length=1_000)
    followup_policy: str = Field(
        default="情绪强烈时先安慰；明确说只听我说时暂停当轮建议",
        min_length=1,
        max_length=2_000,
    )


def create_owner_profile_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    consume_confirmation: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/owner", tags=["owner-profile"])

    @router.get("/profile")
    def list_profile(
        include_deleted: bool = Query(default=False),
        owner: str = Depends(read_auth),
    ) -> dict[str, Any]:
        del owner
        store = get_owner_profile_store()
        return {
            "items": store.list_entries(include_deleted=include_deleted, reveal_sensitive=False),
        }

    @router.put("/profile/{profile_key}")
    def put_profile_entry(
        profile_key: str,
        body: OwnerProfileEntryUpdate,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        store = get_owner_profile_store()
        before = store.get_entry_by_key(profile_key, reveal_sensitive=False)
        try:
            after = store.upsert_entry(profile_key=profile_key, actor=owner, **body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="owner.profile.upsert",
            scope_type="owner_profile",
            outcome="success",
            resource_id=str(after.get("id") or profile_key),
            before={"version": before.get("version")} if before else None,
            after={
                "version": after.get("version"),
                "category": after.get("category"),
                "sensitivity": after.get("sensitivity"),
            },
        )
        return after

    @router.get("/profile/{entry_id}/versions")
    def versions(entry_id: str, owner: str = Depends(read_auth)) -> dict[str, Any]:
        del owner
        try:
            return {"items": get_owner_profile_store().list_versions(entry_id)}
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/profile/{entry_id}/reveal")
    def reveal(
        entry_id: str,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        consume_confirmation(
            request,
            owner=owner,
            action="owner.profile.reveal",
            scope="owner_profile",
            resource_id=entry_id,
        )
        entry = get_owner_profile_store().get_entry(entry_id, reveal_sensitive=True)
        if entry is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner 资料不存在")
        audit(
            request,
            actor=owner,
            action="owner.profile.reveal",
            scope_type="owner_profile",
            outcome="success",
            resource_id=entry_id,
            after={"sensitivity": entry["sensitivity"]},
        )
        return entry

    @router.post("/profile/{entry_id}/versions/{version}/restore")
    def restore(
        entry_id: str,
        version: int,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        reason = consume_confirmation(
            request,
            owner=owner,
            action="owner.profile.restore",
            scope="owner_profile",
            resource_id=entry_id,
        )
        try:
            restored = get_owner_profile_store().restore_version(entry_id, version, actor=owner)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="owner.profile.restore",
            scope_type="owner_profile",
            outcome="success",
            resource_id=entry_id,
            reason=reason,
            after={"version": restored["version"], "restored_from_version": version},
        )
        return restored

    @router.delete("/profile/{entry_id}")
    def delete(
        entry_id: str,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        reason = consume_confirmation(
            request,
            owner=owner,
            action="owner.profile.delete",
            scope="owner_profile",
            resource_id=entry_id,
        )
        try:
            deleted = get_owner_profile_store().set_status(entry_id, "deleted")
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="owner.profile.delete",
            scope_type="owner_profile",
            outcome="success",
            resource_id=entry_id,
            reason=reason,
            after={"status": "deleted"},
        )
        return deleted

    @router.get("/profile/disclosures/{character_id}")
    def disclosures(character_id: str, owner: str = Depends(read_auth)) -> dict[str, Any]:
        del owner
        return {"items": get_owner_profile_store().list_disclosures(character_id=character_id)}

    @router.patch("/profile/{entry_id}/disclosures/{character_id}")
    def update_disclosure(
        entry_id: str,
        character_id: str,
        body: DisclosureUpdate,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        try:
            result = get_owner_profile_store().set_disclosure(
                character_id=character_id,
                entry_id=entry_id,
                state=body.state,
                evidence=body.evidence,
                actor=owner,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="owner.profile.disclosure.update",
            scope_type="owner_profile_disclosure",
            outcome="success",
            resource_id=entry_id,
            character_id=character_id,
            after={"state": body.state},
        )
        return result

    @router.get("/goals")
    def goals(owner: str = Depends(read_auth)) -> dict[str, Any]:
        del owner
        return {"items": get_owner_profile_store().list_goals()}

    @router.put("/goals")
    def put_goal(
        body: GoalUpdate,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        result = get_owner_profile_store().upsert_goal(
            goal_id=body.id,
            title=body.title,
            detail=body.detail,
            status=body.status,
            due_at=body.due_at,
            next_step=body.next_step,
            followup_policy=body.followup_policy,
        )
        audit(
            request,
            actor=owner,
            action="owner.goal.upsert",
            scope_type="owner_goal",
            outcome="success",
            resource_id=str(result.get("id") or ""),
            after={"status": result.get("status"), "due_at": result.get("due_at")},
        )
        return result

    return router
