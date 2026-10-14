"""角色隔离的夜间记忆整合任务。"""
from core.character_context import CharacterContext
from memory.consolidator import run as _consolidate


async def sleep_consolidation(context: CharacterContext) -> None:
    await _consolidate(context)
