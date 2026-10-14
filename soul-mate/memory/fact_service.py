"""记忆事实链的运行服务。

此模块是候选事实、证据验证、向量索引与发布的唯一编排边界。
SQLite 事实状态是权威；向量库只保存可重建的已索引副本。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Mapping

from api import llm_client
from core.character_context import CharacterContext, character_scope
from memory import fact_store, vector_store
from memory.fact_store import MemoryEvidence, MemoryFact

EmbeddingProvider = Callable[[str], Awaitable[list[float]]]


@dataclass(frozen=True, slots=True)
class FactIngestResult:
    created_ids: tuple[str, ...]
    published_ids: tuple[str, ...]
    rejected_ids: tuple[str, ...]
    index_failed_ids: tuple[str, ...]
    publication_pending_ids: tuple[str, ...]
    skipped: tuple[str, ...]


def _text(value: object) -> str:
    return str(value or "").strip()


def _candidate_evidence(source_message_id: int, item: Mapping[str, object]) -> MemoryEvidence:
    quote = _text(item.get("evidence_quote"))
    if not quote:
        return MemoryEvidence(
            source_message_id=source_message_id,
            source_role="unverified",
            quote="[模型未提供原文引用]",
        )
    return MemoryEvidence(
        source_message_id=source_message_id,
        source_role="user",
        quote=quote,
    )


def _candidate_deduplication_key(item: Mapping[str, object]) -> str:
    return "\u0000".join((_text(item.get("type")), _text(item.get("content"))))


async def ingest_conversation_facts(
    context: CharacterContext,
    candidates: Iterable[Mapping[str, object]],
    *,
    source_message_id: int,
    embedder: EmbeddingProvider | None = None,
) -> FactIngestResult:
    """接收本轮对话的候选事实，并仅发布证据与索引均成功的记录。"""
    if isinstance(source_message_id, bool) or not isinstance(source_message_id, int):
        raise ValueError("对话来源消息 ID 无效")

    created: list[str] = []
    published: list[str] = []
    rejected: list[str] = []
    index_failed: list[str] = []
    publication_pending: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()

    with character_scope(context):
        for raw_item in candidates:
            if not isinstance(raw_item, Mapping):
                skipped.append("候选格式无效")
                continue
            duplicate_key = _candidate_deduplication_key(raw_item)
            if not duplicate_key.strip("\u0000") or duplicate_key in seen:
                skipped.append(_text(raw_item.get("content")) or "候选为空或重复")
                continue
            seen.add(duplicate_key)

            try:
                candidate = fact_store.create_memory_candidate(
                    context,
                    content=raw_item.get("content"),
                    memory_type=raw_item.get("type", "event"),
                    importance=raw_item.get("importance", 3),
                    valence=raw_item.get("valence", 0.0),
                    arousal=raw_item.get("arousal", 0.5),
                    source_kind="conversation",
                    created_by="conversation_memory_extractor",
                    evidence=(_candidate_evidence(source_message_id, raw_item),),
                )
            except (TypeError, ValueError) as exc:
                skipped.append(f"{_text(raw_item.get('content'))[:80]}: {exc}")
                continue

            created.append(candidate.id)
            verified = fact_store.verify_conversation_evidence(context, candidate.id)
            if verified.status != "verified":
                rejected.append(verified.id)
                continue

            indexed = await index_and_publish_fact(context, verified.id, embedder=embedder)
            if indexed.status == "published":
                published.append(indexed.id)
            elif indexed.status == "rejected":
                rejected.append(indexed.id)
            elif indexed.status == "index_failed":
                index_failed.append(indexed.id)
            elif indexed.status == "indexing" and (indexed.index_token or "").startswith("written:"):
                publication_pending.append(indexed.id)
            else:
                skipped.append(f"{indexed.id}: 索引状态未完成")

    return FactIngestResult(
        created_ids=tuple(created),
        published_ids=tuple(published),
        rejected_ids=tuple(rejected),
        index_failed_ids=tuple(index_failed),
        publication_pending_ids=tuple(publication_pending),
        skipped=tuple(skipped),
    )


async def index_and_publish_fact(
    context: CharacterContext,
    fact_id: str,
    *,
    embedder: EmbeddingProvider | None = None,
    retry_actor: str = "memory_indexer",
    retry_reason: str = "自动重试索引",
) -> MemoryFact:
    """为已验证事实建立索引并发布，任何失败都不会产生可检索记录。"""
    with character_scope(context):
        current = fact_store.get_memory_fact(context, fact_id)
        if current is None:
            raise KeyError("记忆事实不存在")
        if current.status == "verified":
            indexing = fact_store.begin_memory_index(context, fact_id)
        elif current.status == "index_failed":
            indexing = fact_store.retry_memory_index(
                context,
                fact_id,
                actor=retry_actor,
                reason=retry_reason,
            )
        elif current.status == "indexing" and (current.index_token or "").startswith("written:"):
            return fact_store.publish_memory_fact(context, fact_id)
        elif current.status == "indexing":
            indexing = current
        elif current.status == "published":
            return current
        else:
            raise ValueError("当前记忆事实不能建立索引")

        try:
            memory = fact_store.to_memory(context, indexing)
            active_embedder = embedder or llm_client.embed
            embedding = await active_embedder(indexing.content)
            await vector_store.add_memory(memory, embedding)
        except Exception as exc:
            return _mark_index_failed_or_reload(context, fact_id, exc)

        try:
            return fact_store.publish_memory_fact(context, fact_id)
        except Exception as exc:
            # 向量已落盘但 SQLite 未发布时，检索仍会被事实状态过滤；
            # 标记失败后，后续重试会使用同一事实 ID 覆盖这份派生副本。
            return _mark_index_failed_or_reload(context, fact_id, exc)


def _mark_index_failed_or_reload(
    context: CharacterContext,
    fact_id: str,
    error: Exception,
) -> MemoryFact:
    latest = fact_store.get_memory_fact(context, fact_id)
    if latest is None:
        raise KeyError("记忆事实不存在")
    if latest.status == "published":
        return latest
    if latest.status != "indexing":
        return latest
    if (latest.index_token or "").startswith("written:"):
        fact_store.append_memory_fact_audit(
            context,
            fact_id,
            action="memory_publish_deferred",
            actor="memory_indexer",
            payload={"error": str(error)[:300]},
        )
        return latest
    try:
        return fact_store.mark_memory_index_failed(context, fact_id, error)
    except (fact_store.MemoryFactStateConflict, ValueError):
        current = fact_store.get_memory_fact(context, fact_id)
        if current is None:
            raise KeyError("记忆事实不存在")
        return current


async def create_manual_fact(
    context: CharacterContext,
    *,
    content: object,
    memory_type: object,
    importance: object,
    valence: object,
    arousal: object,
    actor: str,
) -> MemoryFact:
    """管理员录入先形成候选，必须经显式验证和发布才可检索。"""
    with character_scope(context):
        return fact_store.create_memory_candidate(
            context,
            content=content,
            memory_type=memory_type,
            importance=importance,
            valence=valence,
            arousal=arousal,
            source_kind="manual",
            created_by=actor,
            evidence=(MemoryEvidence(None, "admin", _text(content) or "[空人工录入]"),),
        )


async def verify_and_publish_manual_fact(
    context: CharacterContext,
    fact_id: str,
    *,
    actor: str,
    embedder: EmbeddingProvider | None = None,
) -> MemoryFact:
    with character_scope(context):
        verified = fact_store.verify_manual_evidence(context, fact_id, actor=actor)
        return await index_and_publish_fact(context, verified.id, embedder=embedder)


async def reject_fact(
    context: CharacterContext,
    fact_id: str,
    *,
    actor: str,
    reason: str,
) -> MemoryFact:
    with character_scope(context):
        return fact_store.reject_memory_fact(
            context,
            fact_id,
            actor=actor,
            reason=reason,
        )


async def retry_failed_fact(
    context: CharacterContext,
    fact_id: str,
    *,
    actor: str,
    reason: str,
    embedder: EmbeddingProvider | None = None,
) -> MemoryFact:
    with character_scope(context):
        current = fact_store.get_memory_fact(context, fact_id)
        if current is None:
            raise KeyError("记忆事实不存在")
        if current.status != "index_failed":
            raise ValueError("只有索引失败的事实可以重试")
        return await index_and_publish_fact(
            context,
            fact_id,
            embedder=embedder,
            retry_actor=actor,
            retry_reason=reason,
        )


async def archive_fact(
    context: CharacterContext,
    fact_id: str,
    *,
    actor: str,
    reason: str,
) -> MemoryFact:
    """撤销事实的检索资格，并尽力删除其派生向量。"""
    with character_scope(context):
        archived = fact_store.archive_memory_fact(
            context,
            fact_id,
            actor=actor,
            reason=reason,
        )
        try:
            await vector_store.delete_fact_index(fact_id)
        except Exception as exc:
            fact_store.append_memory_fact_audit(
                context,
                fact_id,
                action="vector_cleanup_failed",
                actor=actor,
                payload={"error": str(exc)[:300]},
            )
        else:
            fact_store.append_memory_fact_audit(
                context,
                fact_id,
                action="vector_cleanup_succeeded",
                actor=actor,
                payload={},
            )
        return archived
