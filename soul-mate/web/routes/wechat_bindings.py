"""Owner 管理台的人物微信账号绑定。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from scripts.character_registry import get_wechat_binding, upsert_wechat_binding


class WechatBindingUpdate(BaseModel):
    window_nickname: str = Field(min_length=1, max_length=128)
    wechat_id: str = Field(min_length=1, max_length=128)
    owner_remark: str = Field(min_length=1, max_length=128)
    enabled: bool
    identity_confirmed: bool
    expected_version: int = Field(ge=1)


def create_wechat_bindings_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    consume_confirmation: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/characters", tags=["owner-wechat-bindings"])

    @router.get("/{character_id}/wechat-binding")
    def get_binding(character_id: str, owner: str = Depends(read_auth)) -> dict[str, Any]:
        del owner
        binding = get_wechat_binding(character_id)
        if binding is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="人物不存在")
        return binding

    @router.patch("/{character_id}/wechat-binding")
    def update_binding(
        character_id: str,
        body: WechatBindingUpdate,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        before = get_wechat_binding(character_id)
        if before is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="人物不存在")
        reason = consume_confirmation(
            request,
            owner=owner,
            action="character.wechat_binding.update",
            scope="character",
            resource_id=character_id,
        )
        try:
            after = upsert_wechat_binding(
                character_id,
                body.model_dump(exclude={"expected_version"}),
                expected_version=body.expected_version,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="character.wechat_binding.update",
            scope_type="character",
            outcome="success",
            resource_id=character_id,
            reason=reason,
            before=before,
            after=after,
            character_id=character_id,
        )
        return after

    return router
