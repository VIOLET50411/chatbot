"""角色级记忆事实仓储。

候选、证据、状态迁移与审计都保存在角色工作空间内；向量索引只是已发布事实的派生副本。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Literal

from core.character_context import CharacterContext
from memory.models import Memory, MemoryType


MemoryFactStatus = Literal[
    "candidate", "verified", "indexing", "published", "rejected", "index_failed", "archived"
]

_MEMORY_FACT_STATUSES = frozenset({
    "candidate", "verified", "indexing", "published", "rejected", "index_failed", "archived",
})
_RETRIEVABLE_FACT_STATUS = "published"
_ALLOWED_TYPES = {item.value for item in MemoryType}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_fact (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    importance INTEGER NOT NULL,
    valence REAL NOT NULL,
    arousal REAL NOT NULL,
    source_kind TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    vector_status TEXT NOT NULL DEFAULT 'not_indexed',
    index_token TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_fact_evidence (
    id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL,
    source_message_id INTEGER,
    source_role TEXT NOT NULL,
    quote TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (fact_id) REFERENCES memory_fact(id)
);

CREATE TABLE IF NOT EXISTS memory_fact_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (fact_id) REFERENCES memory_fact(id)
);

CREATE INDEX IF NOT EXISTS idx_memory_fact_status
ON memory_fact(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_fact_evidence_fact
ON memory_fact_evidence(fact_id);
CREATE INDEX IF NOT EXISTS idx_memory_fact_audit_fact
ON memory_fact_audit(fact_id, id DESC);
"""


def _initialize_schema(connection: sqlite3.Connection) -> None:
    """创建事实链表并为已存在的角色库补齐可逆字段。"""
    connection.executescript(_SCHEMA)
    columns = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in connection.execute("PRAGMA table_info(memory_fact)").fetchall()
    }
    if "vector_status" not in columns:
        connection.execute(
            "ALTER TABLE memory_fact ADD COLUMN vector_status TEXT NOT NULL DEFAULT 'not_indexed'"
        )
        connection.execute(
            """
            UPDATE memory_fact
            SET vector_status = CASE status
                WHEN 'published' THEN 'indexed'
                WHEN 'indexing' THEN 'failed'
                WHEN 'index_failed' THEN 'failed'
                WHEN 'archived' THEN 'archived'
                ELSE 'not_indexed'
            END
            """
        )
    if "index_token" not in columns:
        connection.execute("ALTER TABLE memory_fact ADD COLUMN index_token TEXT")


class MemoryFactStateConflict(RuntimeError):
    """事实在读取后被其他任务改变，调用方必须重新读取。"""


@dataclass(frozen=True, slots=True)
class MemoryEvidence:
    source_message_id: int | None
    source_role: str
    quote: str


@dataclass(frozen=True, slots=True)
class MemoryFact:
    id: str
    content: str
    memory_type: MemoryType
    importance: int
    valence: float
    arousal: float
    source_kind: str
    created_by: str
    status: MemoryFactStatus
    pinned: bool
    resolved: bool
    vector_status: str
    index_token: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    evidence: tuple[MemoryEvidence, ...] = ()


def _database_path(context: CharacterContext) -> Path:
    return context.workspace_dir / "soul_mate.db"


