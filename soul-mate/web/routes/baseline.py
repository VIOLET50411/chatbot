"""Owner 审核结果的人格基线预览与显式激活 API。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from persona.review_compiler import (
    BaselineCompileError,
    BaselineResolution,
    PersonaReviewCompiler,
)
from web.character_access import store_from_request


class BaselineDraftRequest(BaseModel):
    contact_id: str = Field(min_length=1, max_length=128)
    addressing_style: str = Field(min_length=2, max_length=2_000)
    punctuation_style: str = Field(min_length=2, max_length=2_000)
    relationship_baseline: str = Field(min_length=2, max_length=2_000)
    known_partner_status: str = Field(min_length=2, max_length=2_000)
    education_goal: str = Field(min_length=2, max_length=2_000)
    education_disclosure: str = Field(min_length=2, max_length=2_000)
    moments_excluded_are_style_evidence: bool
    pet_status: str = Field(min_length=2, max_length=2_000)
    last_shared_context: str = Field(min_length=2, max_length=2_000)
    note: str = Field(default="", max_length=500)


def create_baseline_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    consume_confirmation: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/persona/baseline", tags=["owner-persona-baseline"])

    def service(request: Request) -> PersonaReviewCompiler:
        return PersonaReviewCompiler(store_from_request(request))

    @router.get("")
    def state(
        owner: str = Depends(read_auth),
        compiler: PersonaReviewCompiler = Depends(service),
    ) -> dict[str, Any]:
        del owner
        try:
            return compiler.get_state()
        except BaselineCompileError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/drafts", status_code=status.HTTP_201_CREATED)
    def create_draft(
        body: BaselineDraftRequest,
        request: Request,
        owner: str = Depends(write_auth),
        compiler: PersonaReviewCompiler = Depends(service),
    ) -> dict[str, Any]:
        try:
            result = compiler.create_draft(
                BaselineResolution.from_mapping(body.model_dump(exclude={"note"})),
                actor=owner,
                note=body.note,
            )
        except BaselineCompileError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        draft = result.get("draft") or {}
        audit(
            request,
            actor=owner,
            action="persona.baseline.preview",
            scope_type="persona_baseline",
            outcome="success",
            resource_id=str(draft.get("id") or ""),
            after={
                "status": draft.get("status"),
                "profile_changes": len(draft.get("profile_diff") or []),
                "style_changes": len(draft.get("style_diff") or []),
            },
        )
        return result

    @router.post("/drafts/{draft_id}/activate")
    def activate_draft(
        draft_id: str,
        request: Request,
        owner: str = Depends(write_auth),
        compiler: PersonaReviewCompiler = Depends(service),
    ) -> dict[str, Any]:
        reason = consume_confirmation(
            request,
            owner=owner,
            action="persona.baseline.activate",
            scope="persona_baseline",
            resource_id=draft_id,
        )
        try:
            result = compiler.activate_draft(draft_id, actor=owner)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except BaselineCompileError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        active = result.get("active_snapshot") or {}
        audit(
            request,
            actor=owner,
            action="persona.baseline.activate",
            scope_type="persona_baseline",
            outcome="success",
            resource_id=draft_id,
            reason=reason,
            after={"snapshot_id": active.get("id"), "version": active.get("version")},
        )
        return result

    return router
