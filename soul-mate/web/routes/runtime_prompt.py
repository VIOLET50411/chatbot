"""Owner 全局运行提示词接口。"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from core.global_runtime_prompt import (
    get_active_character_prompt_version,
    get_active_prompt_version,
    list_character_prompt_versions,
    list_prompt_versions,
    restore_character_prompt_version,
    restore_prompt_version,
    save_active_character_prompt,
    save_active_prompt,
)


class RuntimePromptUpdate(BaseModel):
    prompt: str = Field(default="", max_length=12_000)


def create_runtime_prompt_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/runtime-prompt", tags=["runtime-prompt"])

    @router.get("")
    def current(owner: str = Depends(read_auth)) -> dict[str, Any]:
        del owner
        version = get_active_prompt_version()
        return {"prompt": version.prompt if version else "", "version": asdict(version) if version else None}

    @router.get("/history")
    def history(owner: str = Depends(read_auth)) -> dict[str, Any]:
        del owner
        return {"items": [asdict(item) for item in list_prompt_versions()]}

    @router.put("")
    def update(
        body: RuntimePromptUpdate,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        before = get_active_prompt_version()
        try:
            version = save_active_prompt(body.prompt, created_by=owner)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="runtime_prompt.update",
            scope_type="global_runtime_prompt",
            outcome="success",
            resource_id=version.id,
            before={"version_id": before.id} if before else {},
            after={"version_id": version.id, "length": len(version.prompt)},
        )
        return {"prompt": version.prompt, "version": asdict(version)}

    @router.post("/{version_id}/restore")
    def restore(
        version_id: str,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        try:
            version = restore_prompt_version(version_id, created_by=owner)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="runtime_prompt.restore",
            scope_type="global_runtime_prompt",
            outcome="success",
            resource_id=version.id,
            after={"version_id": version.id, "restored_from_id": version_id},
        )
        return {"prompt": version.prompt, "version": asdict(version)}

    @router.get("/character/current")
    def character_current(
        request: Request,
        owner: str = Depends(read_auth),
    ) -> dict[str, Any]:
        del owner
        character_id = request.state.character_context.character_id
        version = get_active_character_prompt_version(character_id)
        return {
            "character_id": character_id,
            "prompt": version.prompt if version else "",
            "version": asdict(version) if version else None,
        }

    @router.get("/character/history")
    def character_history(
        request: Request,
        owner: str = Depends(read_auth),
    ) -> dict[str, Any]:
        del owner
        character_id = request.state.character_context.character_id
        return {
            "character_id": character_id,
            "items": [asdict(item) for item in list_character_prompt_versions(character_id)],
        }

    @router.put("/character/current")
    def update_character_prompt(
        body: RuntimePromptUpdate,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        character_id = request.state.character_context.character_id
        before = get_active_character_prompt_version(character_id)
        try:
            version = save_active_character_prompt(character_id, body.prompt, created_by=owner)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="runtime_prompt.character.update",
            scope_type="character_runtime_prompt",
            outcome="success",
            resource_id=version.id,
            character_id=character_id,
            before={"version_id": before.id} if before else {},
            after={"version_id": version.id, "length": len(version.prompt)},
        )
        return {
            "character_id": character_id,
            "prompt": version.prompt,
            "version": asdict(version),
        }

    @router.post("/character/{version_id}/restore")
    def restore_character_prompt(
        version_id: str,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        character_id = request.state.character_context.character_id
        try:
            version = restore_character_prompt_version(
                character_id,
                version_id,
                created_by=owner,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="runtime_prompt.character.restore",
            scope_type="character_runtime_prompt",
            outcome="success",
            resource_id=version.id,
            character_id=character_id,
            after={"version_id": version.id, "restored_from_id": version_id},
        )
        return {
            "character_id": character_id,
            "prompt": version.prompt,
            "version": asdict(version),
        }

    return router
