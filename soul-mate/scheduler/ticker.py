"""角色隔离的 15 分钟内心状态心跳。"""
from core.character_context import CharacterContext
from core.inner_world import tick as _tick


async def tick(context: CharacterContext) -> None:
    await _tick(context)
