"""ChromaDB 封装 — 动态强度计算，不持久化 strength 字段"""
# CentOS 系系统自带 sqlite3 <= 3.26，chromadb 要求 >= 3.35
# pysqlite3-binary 携带预编译的新版 sqlite3，在 import chromadb 之前 patch 进来
try:
    import pysqlite3 as _sq3
    import sys as _sys
    _sys.modules["sqlite3"] = _sq3
except ImportError:
    pass  # 非 CentOS 环境（本地 macOS/Windows）无需 patch

import os
import chromadb
from chromadb.config import Settings
from datetime import datetime
from pathlib import Path
from memory import fact_store
from memory.models import Memory, MemoryType
from core.character_context import require_character_context

DECAY_RATES = {
    MemoryType.IDENTITY:   1.0,    # 不衰减
    MemoryType.PREFERENCE: 0.995,
    MemoryType.RELATION:   0.990,
    MemoryType.EVENT:      0.970,
    MemoryType.EMOTION:    0.930,
}

_clients: dict[tuple[str, str], object] = {}
_collections: dict[tuple[tuple[str, str], str], object] = {}


def _workspace_key() -> tuple[str, str]:
    context = require_character_context()
    # 缓存同时绑定角色身份和规范化工作空间，避免路径复用时拿到旧角色对象。
    return context.character_id, str(context.workspace_dir.resolve())


def get_client():
    scope = _workspace_key()
    if scope not in _clients:
        _clients[scope] = chromadb.PersistentClient(
            path=str(Path(scope[1]) / "chroma"),
            settings=Settings(anonymized_telemetry=False),
        )
    return _clients[scope]


def get_collection(name: str):
    scope = _workspace_key()
    key = (scope, name)
    if key not in _collections:
        _collections[key] = get_client().get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )
    return _collections[key]


def compute_strength(
    memory_type: str,
    created_at: datetime,
    importance: int,
    recall_count: int,
    initial_strength: float = 1.0,
    arousal: float = 0.5,
    pinned: bool = False,
    resolved: bool = False,
) -> float:
    """动态计算记忆强度（改进版艾宾浩斯 + Russell 情感坐标加权）"""
    if pinned:
        return 10.0  # 永不衰减

    mem_type = MemoryType(memory_type)
    days = max(0, (datetime.now() - created_at).days)
    base_rate = DECAY_RATES[mem_type]

    # 重要性修正：越重要衰减越慢
    rate = base_rate ** (1.0 / (1 + importance))
    # 情绪强度修正：arousal 越高 → 越难忘
    emotion_weight = 1.0 + arousal * 0.8
    # 被回忆次数加固
    recall_bonus = 1 + recall_count * 0.02

    strength = initial_strength * (rate ** days) * recall_bonus * emotion_weight

    # resolved 记忆降权至 5%，沉底等关键词唤醒
    if resolved:
        strength *= 0.05

    return max(0.01, min(10.0, strength))


async def add_memory(memory: Memory, embedding: list[float]):
    """写入当前索引事务的派生向量，不接受旧记忆直写。"""
    context = require_character_context()
    if memory.status != "indexed" or memory.fact_id != memory.id:
        raise RuntimeError("向量写入只能接收事实链索引中的派生记录")
    if memory.character_id != context.character_id:
        raise RuntimeError("向量记录角色与当前 CharacterContext 不一致")
    if memory.source_trace_id != memory.fact_id:
        raise RuntimeError("向量记录缺少可追溯事实标识")
    if not memory.index_token or not embedding:
        raise ValueError("向量写入缺少索引凭据或向量")

    fact = fact_store.get_memory_fact(context, memory.fact_id)
    if (
        fact is None
        or fact.status != "indexing"
        or fact.vector_status != "indexing"
        or fact.index_token != memory.index_token
    ):
        raise RuntimeError("向量写入不匹配当前事实索引事务")

    col = get_collection("memories")
    col.upsert(
        ids=[memory.id],
        embeddings=[embedding],
        documents=[memory.content],
        metadatas=[{
            "fact_id":          memory.fact_id,
            "character_id":     memory.character_id,
            "status":           "indexed",
            "type":             memory.type.value,
            "importance":       memory.importance,
            "initial_strength": memory.initial_strength,
            "recall_count":     memory.recall_count,
            "created_at":       memory.created_at.isoformat(),
            "source":           memory.source,
            "source_trace_id":  memory.source_trace_id,
            "valence":          memory.valence,
            "arousal":          memory.arousal,
            "pinned":           int(memory.pinned),
            "resolved":         int(memory.resolved),
        }],
    )
    try:
        fact_store.confirm_memory_vector_index(
            context,
            memory.fact_id,
            index_token=memory.index_token,
        )
    except Exception:
        # SQLite 与 Chroma 无法共享事务；归档一旦胜出，补偿删除晚到的向量副本。
        try:
            latest = fact_store.get_memory_fact(context, memory.fact_id)
            if latest is not None and latest.status == "archived":
                try:
                    col.delete(ids=[memory.id])
                except Exception as cleanup_error:
                    fact_store.append_memory_fact_audit(
                        context,
                        memory.fact_id,
                        action="vector_orphan_cleanup_failed",
                        actor="memory_indexer",
                        payload={"error": str(cleanup_error)[:300]},
                    )
                else:
                    fact_store.append_memory_fact_audit(
                        context,
                        memory.fact_id,
                        action="vector_orphan_cleanup_succeeded",
                        actor="memory_indexer",
                        payload={},
                    )
        except Exception:
            pass
        raise


