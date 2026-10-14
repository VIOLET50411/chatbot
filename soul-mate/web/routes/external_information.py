"""Owner 查看与手动刷新只读外界信息源。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request

from core.external_information import get_external_information_service


def create_external_information_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/external-information", tags=["owner-external-information"])

    @router.get("")
    def status(owner: str = Depends(read_auth)) -> dict[str, Any]:
        del owner
        return get_external_information_service().status()

    @router.post("/refresh")
    async def refresh(request: Request, owner: str = Depends(write_auth)) -> dict[str, Any]:
        result = await get_external_information_service().refresh(force=True)
        audit(
            request,
            actor=owner,
            action="external_information.refresh",
            scope_type="external_information",
            outcome="success" if result["failed"] == 0 else "partial",
            after=result,
        )
        return {"refresh": result, **get_external_information_service().status()}

    return router
