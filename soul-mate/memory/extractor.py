"""记忆提取器 — 将 LLM 候选交给可审计事实链"""
from core.character_context import require_character_context
from memory.fact_service import FactIngestResult, ingest_conversation_facts


async def extract_and_save(
    remember_list: list[dict],
    *,
    source_message_id: int,
) -> FactIngestResult:
    """将本轮候选绑定到真实用户消息，禁止绕过事实链直接写入向量库。"""
    context = require_character_context()
    return await ingest_conversation_facts(
        context,
        remember_list,
        source_message_id=source_message_id,
    )


async def extract_profile_facts(text: str) -> dict:
    raise RuntimeError("旧用户档案提取已停用，请将可验证信息作为事实候选处理")
