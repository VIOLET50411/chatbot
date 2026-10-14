"""已发布事实的角色级检索。"""
import asyncio
import re

from api import llm_client
from core.character_context import require_character_context
from memory import vector_store


async def retrieve_context(message: str) -> dict:
    """只把 SQLite 已发布且向量索引成功的事实投影给提示词。"""
    require_character_context()
    query_embedding = await llm_client.embed(message)
    semantic_task = vector_store.search_memories(
        query_embedding,
        top_k=5,
        query_text=message,
    )
    entity_task = _entity_search(message)
    semantic, entities = await asyncio.gather(semantic_task, entity_task)

    return {
        "relevant_memories": semantic,
        "mentioned_people": entities,
    }


async def _entity_search(message: str) -> list[dict]:
    """从当前消息提取展示用实体；不产生或读取任何记忆事实。"""
    patterns = [
        r'我(妈|爸|姐|哥|弟|妹|朋友|同事|老板|男友|女友|男朋友|女朋友)',
        r'([A-Z][a-z]+)',
    ]
    names_found = []
    for pattern in patterns:
        names_found.extend(re.findall(pattern, message))
    return [{"name": name} for name in names_found[:3]]
