"""Owner 查看、编辑、切换人格历史版本的 API。"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator

from persona.dossier import activate_snapshot, create_edited_snapshot, diff_snapshots, list_snapshots
from persona.review_cases import PersonaReviewStore
from scripts.init_db import init
from web.character_access import context_from_request, store_from_request


class SnapshotEditRequest(BaseModel):
    stable_profile: dict[str, str]
    channel_styles: dict[str, Any] | None = None
    note: str = Field(min_length=2, max_length=500)

    @field_validator("stable_profile")
    @classmethod
    def validate_profile(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned = {str(key).strip(): str(item).strip() for key, item in value.items() if str(key).strip() and str(item).strip()}
        if not cleaned:
            raise ValueError("人格事实不能为空")
        if len(cleaned) > 200 or any(len(key) > 128 or len(item) > 20_000 for key, item in cleaned.items()):
            raise ValueError("人格事实数量或单项长度超出限制")
        return cleaned

    @model_validator(mode="after")
    def validate_total_size(self) -> "SnapshotEditRequest":
        if len(json.dumps(self.model_dump(), ensure_ascii=False)) > 200_000:
            raise ValueError("本次人设编辑内容过大")
        return self


def create_snapshots_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    consume_confirmation: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/persona/snapshots", tags=["owner-persona-snapshots"])

    def prepare_workspace(request: Request) -> None:
        init(data_dir=context_from_request(request).workspace_dir)

    @router.get("")
    def snapshots(
        owner: str = Depends(read_auth),
        prepared: None = Depends(prepare_workspace),
    ) -> dict[str, Any]:
        del owner
        del prepared
        return {"items": list_snapshots()}

    @router.get("/diff")
    def snapshot_diff(
        from_id: str,
        to_id: str,
        owner: str = Depends(read_auth),
        prepared: None = Depends(prepare_workspace),
    ) -> dict[str, Any]:
        del owner
        del prepared
        try:
            return diff_snapshots(from_id, to_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/{base_snapshot_id}/edits", status_code=status.HTTP_201_CREATED)
    def edit_snapshot(
        base_snapshot_id: str,
        body: SnapshotEditRequest,
        request: Request,
        owner: str = Depends(write_auth),
        prepared: None = Depends(prepare_workspace),
    ) -> dict[str, Any]:
        del prepared
        try:
            created = create_edited_snapshot(
                base_snapshot_id,
                stable_profile=body.stable_profile,
                channel_styles=body.channel_styles,
                note=body.note,
                actor=owner,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="persona.snapshot.edit",
            scope_type="persona_snapshot",
            outcome="success",
            resource_id=str(created["id"]),
            reason=body.note,
            after={"version": created["version"], "base_snapshot_id": base_snapshot_id},
        )
        return created

    @router.post("/{snapshot_id}/activate")
    def activate(
        snapshot_id: str,
        request: Request,
        owner: str = Depends(write_auth),
        prepared: None = Depends(prepare_workspace),
    ) -> dict[str, Any]:
        del prepared
        target = next((item for item in list_snapshots() if str(item.get("id")) == snapshot_id), None)
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到当前角色的人格快照")
        review = PersonaReviewStore(store_from_request(request).database_path).list_cases(snapshot_id)
        if int(review["summary"]["total"]) > 0 and not bool(review["summary"]["passes_strict_review"]):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该人格候选仍未完成全部场景审核，或仍有标记为人机感明显的回复",
            )
        reason = consume_confirmation(
            request,
            owner=owner,
            action="persona.snapshot.activate",
            scope="persona_snapshot",
            resource_id=snapshot_id,
        )
        try:
            activated = activate_snapshot(snapshot_id, actor=owner)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="persona.snapshot.activate",
            scope_type="persona_snapshot",
            outcome="success",
            resource_id=snapshot_id,
            reason=reason,
            after={"version": activated["version"]},
        )
        return activated

    return router
