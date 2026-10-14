"""管理台请求中的角色作用域与独立数据库选择。"""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from bridge.v1_store import BridgeStore
from core.character_context import CharacterContext


def context_from_request(request: Request) -> CharacterContext:
    context = getattr(request.state, "character_context", None)
    if not isinstance(context, CharacterContext):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="管理台角色作用域尚未建立",
        )
    return context


def store_for_context(context: CharacterContext) -> BridgeStore:
    store = (
        BridgeStore.from_environment()
        if context.character_id == "default"
        else BridgeStore(context.workspace_dir / "soul_mate.db")
    )
    store.initialize_schema()
    return store


def store_from_request(request: Request) -> BridgeStore:
    context = getattr(request.state, "character_context", None)
    if isinstance(context, CharacterContext):
        return store_for_context(context)
    # 领域路由也会被测试或维护工具单独挂载，此时没有应用级人物中间件。
    # 保留旧的 DATA_DIR 注入语义，生产应用仍始终走上面的显式人物作用域。
    store = BridgeStore.from_environment()
    store.initialize_schema()
    return store