async def search_memories(
    query_embedding: list[float],
    top_k: int = 5,
    min_strength: float = 0.08,
    type_filter: str | None = None,
    query_text: str = "",
) -> list[dict]:
    """只返回事实仓储已发布且索引成功的记忆。"""
    context = require_character_context()
    if not query_embedding or top_k <= 0:
        return []
    parsed_type = MemoryType(type_filter).value if type_filter else None

    filters: list[dict] = [
        {"status": {"$eq": "indexed"}},
        {"character_id": {"$eq": context.character_id}},
    ]
    if parsed_type:
        filters.append({"type": {"$eq": parsed_type}})
    where = {"$and": filters}

    col = get_collection("memories")
    indexed_count = col.count()
    if indexed_count <= 0:
        return []
    results = col.query(
        query_embeddings=[query_embedding],
        n_results=min(max(top_k * 8, top_k), indexed_count),
        where=where,
        include=["metadatas", "distances"],
    )

    identifiers = (results.get("ids") or [[]])[0] or []
    metadatas = (results.get("metadatas") or [[]])[0] or []
    distances = (results.get("distances") or [[]])[0] or []
    fact_ids = [
        str(metadata.get("fact_id") or identifier)
        for identifier, metadata in zip(identifiers, metadatas)
        if isinstance(metadata, dict)
    ]
    published = fact_store.get_published_memory_facts(context, fact_ids)
    query_tokens = set(query_text.lower().split()) if query_text else set()
    tier_weights = {
        "identity": 1.0,
        "preference": 0.8,
        "relationship": 0.8,
        "event": 0.6,
        "emotion": 0.5,
    }

    memories: list[dict] = []
    for identifier, metadata, distance in zip(identifiers, metadatas, distances):
        if not isinstance(metadata, dict):
            continue
        fact_id = str(metadata.get("fact_id") or identifier)
        fact = published.get(fact_id)
        if fact is None or (parsed_type and fact.memory_type.value != parsed_type):
            continue
        strength = compute_strength(
            memory_type=fact.memory_type.value,
            created_at=fact.created_at,
            importance=fact.importance,
            recall_count=0,
            initial_strength=1.0,
            arousal=fact.arousal,
            pinned=fact.pinned,
            resolved=fact.resolved,
        )
        if strength < min_strength:
            continue
        try:
            vec_score = max(0.0, 1 - float(distance))
        except (TypeError, ValueError):
            continue
        days_old = max(0, (datetime.now() - fact.created_at).days)
        combined = (
            vec_score * 0.40
            + _keyword_score(fact.content, query_tokens) * 0.30
            + max(0.0, 1 - days_old / 90) * 0.15
            + tier_weights.get(fact.memory_type.value, 0.5) * 0.15
        )
        memories.append({
            "id": fact.id,
            "fact_id": fact.id,
            "content": fact.content,
            "type": fact.memory_type.value,
            "importance": fact.importance,
            "strength": round(strength, 3),
            "relevance": round(combined, 3),
            "valence": fact.valence,
            "arousal": fact.arousal,
            "status": fact.status,
        })

    memories.sort(key=lambda item: item["relevance"], reverse=True)
    return memories[:top_k]


def _keyword_score(text: str, tokens: set[str]) -> float:
    """简单关键词覆盖率，支持中英文。"""
    if not tokens:
        return 0.0
    text_lower = text.lower()
    hit = sum(1 for t in tokens if t in text_lower)
    return hit / len(tokens)


async def add_corpus(text: str, metadata: dict):
    raise RuntimeError("旧语料向量写入已停用，请通过角色档案素材审核链处理")


async def get_all_ids() -> list[str]:
    raise RuntimeError("向量内部条目不可作为记忆管理接口，请读取事实仓储")


async def delete_fact_index(fact_id: str) -> None:
    require_character_context()
    if not fact_id.strip():
        raise ValueError("事实 ID 不能为空")
    col = get_collection("memories")
    col.delete(ids=[fact_id])


async def delete_memory(memory_id: str):
    raise RuntimeError("旧向量删除入口已停用，请通过事实归档服务处理")