def _connect(context: CharacterContext) -> sqlite3.Connection:
    context.workspace_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(_database_path(context), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    _initialize_schema(connection)
    connection.commit()
    return connection


def _now() -> datetime:
    return datetime.now()


def _encode(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _memory_type(value: object) -> MemoryType:
    try:
        parsed = MemoryType(str(value))
    except ValueError as exc:
        raise ValueError("不支持的记忆类型") from exc
    if parsed.value not in _ALLOWED_TYPES:
        raise ValueError("不支持的记忆类型")
    return parsed


def _bounded_number(value: object, lower: float, upper: float, fallback: float) -> float:
    try:
        return max(lower, min(upper, float(value)))
    except (TypeError, ValueError):
        return fallback


def _bounded_importance(value: object) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 3


def _normalized_text(value: object, *, field: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field}不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{field}不能超过 {maximum} 个字符")
    return normalized


def _normalize_evidence(items: Iterable[MemoryEvidence]) -> tuple[MemoryEvidence, ...]:
    normalized: list[MemoryEvidence] = []
    for item in items:
        if not isinstance(item, MemoryEvidence):
            raise TypeError("记忆证据必须使用 MemoryEvidence")
        message_id = item.source_message_id
        if message_id is not None and (isinstance(message_id, bool) or not isinstance(message_id, int)):
            raise ValueError("对话证据消息 ID 无效")
        normalized.append(MemoryEvidence(
            source_message_id=message_id,
            source_role=_normalized_text(item.source_role, field="证据角色", maximum=32),
            quote=_normalized_text(item.quote, field="证据引用", maximum=1000),
        ))
    return tuple(normalized)


def _expected_vector_status(status: MemoryFactStatus) -> str:
    return {
        "candidate": "not_indexed",
        "verified": "not_indexed",
        "indexing": "indexing",
        "published": "indexed",
        "rejected": "not_indexed",
        "index_failed": "failed",
        "archived": "archived",
    }[status]


def _validate_transition(
    current: MemoryFactStatus,
    next_status: MemoryFactStatus,
    next_vector_status: str,
    allowed_from: Iterable[MemoryFactStatus],
) -> None:
    allowed = frozenset(allowed_from)
    if current not in allowed:
        raise ValueError(f"记忆事实当前状态 {current} 不允许此操作")
    if next_status not in _MEMORY_FACT_STATUSES:
        raise ValueError("目标记忆事实状态无效")
    if next_vector_status != _expected_vector_status(next_status):
        raise ValueError("目标事实状态与向量状态不一致")


def _load_evidence(connection: sqlite3.Connection, fact_id: str) -> tuple[MemoryEvidence, ...]:
    rows = connection.execute(
        """
        SELECT source_message_id, source_role, quote
        FROM memory_fact_evidence WHERE fact_id=? ORDER BY id
        """,
        (fact_id,),
    ).fetchall()
    return tuple(
        MemoryEvidence(
            source_message_id=row["source_message_id"],
            source_role=row["source_role"],
            quote=row["quote"],
        )
        for row in rows
    )


def _to_fact(connection: sqlite3.Connection, row: sqlite3.Row) -> MemoryFact:
    try:
        status: MemoryFactStatus = row["status"]
        if status not in {"candidate", "verified", "indexing", "published", "rejected", "index_failed", "archived"}:
            raise ValueError
    except ValueError:
        status = "rejected"
    return MemoryFact(
        id=row["id"],
        content=row["content"],
        memory_type=_memory_type(row["memory_type"]),
        importance=_bounded_importance(row["importance"]),
        valence=_bounded_number(row["valence"], -1.0, 1.0, 0.0),
        arousal=_bounded_number(row["arousal"], 0.0, 1.0, 0.5),
        source_kind=row["source_kind"],
        created_by=row["created_by"],
        status=status,
        pinned=bool(row["pinned"]),
        resolved=bool(row["resolved"]),
        vector_status=row["vector_status"],
        index_token=row["index_token"],
        version=int(row["version"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        evidence=_load_evidence(connection, row["id"]),
    )


def _load(connection: sqlite3.Connection, fact_id: str) -> MemoryFact | None:
    row = connection.execute("SELECT * FROM memory_fact WHERE id=?", (fact_id,)).fetchone()
    return _to_fact(connection, row) if row else None


def _audit(
    connection: sqlite3.Connection,
    fact_id: str,
    action: str,
    actor: str,
    payload: dict[str, object],
    *,
    occurred_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO memory_fact_audit (fact_id, action, actor, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (fact_id, action, actor, _encode(payload), occurred_at.isoformat()),
    )


def initialize_memory_fact_schema(connection: sqlite3.Connection) -> None:
    """在调用方已打开的角色数据库连接中创建事实链所需表。"""
    _initialize_schema(connection)


def ensure_memory_fact_schema(context: CharacterContext) -> None:
    connection = _connect(context)
    try:
        connection.commit()
    finally:
        connection.close()


def create_memory_candidate(
    context: CharacterContext,
    *,
    content: object,
    memory_type: object,
    importance: object,
    valence: object,
    arousal: object,
    source_kind: str,
    created_by: str,
    evidence: Iterable[MemoryEvidence] = (),
) -> MemoryFact:
    normalized_content = _normalized_text(content, field="记忆内容", maximum=500)
    normalized_source_kind = _normalized_text(source_kind, field="事实来源", maximum=64)
    normalized_created_by = _normalized_text(created_by, field="创建主体", maximum=128)
    fact_evidence = _normalize_evidence(evidence)

    fact_id = str(uuid.uuid4())
    now = _now()
    connection = _connect(context)
    try:
        with connection:
            parsed_type = _memory_type(memory_type)
            connection.execute(
                """
                INSERT INTO memory_fact
                (id, content, memory_type, importance, valence, arousal, source_kind, created_by,
                 status, pinned, resolved, vector_status, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', 0, 0, 'not_indexed', 0, ?, ?)
                """,
                (
                    fact_id,
                    normalized_content,
                    parsed_type.value,
                    _bounded_importance(importance),
                    _bounded_number(valence, -1.0, 1.0, 0.0),
                    _bounded_number(arousal, 0.0, 1.0, 0.5),
                    normalized_source_kind,
                    normalized_created_by,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            for item in fact_evidence:
                connection.execute(
                    """
                    INSERT INTO memory_fact_evidence
                    (id, fact_id, source_message_id, source_role, quote, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()), fact_id, item.source_message_id,
                        item.source_role, item.quote, now.isoformat(),
                    ),
                )
            _audit(
                connection, fact_id, "candidate_created", normalized_created_by,
                {"source_kind": normalized_source_kind, "evidence_count": len(fact_evidence)},
                occurred_at=now,
            )
            return _load(connection, fact_id)  # type: ignore[return-value]
    finally:
        connection.close()


def get_memory_fact(context: CharacterContext, fact_id: str) -> MemoryFact | None:
    connection = _connect(context)
    try:
        with connection:
            return _load(connection, fact_id)
    finally:
        connection.close()


def _conversation_evidence_is_valid(
    connection: sqlite3.Connection,
    evidence: MemoryEvidence,
) -> bool:
    if evidence.source_role != "user" or not evidence.quote.strip() or evidence.source_message_id is None:
        return False
    row = connection.execute(
        "SELECT role, content FROM conversations WHERE id=?",
        (evidence.source_message_id,),
    ).fetchone()
    return bool(
        row
        and row["role"] == evidence.source_role
        and evidence.quote.strip() in row["content"]
    )


def _transition_memory_fact(
    context: CharacterContext,
    *,
    fact_id: str,
    expected_version: int,
    next_status: MemoryFactStatus,
    next_vector_status: str,
    action: str,
    actor: str,
    payload: dict[str, object],
    allowed_from: Iterable[MemoryFactStatus],
    next_index_token: str | None = None,
) -> MemoryFact:
    normalized_action = _normalized_text(action, field="审计动作", maximum=128)
    normalized_actor = _normalized_text(actor, field="执行主体", maximum=128)
    if next_status == "indexing" and not next_index_token:
        raise ValueError("索引中的记忆必须持有索引凭据")
    if next_status != "indexing" and next_index_token is not None:
        raise ValueError("非索引状态不能保留索引凭据")

    now = _now()
    connection = _connect(context)
    try:
        with connection:
            current = _load(connection, fact_id)
            if current is None:
                raise KeyError("记忆事实不存在")
            if current.version != expected_version:
                raise MemoryFactStateConflict("记忆事实已被其他任务更新")
            if current.vector_status != _expected_vector_status(current.status):
                raise RuntimeError("记忆事实状态与向量状态不一致，拒绝继续迁移")
            if current.status == "indexing" and not current.index_token:
                raise RuntimeError("索引中的记忆缺少索引凭据，拒绝继续迁移")
            if current.status != "indexing" and current.index_token is not None:
                raise RuntimeError("非索引状态持有索引凭据，拒绝继续迁移")
            _validate_transition(
                current.status,
                next_status,
                next_vector_status,
                allowed_from,
            )
            cursor = connection.execute(
                """
                UPDATE memory_fact
                SET status=?, vector_status=?, index_token=?, version=?, updated_at=?
                WHERE id=? AND version=?
                """,
                (
                    next_status,
                    next_vector_status,
                    next_index_token,
                    current.version + 1,
                    now.isoformat(),
                    fact_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise MemoryFactStateConflict("记忆事实状态提交冲突")
            _audit(connection, fact_id, normalized_action, normalized_actor, payload, occurred_at=now)
            return _load(connection, fact_id)  # type: ignore[return-value]
    finally:
        connection.close()


def verify_conversation_evidence(context: CharacterContext, fact_id: str) -> MemoryFact:
    connection = _connect(context)
    try:
        with connection:
            current = _load(connection, fact_id)
            if current is None:
                raise KeyError("记忆事实不存在")
            verified = bool(current.evidence) and all(
                _conversation_evidence_is_valid(connection, item)
                for item in current.evidence
            )
            version = current.version
            evidence_count = len(current.evidence)
    finally:
        connection.close()

    next_status: MemoryFactStatus = "verified" if verified else "rejected"
    action = "conversation_evidence_verified" if verified else "conversation_evidence_rejected"
    return _transition_memory_fact(
        context,
        fact_id=fact_id,
        expected_version=version,
        next_status=next_status,
        next_vector_status=_expected_vector_status(next_status),
        action=action,
        actor="memory_evidence_verifier",
        payload={"evidence_count": evidence_count, "valid": verified},
        allowed_from=("candidate",),
    )


def begin_memory_index(context: CharacterContext, fact_id: str) -> MemoryFact:
    current = get_memory_fact(context, fact_id)
    if current is None:
        raise KeyError("记忆事实不存在")
    if current.status != "verified":
        raise ValueError("只有已验证记忆可以建立索引")
    return _transition_memory_fact(
        context,
        fact_id=fact_id,
        expected_version=current.version,
        next_status="indexing",
        next_vector_status="indexing",
        action="vector_index_started",
        actor="memory_indexer",
        payload={},
        allowed_from=("verified",),
        next_index_token=uuid.uuid4().hex,
    )


def publish_memory_fact(context: CharacterContext, fact_id: str) -> MemoryFact:
    current = get_memory_fact(context, fact_id)
    if current is None:
        raise KeyError("记忆事实不存在")
    if current.status != "indexing" or not (current.index_token or "").startswith("written:"):
        raise ValueError("只有已确认向量写入的索引记忆可以发布")
    return _transition_memory_fact(
        context,
        fact_id=fact_id,
        expected_version=current.version,
        next_status="published",
        next_vector_status="indexed",
        action="memory_published",
        actor="memory_indexer",
        payload={},
        allowed_from=("indexing",),
    )


def mark_memory_index_failed(context: CharacterContext, fact_id: str, error: Exception) -> MemoryFact:
    current = get_memory_fact(context, fact_id)
    if current is None:
        raise KeyError("记忆事实不存在")
    if current.status != "indexing":
        raise ValueError("只有索引中的记忆可以标记为索引失败")
    if (current.index_token or "").startswith("written:"):
        raise ValueError("已确认向量写入的记忆不能标记为索引失败")
    return _transition_memory_fact(
        context,
        fact_id=fact_id,
        expected_version=current.version,
        next_status="index_failed",
        next_vector_status="failed",
        action="vector_index_failed",
        actor="memory_indexer",
        payload={"error": str(error)[:300]},
        allowed_from=("indexing",),
    )


def to_memory(context: CharacterContext, fact: MemoryFact) -> Memory:
    if fact.status != "indexing" or not fact.index_token or fact.index_token.startswith("written:"):
        raise ValueError("只有待写入向量的索引记忆可以转换为向量记录")
    return Memory(
        id=fact.id,
        type=fact.memory_type,
        content=fact.content,
        importance=fact.importance,
        initial_strength=1.0,
        created_at=fact.created_at,
        source=fact.source_kind,
        valence=fact.valence,
        arousal=fact.arousal,
        pinned=fact.pinned,
        resolved=fact.resolved,
        character_id=context.character_id,
        fact_id=fact.id,
        source_trace_id=fact.id,
        status="indexed",
        created_by=fact.created_by,
        index_token=fact.index_token,
    )


def confirm_memory_vector_index(
    context: CharacterContext,
    fact_id: str,
    *,
    index_token: str,
) -> MemoryFact:
    current = get_memory_fact(context, fact_id)
    if current is None:
        raise KeyError("记忆事实不存在")
    if current.status != "indexing" or not index_token or current.index_token != index_token:
        raise ValueError("向量写入确认不匹配当前索引凭据")
    return _transition_memory_fact(
        context,
        fact_id=fact_id,
        expected_version=current.version,
        next_status="indexing",
        next_vector_status="indexing",
        action="vector_index_confirmed",
        actor="memory_indexer",
        payload={},
        allowed_from=("indexing",),
        next_index_token=f"written:{index_token}",
    )


def retry_memory_index(
    context: CharacterContext,
    fact_id: str,
    *,
    actor: str = "memory_indexer",
    reason: str = "自动重试索引",
) -> MemoryFact:
    current = get_memory_fact(context, fact_id)
    if current is None:
        raise KeyError("记忆事实不存在")
    if current.status != "index_failed":
        raise ValueError("只有索引失败的记忆可以重试索引")
    normalized_reason = _normalized_text(reason, field="重试原因", maximum=500)
    return _transition_memory_fact(
        context,
        fact_id=fact_id,
        expected_version=current.version,
        next_status="indexing",
        next_vector_status="indexing",
        action="vector_index_retried",
        actor=actor,
        payload={"reason": normalized_reason},
        allowed_from=("index_failed",),
        next_index_token=uuid.uuid4().hex,
    )


def reject_memory_fact(
    context: CharacterContext,
    fact_id: str,
    *,
    actor: str,
    reason: str,
) -> MemoryFact:
    current = get_memory_fact(context, fact_id)
    if current is None:
        raise KeyError("记忆事实不存在")
    normalized_reason = _normalized_text(reason, field="拒绝原因", maximum=500)
    return _transition_memory_fact(
        context,
        fact_id=fact_id,
        expected_version=current.version,
        next_status="rejected",
        next_vector_status="not_indexed",
        action="memory_rejected",
        actor=actor,
        payload={"reason": normalized_reason},
        allowed_from=("candidate", "verified", "index_failed"),
    )


def archive_memory_fact(
    context: CharacterContext,
    fact_id: str,
    *,
    actor: str,
    reason: str,
) -> MemoryFact:
    current = get_memory_fact(context, fact_id)
    if current is None:
        raise KeyError("记忆事实不存在")
    normalized_reason = _normalized_text(reason, field="归档原因", maximum=500)
    return _transition_memory_fact(
        context,
        fact_id=fact_id,
        expected_version=current.version,
        next_status="archived",
        next_vector_status="archived",
        action="memory_archived",
        actor=actor,
        payload={"reason": normalized_reason},
        allowed_from=("candidate", "verified", "indexing", "published", "rejected", "index_failed"),
    )


def get_published_memory_facts(
    context: CharacterContext,
    fact_ids: Iterable[str],
) -> dict[str, MemoryFact]:
    unique_ids = tuple(dict.fromkeys(
        fact_id for fact_id in fact_ids
        if isinstance(fact_id, str) and fact_id.strip()
    ))
    if not unique_ids:
        return {}
    if len(unique_ids) > 200:
        raise ValueError("一次最多读取 200 条已发布记忆")

    placeholders = ", ".join("?" for _ in unique_ids)
    connection = _connect(context)
    try:
        with connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_fact
                WHERE status=? AND vector_status=? AND id IN ({placeholders})
                """,
                (_RETRIEVABLE_FACT_STATUS, "indexed", *unique_ids),
            ).fetchall()
            return {row["id"]: _to_fact(connection, row) for row in rows}
    finally:
        connection.close()


def list_memory_fact_audit(
    context: CharacterContext,
    *,
    fact_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    bounded_limit = max(1, min(200, int(limit)))
    connection = _connect(context)
    try:
        with connection:
            if fact_id:
                rows = connection.execute(
                    """
                    SELECT id, fact_id, action, actor, payload_json, created_at
                    FROM memory_fact_audit WHERE fact_id=? ORDER BY id DESC LIMIT ?
                    """,
                    (fact_id, bounded_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, fact_id, action, actor, payload_json, created_at
                    FROM memory_fact_audit ORDER BY id DESC LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
            result: list[dict[str, object]] = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    payload = {"raw": row["payload_json"]}
                result.append({
                    "id": row["id"],
                    "fact_id": row["fact_id"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "payload": payload,
                    "created_at": row["created_at"],
                })
            return result
    finally:
        connection.close()


def list_memory_facts(
    context: CharacterContext,
    *,
    limit: int = 50,
    offset: int = 0,
    memory_type: str | None = None,
    status: str | None = None,
) -> tuple[int, list[MemoryFact]]:
    bounded_limit = max(1, min(200, int(limit)))
    bounded_offset = max(0, int(offset))
    filters: list[str] = []
    values: list[object] = []
    if memory_type:
        filters.append("memory_type=?")
        values.append(_memory_type(memory_type).value)
    if status:
        if status not in _MEMORY_FACT_STATUSES:
            raise ValueError("记忆事实状态无效")
        filters.append("status=?")
        values.append(status)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""

    connection = _connect(context)
    try:
        with connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM memory_fact{where}",
                values,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT * FROM memory_fact{where}
                ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?
                """,
                (*values, bounded_limit, bounded_offset),
            ).fetchall()
            return int(total), [_to_fact(connection, row) for row in rows]
    finally:
        connection.close()


def recover_stale_indexing(
    context: CharacterContext,
    *,
    stale_after: timedelta = timedelta(minutes=30),
) -> list[str]:
    if stale_after <= timedelta(0):
        raise ValueError("索引恢复超时时间必须大于零")
    cutoff = _now() - stale_after
    recovered: list[str] = []
    connection = _connect(context)
    try:
        with connection:
            rows = connection.execute(
                """
                SELECT id, version, index_token FROM memory_fact
                WHERE status='indexing' AND vector_status='indexing' AND updated_at < ?
                ORDER BY updated_at ASC
                """,
                (cutoff.isoformat(),),
            ).fetchall()
            now = _now()
            for row in rows:
                vector_written = str(row["index_token"] or "").startswith("written:")
                next_status = "published" if vector_written else "index_failed"
                next_vector_status = "indexed" if vector_written else "failed"
                action = "memory_publication_recovered" if vector_written else "vector_index_recovery_failed"
                payload = {
                    "stale_after_seconds": int(stale_after.total_seconds()),
                    "recovered_phase": "publication" if vector_written else "vector_write",
                }
                cursor = connection.execute(
                    """
                    UPDATE memory_fact
                    SET status=?, vector_status=?, index_token=NULL, version=?, updated_at=?
                    WHERE id=? AND version=? AND status='indexing' AND vector_status='indexing'
                    """,
                    (
                        next_status,
                        next_vector_status,
                        int(row["version"]) + 1,
                        now.isoformat(),
                        row["id"],
                        row["version"],
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                _audit(
                    connection,
                    row["id"],
                    action,
                    "memory_index_recovery",
                    payload,
                    occurred_at=now,
                )
                recovered.append(row["id"])
            return recovered
    finally:
        connection.close()


def verify_manual_evidence(
    context: CharacterContext,
    fact_id: str,
    *,
    actor: str,
) -> MemoryFact:
    normalized_actor = _normalized_text(actor, field="执行主体", maximum=128)
    current = get_memory_fact(context, fact_id)
    if current is None:
        raise KeyError("记忆事实不存在")
    if current.source_kind != "manual":
        raise ValueError("只有人工录入事实可以走人工验证")
    valid = bool(current.evidence) and all(
        item.source_message_id is None and item.source_role == "admin"
        for item in current.evidence
    )
    if not valid:
        raise ValueError("人工事实缺少受信任的管理员证据")
    return _transition_memory_fact(
        context,
        fact_id=fact_id,
        expected_version=current.version,
        next_status="verified",
        next_vector_status="not_indexed",
        action="manual_evidence_verified",
        actor=normalized_actor,
        payload={"evidence_count": len(current.evidence)},
        allowed_from=("candidate",),
    )


def append_memory_fact_audit(
    context: CharacterContext,
    fact_id: str,
    *,
    action: str,
    actor: str,
    payload: dict[str, object],
) -> None:
    normalized_action = _normalized_text(action, field="审计动作", maximum=128)
    normalized_actor = _normalized_text(actor, field="执行主体", maximum=128)
    connection = _connect(context)
    try:
        with connection:
            if _load(connection, fact_id) is None:
                raise KeyError("记忆事实不存在")
            _audit(
                connection,
                fact_id,
                normalized_action,
                normalized_actor,
                payload,
                occurred_at=_now(),
            )
    finally:
        connection.close()