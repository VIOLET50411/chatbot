"""单人设、多联系人的会话、画像、记忆和关系仓储。"""
from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from api import llm_client
from bridge.v1_store import UnknownContact
from core.interaction_state import InteractionState
from memory.models import RelationshipStage


MEMORY_LAYERS = frozenset(
    {"core", "daily", "diary", "gallery", "songlist", "plans", "tutorials", "handoff"}
)
_LAYER_HALF_LIFE_DAYS = {
    "core": 3650.0,
    "daily": 21.0,
    "diary": 730.0,
    "gallery": 1460.0,
    "songlist": 1460.0,
    "plans": 365.0,
    "tutorials": 1460.0,
    "handoff": 45.0,
}
_LAYER_RECALL_WEIGHT = {
    "core": 1.0,
    "daily": 0.65,
    "diary": 0.9,
    "gallery": 0.75,
    "songlist": 0.7,
    "plans": 0.9,
    "tutorials": 0.8,
    "handoff": 0.95,
}

_MEMORY_EXTENSION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS memory_entities (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        canonical_name TEXT NOT NULL,
        display_name TEXT NOT NULL,
        facts_json TEXT NOT NULL DEFAULT '[]',
        current_status TEXT NOT NULL DEFAULT '',
        judgment TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0.5,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(contact_id, entity_type, canonical_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_entity_mentions (
        memory_id TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        contact_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'mention',
        confidence REAL NOT NULL DEFAULT 0.5,
        created_at TEXT NOT NULL,
        PRIMARY KEY(memory_id, entity_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_episodes (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL,
        episode_key TEXT NOT NULL,
        title TEXT NOT NULL,
        memory_ids_json TEXT NOT NULL DEFAULT '[]',
        start_at TEXT NOT NULL,
        end_at TEXT NOT NULL,
        importance INTEGER NOT NULL DEFAULT 3,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(contact_id, episode_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_recall_audit (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL,
        memory_id TEXT NOT NULL,
        source_message_id TEXT NOT NULL,
        selected INTEGER NOT NULL DEFAULT 0,
        used INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_checkpoints (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL,
        covered_until_message_id TEXT NOT NULL,
        summary_text TEXT NOT NULL,
        open_topics_json TEXT NOT NULL DEFAULT '[]',
        promises_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memory_entities_contact ON memory_entities(contact_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memory_episodes_contact ON memory_episodes(contact_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memory_recall_contact ON memory_recall_audit(contact_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS conversation_topic_state (
        contact_id TEXT PRIMARY KEY,
        source_message_id TEXT NOT NULL,
        topic_text TEXT NOT NULL,
        followup_after_at TEXT,
        eligible INTEGER NOT NULL DEFAULT 0,
        consumed_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_topic_state_due ON conversation_topic_state(eligible, followup_after_at)",
)

_ENTITY_KEYWORDS = (
    "考研", "复习", "考试", "题目", "咖啡", "城市散步", "穿搭", "旅行", "做饭",
    "舞蹈", "普拉提", "电影", "游戏", "小红书", "朋友圈", "猫", "学校", "工作",
)
_ENTITY_STOPWORDS = {
    "今天", "最近", "现在", "真的", "感觉", "有点", "然后", "因为", "所以", "喜欢",
    "可以", "还是", "这个", "那个", "一下", "事情", "东西", "自己", "对方",
}
_SERIOUS_TOPIC_MARKERS = (
    "难受", "崩了", "崩溃", "考不上", "失败", "争执", "生气", "死亡", "害怕", "不想活",
)


class ContactRuntimeConflict(RuntimeError):
    """联系人关系版本已被其他任务更新。"""


@dataclass(frozen=True, slots=True)
class ContactRelationshipSnapshot:
    contact_id: str
    stage: RelationshipStage
    trust: float
    intimacy: float
    affection: float
    comfort: float
    resentment: float
    intimacy_context: str
    last_chat_at: datetime | None
    version: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value)).astimezone(timezone.utc)


def _stage(value: object) -> RelationshipStage:
    try:
        return RelationshipStage(str(value))
    except ValueError:
        return RelationshipStage.STRANGER


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


class ContactRuntimeStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            self._ensure_memory_extensions(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _ensure_memory_extensions(connection: sqlite3.Connection) -> None:
        for statement in _MEMORY_EXTENSION_STATEMENTS:
            connection.execute(statement)

    def _ensure_contact(
        self,
        connection: sqlite3.Connection,
        contact_id: str,
        *,
        include_disabled: bool = False,
    ) -> None:
        query = "SELECT 1 FROM contacts WHERE id=?"
        if not include_disabled:
            query += " AND enabled=1"
        exists = connection.execute(query, (contact_id,)).fetchone()
        if exists is None:
            raise UnknownContact("联系人不存在或已停用")

    @staticmethod
    def _entity_candidates(content: str) -> list[tuple[str, str]]:
        text = str(content or "").strip()
        if not text:
            return []
        found: list[tuple[int, str, str]] = []
        for keyword in _ENTITY_KEYWORDS:
            position = text.find(keyword)
            if position >= 0:
                found.append((position, "topic", keyword))
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,24}", text):
            lowered = token.lower()
            if lowered not in _ENTITY_STOPWORDS:
                found.append((text.lower().find(lowered), "named", token))
        unique: dict[tuple[str, str], tuple[int, str, str]] = {}
        for item in found:
            unique[(item[1], item[2].lower())] = item
        return [
            (entity_type, display_name)
            for _, entity_type, display_name in sorted(unique.values(), key=lambda item: item[0])[:8]
        ]

    def _organize_memory(
        self,
        *,
        contact_id: str,
        memory_id: str,
        content: str,
        importance: int,
        occurred_at: datetime | None = None,
    ) -> None:
        current = occurred_at or _now()
        now = _iso(current)
        entities = self._entity_candidates(content)
        episode_key = current.astimezone(timezone.utc).date().isoformat()
        with self._connection(immediate=True) as connection:
            self._ensure_contact(connection, contact_id, include_disabled=True)
            for entity_type, display_name in entities:
                canonical = display_name.casefold()
                entity_id = str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"memory-entity|{contact_id}|{entity_type}|{canonical}",
                ))
                existing = connection.execute(
                    """
                    SELECT id, facts_json FROM memory_entities
                    WHERE contact_id=? AND entity_type=? AND canonical_name=?
                    """,
                    (contact_id, entity_type, canonical),
                ).fetchone()
                if existing is None:
                    facts = [memory_id]
                    connection.execute(
                        """
                        INSERT INTO memory_entities(
                            id, contact_id, entity_type, canonical_name, display_name,
                            facts_json, current_status, confidence, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entity_id,
                            contact_id,
                            entity_type,
                            canonical,
                            display_name,
                            json.dumps(facts, ensure_ascii=False),
                            content[:300],
                            0.7,
                            now,
                            now,
                        ),
                    )
                else:
                    entity_id = str(existing["id"])
                    try:
                        facts = list(json.loads(str(existing["facts_json"] or "[]")))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        facts = []
                    if memory_id not in facts:
                        facts.append(memory_id)
                    connection.execute(
                        """
                        UPDATE memory_entities
                        SET facts_json=?, current_status=?, updated_at=?
                        WHERE id=? AND contact_id=?
                        """,
                        (json.dumps(facts[-30:], ensure_ascii=False), content[:300], now, entity_id, contact_id),
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO memory_entity_mentions(
                        memory_id, entity_id, contact_id, role, confidence, created_at
                    ) VALUES (?, ?, ?, 'mention', ?, ?)
                    """,
                    (memory_id, entity_id, contact_id, 0.7, now),
                )

            episode = connection.execute(
                """
                SELECT id, memory_ids_json FROM memory_episodes
                WHERE contact_id=? AND episode_key=?
                """,
                (contact_id, episode_key),
            ).fetchone()
            if episode is None:
                episode_id = str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"memory-episode|{contact_id}|{episode_key}",
                ))
                connection.execute(
                    """
                    INSERT INTO memory_episodes(
                        id, contact_id, episode_key, title, memory_ids_json,
                        start_at, end_at, importance, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        episode_id,
                        contact_id,
                        episode_key,
                        content[:120],
                        json.dumps([memory_id], ensure_ascii=False),
                        now,
                        now,
                        max(1, min(int(importance), 5)),
                        now,
                        now,
                    ),
                )
            else:
                episode_id = str(episode["id"])
                try:
                    memory_ids = list(json.loads(str(episode["memory_ids_json"] or "[]")))
                except (TypeError, ValueError, json.JSONDecodeError):
                    memory_ids = []
                if memory_id not in memory_ids:
                    memory_ids.append(memory_id)
                connection.execute(
                    """
                    UPDATE memory_episodes
                    SET memory_ids_json=?, end_at=?, importance=MAX(importance, ?), updated_at=?
                    WHERE id=? AND contact_id=?
                    """,
                    (
                        json.dumps(memory_ids[-50:], ensure_ascii=False),
                        now,
                        max(1, min(int(importance), 5)),
                        now,
                        episode_id,
                        contact_id,
                    ),
                )

    def get_relationship(
        self,
        contact_id: str,
        *,
        include_disabled: bool = False,
    ) -> ContactRelationshipSnapshot:
        with self._connection() as connection:
            self._ensure_contact(connection, contact_id, include_disabled=include_disabled)
            row = connection.execute(
                """
                SELECT contact_id, stage, trust, intimacy, affection, comfort,
                       resentment, intimacy_context, last_chat_at, version
                FROM contact_relationships
                WHERE contact_id=?
                """,
                (contact_id,),
            ).fetchone()
        if row is None:
            raise UnknownContact("联系人不存在或缺少关系状态")
        return ContactRelationshipSnapshot(
            contact_id=str(row["contact_id"]),
            stage=_stage(row["stage"]),
            trust=float(row["trust"]),
            intimacy=float(row["intimacy"]),
            affection=float(row["affection"]),
            comfort=float(row["comfort"]),
            resentment=float(row["resentment"]),
            intimacy_context=str(row["intimacy_context"] or "normal"),
            last_chat_at=_parse_time(row["last_chat_at"]),
            version=int(row["version"]),
        )

    def get_profile(self, contact_id: str, *, include_disabled: bool = False) -> dict[str, str]:
        with self._connection() as connection:
            self._ensure_contact(connection, contact_id, include_disabled=include_disabled)
            rows = connection.execute(
                "SELECT key, value FROM contact_profiles WHERE contact_id=?",
                (contact_id,),
            ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def save_message(
        self,
        *,
        contact_id: str,
        role: str,
        content: str,
        source_event_id: str | None = None,
        outbound_action_id: str | None = None,
        message_id: str | None = None,
    ) -> str:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("非法会话角色")
        normalized = str(content or "").strip()
        if not normalized:
            raise ValueError("会话内容不能为空")
        record_id = message_id or str(uuid.uuid4())
        with self._connection(immediate=True) as connection:
            self._ensure_contact(connection, contact_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO contact_conversations(
                    id, contact_id, role, content, source_event_id,
                    outbound_action_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    contact_id,
                    role,
                    normalized,
                    source_event_id,
                    outbound_action_id,
                    _iso(_now()),
                ),
                )
        return record_id

    def get_recent_messages(self, contact_id: str, *, limit: int = 20) -> list[dict[str, str]]:
        with self._connection() as connection:
            self._ensure_contact(connection, contact_id)
            rows = connection.execute(
                """
                SELECT role, content FROM contact_conversations
                WHERE contact_id=?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (contact_id, max(1, min(int(limit), 100))),
            ).fetchall()
        return [
            {"role": str(row["role"]), "content": str(row["content"])}
            for row in reversed(rows)
        ]

    def save_conversation_checkpoint(
        self,
        *,
        contact_id: str,
        covered_until_message_id: str,
        summary_text: str,
        open_topics: list[str] | None = None,
        promises: list[str] | None = None,
        checkpoint_id: str | None = None,
    ) -> str:
        normalized_summary = " ".join(str(summary_text or "").split()).strip()
        covered = str(covered_until_message_id or "").strip()
        if not normalized_summary or not covered:
            raise ValueError("对话检查点必须包含摘要和覆盖到的消息")
        record_id = checkpoint_id or str(uuid.uuid4())
        now = _iso(_now())
        normalize_list = lambda values: list(dict.fromkeys(
            " ".join(str(item).split()).strip()
            for item in (values or [])
            if " ".join(str(item).split()).strip()
        ))[:20]
        with self._connection(immediate=True) as connection:
            self._ensure_contact(connection, contact_id)
            connection.execute(
                """
                INSERT INTO conversation_checkpoints(
                    id, contact_id, covered_until_message_id, summary_text,
                    open_topics_json, promises_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    contact_id,
                    covered,
                    normalized_summary[:4000],
                    json.dumps(normalize_list(open_topics), ensure_ascii=False),
                    json.dumps(normalize_list(promises), ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return record_id

    def get_conversation_checkpoint(self, contact_id: str) -> dict | None:
        with self._connection() as connection:
            self._ensure_contact(connection, contact_id)
            row = connection.execute(
                """
                SELECT id, covered_until_message_id, summary_text,
                       open_topics_json, promises_json, created_at, updated_at
                FROM conversation_checkpoints
                WHERE contact_id=?
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (contact_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            open_topics = json.loads(str(row["open_topics_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            open_topics = []
        try:
            promises = json.loads(str(row["promises_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            promises = []
        return {
            "id": str(row["id"]),
            "covered_until_message_id": str(row["covered_until_message_id"]),
            "summary_text": str(row["summary_text"]),
            "open_topics": [str(item) for item in open_topics if str(item).strip()],
            "promises": [str(item) for item in promises if str(item).strip()],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def continuity_context(self, contact_id: str, *, recent_limit: int = 20) -> dict:
        return {
            "checkpoint": self.get_conversation_checkpoint(contact_id),
            "recent_messages": self.get_recent_messages(contact_id, limit=recent_limit),
        }

    def note_topic_state(
        self,
        *,
        contact_id: str,
        message: str,
        source_message_id: str,
        relationship_stage: RelationshipStage | str,
        now: datetime | None = None,
    ) -> None:
        """记录一轮对话是否适合稍后自然延续，严重情绪只会清除待跟进状态。"""
        topic = " ".join(str(message or "").split()).strip()
        source = str(source_message_id or "").strip()
        if not topic or not source:
            return
        current = now or _now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("主题状态时间必须包含时区")
        stage = getattr(relationship_stage, "value", str(relationship_stage))
        serious = any(marker in topic for marker in _SERIOUS_TOPIC_MARKERS)
        followup_at: str | None = None
        eligible = 0
        if not serious:
            if stage in {RelationshipStage.CLOSE.value, RelationshipStage.LOVER.value}:
                lower, upper = 15, 45
            elif stage in {RelationshipStage.FAMILIAR.value, RelationshipStage.AMBIGUOUS.value}:
                lower, upper = 30, 60
            else:
                lower, upper = 45, 75
            digest = hashlib.sha256(f"{contact_id}|{source}|{topic}".encode("utf-8")).digest()[0]
            followup_at = _iso(current + timedelta(minutes=lower + digest % (upper - lower + 1)))
            eligible = 1
        with self._connection(immediate=True) as connection:
            self._ensure_contact(connection, contact_id)
            connection.execute(
                """
                INSERT INTO conversation_topic_state(
                    contact_id, source_message_id, topic_text, followup_after_at,
                    eligible, consumed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(contact_id) DO UPDATE SET
                    source_message_id=excluded.source_message_id,
                    topic_text=excluded.topic_text,
                    followup_after_at=excluded.followup_after_at,
                    eligible=excluded.eligible,
                    consumed_at=NULL,
                    updated_at=excluded.updated_at
                """,
                (contact_id, source, topic[:800], followup_at, eligible, _iso(current)),
            )

    def get_pending_topic(self, contact_id: str, *, now: datetime | None = None) -> dict | None:
        current = now or _now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("主题状态时间必须包含时区")
        with self._connection() as connection:
            self._ensure_contact(connection, contact_id)
            row = connection.execute(
                """
                SELECT source_message_id, topic_text, followup_after_at
                FROM conversation_topic_state
                WHERE contact_id=? AND eligible=1 AND consumed_at IS NULL
                  AND followup_after_at IS NOT NULL AND followup_after_at <= ?
                """,
                (contact_id, _iso(current)),
            ).fetchone()
        if row is None:
            return None
        return {
            "source_message_id": str(row["source_message_id"]),
            "topic_text": str(row["topic_text"]),
            "followup_after_at": str(row["followup_after_at"]),
        }

    def consume_topic_state(self, contact_id: str, source_message_id: str) -> bool:
        with self._connection(immediate=True) as connection:
            self._ensure_contact(connection, contact_id)
            cursor = connection.execute(
                """
                UPDATE conversation_topic_state
                SET eligible=0, consumed_at=?, updated_at=?
                WHERE contact_id=? AND source_message_id=? AND eligible=1
                """,
                (_iso(_now()), _iso(_now()), contact_id, str(source_message_id or "").strip()),
            )
        return cursor.rowcount > 0

    async def create_conversation_checkpoint(
        self,
        contact_id: str,
        *,
        min_messages: int = 40,
        keep_recent: int = 20,
    ) -> str | None:
        with self._connection() as connection:
            self._ensure_contact(connection, contact_id, include_disabled=True)
            rows = connection.execute(
                """
                SELECT id, role, content, created_at
                FROM contact_conversations WHERE contact_id=?
                ORDER BY created_at, id
                """,
                (contact_id,),
            ).fetchall()
        threshold = max(4, int(min_messages))
        recent_count = max(1, int(keep_recent))
        if len(rows) <= threshold or len(rows) <= recent_count:
            return None
        covered_rows = rows[:-recent_count]
        covered_until = str(covered_rows[-1]["id"])
        existing = self.get_conversation_checkpoint(contact_id)
        if existing and existing.get("covered_until_message_id") == covered_until:
            return None
        transcript = "\n".join(
            f"{str(row['role'])}: {str(row['content'])[:600]}"
            for row in covered_rows[-80:]
        )
        prompt = (
            "把下面已经结束的对话整理成一个很短的事实账本。只输出 JSON："
            '{"summary":"...","open_topics":["..."],"promises":["..."]}。'
            "不要写情绪分析、推理过程、系统规则或没有证据的结论；最近仍在继续的两条消息不在这份账本里。\n"
            + transcript
        )
        try:
            raw = await llm_client.lightweight_chat(
                prompt,
                max_tokens=500,
                require_complete=True,
            )
            parsed = json.loads(str(raw or ""))
        except (Exception, json.JSONDecodeError):
            return None
        summary = " ".join(str(parsed.get("summary") or "").split()).strip()
        if not summary:
            return None
        open_topics = parsed.get("open_topics") if isinstance(parsed, dict) else []
        promises = parsed.get("promises") if isinstance(parsed, dict) else []
        if not isinstance(parsed, dict):
            return None
        if not isinstance(open_topics, list) or not isinstance(promises, list):
            return None
        open_topics = [str(item).strip() for item in open_topics if str(item).strip()][:12]
        promises = [str(item).strip() for item in promises if str(item).strip()][:12]
        return self.save_conversation_checkpoint(
            contact_id=contact_id,
            covered_until_message_id=covered_until,
            summary_text=summary,
            open_topics=open_topics,
            promises=promises,
        )

    def get_message(self, contact_id: str, message_id: str) -> dict[str, str] | None:
        """按联系人和稳定消息 ID 读取一条会话，用于直接渠道幂等恢复。"""
        with self._connection() as connection:
            self._ensure_contact(connection, contact_id)
            row = connection.execute(
                """
                SELECT id, role, content, created_at
                FROM contact_conversations WHERE id=? AND contact_id=?
                """,
                (str(message_id or "").strip(), contact_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "role": str(row["role"]),
            "content": str(row["content"]),
            "created_at": str(row["created_at"]),
        }

    def get_timeline(
        self,
        contact_id: str,
        *,
        limit: int = 50,
        include_disabled: bool = False,
    ) -> list[dict[str, str | None]]:
        with self._connection() as connection:
            self._ensure_contact(connection, contact_id, include_disabled=include_disabled)
            rows = connection.execute(
                """
                SELECT id, role, content, source_event_id, outbound_action_id, created_at
                FROM contact_conversations WHERE contact_id=?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (contact_id, max(1, min(int(limit), 100))),
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "role": str(row["role"]),
                "content": str(row["content"]),
                "source_event_id": str(row["source_event_id"]) if row["source_event_id"] else None,
                "outbound_action_id": str(row["outbound_action_id"]) if row["outbound_action_id"] else None,
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def get_timeline_page(
        self,
        contact_id: str,
        *,
        limit: int = 40,
        before_created_at: str | None = None,
        before_id: str | None = None,
        include_disabled: bool = False,
    ) -> tuple[list[dict[str, str | None]], bool]:
        """Return one chronological page and whether an older page remains.

        The cursor is the oldest row's ``created_at`` plus ``id``.  Keeping the
        tie-breaker makes history paging deterministic when several messages
        share the same timestamp, which is common for a short burst of WeChat
        messages.
        """
        page_size = max(1, min(int(limit), 100))
        with self._connection() as connection:
            self._ensure_contact(connection, contact_id, include_disabled=include_disabled)
            where = "contact_id=?"
            params: list[object] = [contact_id]
            if before_created_at:
                where += " AND (created_at < ? OR (created_at = ? AND id < ?))"
                params.extend([before_created_at, before_created_at, before_id or "\uffff"])
            rows = connection.execute(
                f"""
                SELECT id, role, content, source_event_id, outbound_action_id, created_at
                FROM contact_conversations
                WHERE {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (*params, page_size + 1),
            ).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        return [
            {
                "id": str(row["id"]),
                "role": str(row["role"]),
                "content": str(row["content"]),
                "source_event_id": str(row["source_event_id"]) if row["source_event_id"] else None,
                "outbound_action_id": str(row["outbound_action_id"]) if row["outbound_action_id"] else None,
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ], has_more

    def list_memories(
        self,
        contact_id: str,
        *,
        published_only: bool = False,
        include_disabled: bool = False,
    ) -> list[dict]:
        query = """
            SELECT id, memory_type, memory_layer, content, importance, status,
                   valence, arousal, initial_strength, recall_count, last_recalled_at,
                   expires_at, source_trace_id, consolidated_from_json,
                   source_event_ids_json, created_at, updated_at
            FROM contact_memories WHERE contact_id=?
        """
        params: list[object] = [contact_id]
        if published_only:
            query += " AND status='published'"
        query += " ORDER BY updated_at DESC, id DESC"
        with self._connection() as connection:
            self._ensure_contact(connection, contact_id, include_disabled=include_disabled)
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "id": str(row["id"]),
                "type": str(row["memory_type"]),
                "layer": str(row["memory_layer"]),
                "content": str(row["content"]),
                "importance": int(row["importance"]),
                "status": str(row["status"]),
                "valence": float(row["valence"]),
                "arousal": float(row["arousal"]),
                "initial_strength": float(row["initial_strength"]),
                "recall_count": int(row["recall_count"]),
                "last_recalled_at": str(row["last_recalled_at"] or ""),
                "expires_at": str(row["expires_at"] or ""),
                "source_trace_id": str(row["source_trace_id"] or row["id"]),
                "consolidated_from": json.loads(str(row["consolidated_from_json"] or "[]")),
                "source_event_ids": json.loads(str(row["source_event_ids_json"] or "[]")),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def review_memory(
        self,
        *,
        contact_id: str,
        memory_id: str,
        content: str | None = None,
        status: str | None = None,
    ) -> dict:
        if status is not None and status not in {"candidate", "published", "rejected", "archived"}:
            raise ValueError("记忆状态无效")
        normalized_content = str(content or "").strip() if content is not None else None
        if content is not None and not normalized_content:
            raise ValueError("记忆内容不能为空")
        if normalized_content is not None and len(normalized_content) > 1000:
            raise ValueError("记忆内容不能超过 1000 个字符")
        if normalized_content is None and status is None:
            raise ValueError("至少需要修改记忆内容或状态")
        with self._connection(immediate=True) as connection:
            self._ensure_contact(connection, contact_id, include_disabled=True)
            current = connection.execute(
                "SELECT 1 FROM contact_memories WHERE id=? AND contact_id=?",
                (memory_id, contact_id),
            ).fetchone()
            if current is None:
                raise KeyError("联系人记忆不存在")
            connection.execute(
                """
                UPDATE contact_memories
                SET content=COALESCE(?, content), status=COALESCE(?, status), updated_at=?
                WHERE id=? AND contact_id=?
                """,
                (normalized_content, status, _iso(_now()), memory_id, contact_id),
            )
        return next(
            item
            for item in self.list_memories(contact_id, include_disabled=True)
            if item["id"] == memory_id
        )

    def save_layer_memory(
        self,
        *,
        contact_id: str,
        layer: str,
        content: str,
        memory_type: str = "event",
        importance: int = 3,
        source_event_ids: list[str] | None = None,
        valence: float = 0.0,
        arousal: float = 0.5,
        expires_at: datetime | None = None,
        memory_id: str | None = None,
    ) -> str:
        """写入明确层级的记忆；所有八层共用同一套联系人隔离、来源和审核字段。"""
        normalized_layer = str(layer or "").strip().lower()
        if normalized_layer not in MEMORY_LAYERS:
            raise ValueError("记忆层级无效")
        normalized_content = str(content or "").strip()
        if not normalized_content or len(normalized_content) > 2_000:
            raise ValueError("记忆内容不能为空且不能超过 2000 个字符")
        normalized_type = str(memory_type or "event").strip().lower()[:64] or "event"
        normalized_sources = list(
            dict.fromkeys(str(item).strip() for item in (source_event_ids or []) if str(item).strip())
        )[:50]
        bounded_importance = max(1, min(int(importance), 5))
        bounded_valence = max(-1.0, min(float(valence), 1.0))
        bounded_arousal = max(0.0, min(float(arousal), 1.0))
        if expires_at is not None and normalized_layer != "daily":
            raise ValueError("只有 daily 层可以设置到期时间")
        current = _now()
        effective_expiry = expires_at
        if normalized_layer == "daily" and effective_expiry is None:
            effective_expiry = current + timedelta(days=21)
        stable_key = (
            f"{contact_id}|{normalized_layer}|{normalized_type}|"
            f"{'|'.join(normalized_sources)}|{normalized_content}"
        )
        record_id = str(memory_id or uuid.uuid5(uuid.NAMESPACE_URL, stable_key))
        now = _iso(current)
        with self._connection(immediate=True) as connection:
            self._ensure_contact(connection, contact_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO contact_memories(
                    id, contact_id, memory_type, content, importance, status,
                    source_event_ids_json, created_at, updated_at, memory_layer,
                    valence, arousal, initial_strength, expires_at, source_trace_id
                ) VALUES (?, ?, ?, ?, ?, 'published', ?, ?, ?, ?, ?, ?, 1.0, ?, ?)
                """,
                (
                    record_id,
                    contact_id,
                    normalized_type,
                    normalized_content,
                    bounded_importance,
                    json.dumps(normalized_sources, ensure_ascii=False),
                    now,
                    now,
                    normalized_layer,
                    bounded_valence,
                    bounded_arousal,
                    _iso(effective_expiry) if effective_expiry is not None else None,
                    record_id,
                ),
            )
        self._organize_memory(
            contact_id=contact_id,
            memory_id=record_id,
            content=normalized_content,
            importance=bounded_importance,
            occurred_at=current,
        )
        return record_id

    def save_memory_candidates(
        self,
        *,
        contact_id: str,
        user_message: str,
        candidates: list,
        source_event_ids: list[str],
    ) -> int:
        normalized_message = str(user_message or "")
        normalized_sources = list(dict.fromkeys(str(item) for item in source_event_ids if str(item)))
        saved = 0
        organized: list[tuple[str, str, int]] = []
        now = _iso(_now())
        with self._connection(immediate=True) as connection:
            self._ensure_contact(connection, contact_id)
            for candidate in candidates[:10]:
                if not isinstance(candidate, dict):
                    continue
                content = str(candidate.get("content") or "").strip()
                evidence = str(candidate.get("evidence_quote") or "").strip()
                memory_type = str(candidate.get("type") or "event").strip()
                if (
                    not content
                    or not evidence
                    or evidence not in normalized_message
                    or memory_type not in {"event", "preference", "emotion", "relationship"}
                ):
                    continue
                try:
                    importance = max(1, min(int(candidate.get("importance", 3)), 5))
                except (TypeError, ValueError):
                    importance = 3
                try:
                    valence = max(-1.0, min(float(candidate.get("valence", 0.0)), 1.0))
                except (TypeError, ValueError):
                    valence = 0.0
                try:
                    arousal = max(0.0, min(float(candidate.get("arousal", 0.5)), 1.0))
                except (TypeError, ValueError):
                    arousal = 0.5
                memory_layer = {
                    "preference": "core",
                    "relationship": "diary",
                }.get(memory_type, "daily")
                expires_at = _iso(_now() + timedelta(days=21)) if memory_layer == "daily" else None
                stable_key = f"{contact_id}|{'|'.join(normalized_sources)}|{memory_type}|{content}"
                memory_id = str(uuid.uuid5(uuid.NAMESPACE_URL, stable_key))
                result = connection.execute(
                    """
                    INSERT OR IGNORE INTO contact_memories(
                        id, contact_id, memory_type, content, importance, status,
                        source_event_ids_json, created_at, updated_at, memory_layer,
                        valence, arousal, initial_strength, expires_at, source_trace_id
                    ) VALUES (?, ?, ?, ?, ?, 'published', ?, ?, ?, ?, ?, ?, 1.0, ?, ?)
                    """,
                    (
                        memory_id,
                        contact_id,
                        memory_type,
                        content[:2000],
                        importance,
                        json.dumps(normalized_sources, ensure_ascii=False),
                        now,
                        now,
                        memory_layer,
                        valence,
                        arousal,
                        expires_at,
                        memory_id,
                    ),
                )
                saved += int(result.rowcount == 1)
                organized.append((memory_id, content[:2000], importance))
        for memory_id, content, importance in organized:
            self._organize_memory(
                contact_id=contact_id,
                memory_id=memory_id,
                content=content,
                importance=importance,
            )
        return saved

    async def retrieve_context(self, contact_id: str, message: str, *, top_k: int = 5) -> dict:
        profile = self.get_profile(contact_id)
        memories = self.list_memories(contact_id, published_only=True)[:200]
        if not memories:
            return {
                "contact_profile": profile,
                "relevant_memories": [],
                "style_examples": [],
            }

        query_vector = await llm_client.embed(message)
        health = llm_client.embedding_health()
        model_name = health.get("model") or "BAAI/bge-m3"
        fts_scores = self._fts_scores(contact_id, message)
        entity_scores = self._entity_memory_scores(contact_id, message)
        semantic_scores: dict[str, float] = {}
        lexical_scores: dict[str, float] = {}
        scored: list[tuple[float, dict]] = []
        for memory in memories:
            vector = await self._embedding_for_memory(
                contact_id=contact_id,
                memory=memory,
                model_name=model_name,
                expected_dimensions=len(query_vector),
            )
            semantic = (_cosine(query_vector, vector) + 1.0) / 2.0
            semantic_scores[memory["id"]] = semantic
            lexical = max(fts_scores.get(memory["id"], 0.0), self._character_ngram_score(message, memory["content"]))
            lexical_scores[memory["id"]] = lexical
            importance = float(memory["importance"]) / 5.0
            try:
                age_days = max(
                    0.0,
                    (_now() - datetime.fromisoformat(memory["updated_at"]).astimezone(timezone.utc)).total_seconds()
                    / 86400,
                )
            except (TypeError, ValueError):
                age_days = 365.0
            recency = math.exp(-age_days / 180.0)
            half_life = _LAYER_HALF_LIFE_DAYS.get(str(memory["layer"]), 90.0)
            strength = float(memory["initial_strength"]) * math.exp(-math.log(2) * age_days / half_life)
            strength *= 1.0 + 0.5 * float(memory["arousal"])
            strength *= 1.0 + 0.03 * int(memory["recall_count"])
            strength = max(0.01, min(2.0, strength)) / 2.0
            layer_weight = _LAYER_RECALL_WEIGHT.get(str(memory["layer"]), 0.5)
            # Reciprocal Rank Fusion：语义、FTS/中文 n-gram、实体关联各自
            # 给出排序，避免单一向量或整句 FTS 把真正相关的旧事压掉。
            score = 0.62 * semantic + 0.10 * lexical + 0.08 * importance + 0.08 * recency + 0.08 * strength + 0.04 * layer_weight
            scored.append((score, memory))

        ranked_lists = [
            sorted(semantic_scores, key=semantic_scores.get, reverse=True),
            [memory_id for memory_id in sorted(lexical_scores, key=lexical_scores.get, reverse=True) if lexical_scores[memory_id] > 0],
            [memory_id for memory_id in sorted(entity_scores, key=entity_scores.get, reverse=True) if entity_scores[memory_id] > 0],
        ]
        rrf_scores: dict[str, float] = {}
        for ranked in ranked_lists:
            for rank, memory_id in enumerate(ranked, start=1):
                rrf_scores[memory_id] = rrf_scores.get(memory_id, 0.0) + 1.0 / (60.0 + rank)
        max_rrf = max(rrf_scores.values(), default=1.0)
        if rrf_scores:
            scored = [
                (0.58 * (rrf_scores.get(memory["id"], 0.0) / max_rrf) + 0.42 * score, memory)
                for score, memory in scored
            ]
        scored.sort(key=lambda item: item[0], reverse=True)
        selected = scored[: max(1, min(int(top_k), 20))]
        return {
            "contact_profile": profile,
            "relevant_memories": [
                {
                    "id": memory["id"],
                    "layer": memory["layer"],
                    "content": memory["content"],
                    "score": round(score, 6),
                    "recall_permission": self._recall_permission(score),
                }
                for score, memory in selected
            ],
            "memory_associations": self._association_context(
                contact_id,
                [memory["id"] for _, memory in selected],
            ),
            "style_examples": [],
        }

    @staticmethod
    def _recall_permission(score: float) -> str:
        if score >= 0.65:
            return "direct"
        if score >= 0.42:
            return "cautious"
        return "associate_only"

    @staticmethod
    def _character_ngram_score(query: str, content: str) -> float:
        """中文没有空格时使用 2/3 字符 n-gram，避免 FTS 只能整句命中。"""
        query_text = str(query or "").strip().casefold()
        content_text = str(content or "").strip().casefold()
        if not query_text or not content_text:
            return 0.0
        query_grams = {
            query_text[index:index + size]
            for size in (2, 3)
            for index in range(max(0, len(query_text) - size + 1))
        }
        if not query_grams:
            return 1.0 if query_text in content_text else 0.0
        matched = sum(1 for gram in query_grams if gram in content_text)
        return min(1.0, matched / max(1, len(query_grams)))

    def _entity_memory_scores(self, contact_id: str, message: str) -> dict[str, float]:
        query = str(message or "").strip().casefold()
        if not query:
            return {}
        with self._connection() as connection:
            entities = connection.execute(
                """
                SELECT id, canonical_name, display_name
                FROM memory_entities WHERE contact_id=?
                """,
                (contact_id,),
            ).fetchall()
            scores: dict[str, float] = {}
            for entity in entities:
                names = {str(entity["canonical_name"] or "").casefold(), str(entity["display_name"] or "").casefold()}
                if not any(name and name in query for name in names):
                    continue
                mentions = connection.execute(
                    "SELECT memory_id FROM memory_entity_mentions WHERE contact_id=? AND entity_id=?",
                    (contact_id, str(entity["id"])),
                ).fetchall()
                for mention in mentions:
                    memory_id = str(mention["memory_id"])
                    scores[memory_id] = scores.get(memory_id, 0.0) + 1.0
        return scores

    def mark_memory_used(
        self,
        *,
        contact_id: str,
        memory_ids: list[str],
        source_message_id: str,
    ) -> int:
        normalized_ids = list(dict.fromkeys(str(item).strip() for item in memory_ids if str(item).strip()))
        normalized_source = str(source_message_id or "").strip()
        if not normalized_ids or not normalized_source:
            return 0
        now = _iso(_now())
        count = 0
        with self._connection(immediate=True) as connection:
            self._ensure_contact(connection, contact_id)
            for memory_id in normalized_ids[:20]:
                updated = connection.execute(
                    """
                    UPDATE contact_memories
                    SET recall_count=recall_count+1, last_recalled_at=?
                    WHERE id=? AND contact_id=? AND status='published'
                    """,
                    (now, memory_id, contact_id),
                )
                if updated.rowcount != 1:
                    continue
                count += 1
                connection.execute(
                    """
                    INSERT INTO memory_recall_audit(
                        id, contact_id, memory_id, source_message_id,
                        selected, used, created_at
                    ) VALUES (?, ?, ?, ?, 1, 1, ?)
                    """,
                    (str(uuid.uuid4()), contact_id, memory_id, normalized_source, now),
                )
        return count

    def _association_context(self, contact_id: str, memory_ids: list[str]) -> dict[str, list[dict]]:
        normalized_ids = list(dict.fromkeys(str(item).strip() for item in memory_ids if str(item).strip()))
        if not normalized_ids:
            return {"entities": [], "episodes": []}
        placeholders = ",".join("?" for _ in normalized_ids)
        with self._connection() as connection:
            entity_rows = connection.execute(
                f"""
                SELECT DISTINCT entity.id, entity.entity_type, entity.display_name,
                                entity.current_status, entity.confidence
                FROM memory_entities AS entity
                JOIN memory_entity_mentions AS mention ON mention.entity_id=entity.id
                WHERE mention.contact_id=? AND mention.memory_id IN ({placeholders})
                ORDER BY entity.updated_at DESC
                LIMIT 20
                """,
                (contact_id, *normalized_ids),
            ).fetchall()
            episode_rows = connection.execute(
                """
                SELECT id, episode_key, title, memory_ids_json, importance, updated_at
                FROM memory_episodes WHERE contact_id=?
                ORDER BY updated_at DESC LIMIT 8
                """,
                (contact_id,),
            ).fetchall()
        selected_set = set(normalized_ids)
        episodes: list[dict] = []
        for row in episode_rows:
            try:
                row_memory_ids = [str(item) for item in json.loads(str(row["memory_ids_json"] or "[]"))]
            except (TypeError, ValueError, json.JSONDecodeError):
                row_memory_ids = []
            if selected_set.intersection(row_memory_ids):
                episodes.append(
                    {
                        "id": str(row["id"]),
                        "key": str(row["episode_key"]),
                        "title": str(row["title"]),
                        "memory_ids": row_memory_ids,
                        "importance": int(row["importance"]),
                        "updated_at": str(row["updated_at"]),
                    }
                )
        return {
            "entities": [
                {
                    "id": str(row["id"]),
                    "type": str(row["entity_type"]),
                    "name": str(row["display_name"]),
                    "status": str(row["current_status"]),
                    "confidence": float(row["confidence"]),
                }
                for row in entity_rows
            ],
            "episodes": episodes,
        }

    def list_memory_associations(self, contact_id: str) -> dict[str, list]:
        with self._connection() as connection:
            self._ensure_contact(connection, contact_id, include_disabled=True)
            entities = connection.execute(
                """
                SELECT id, entity_type, display_name, current_status, confidence,
                       facts_json, updated_at
                FROM memory_entities WHERE contact_id=?
                ORDER BY updated_at DESC
                """,
                (contact_id,),
            ).fetchall()
            episodes = connection.execute(
                """
                SELECT id, episode_key, title, memory_ids_json, importance, start_at, end_at, updated_at
                FROM memory_episodes WHERE contact_id=?
                ORDER BY updated_at DESC
                """,
                (contact_id,),
            ).fetchall()
        memory_ids: list[str] = []
        normalized_entities: list[dict] = []
        for row in entities:
            try:
                entity_memory_ids = [str(item) for item in json.loads(str(row["facts_json"] or "[]"))]
            except (TypeError, ValueError, json.JSONDecodeError):
                entity_memory_ids = []
            memory_ids.extend(entity_memory_ids)
            normalized_entities.append(
                {
                    "id": str(row["id"]),
                    "type": str(row["entity_type"]),
                    "name": str(row["display_name"]),
                    "status": str(row["current_status"]),
                    "confidence": float(row["confidence"]),
                    "memory_ids": entity_memory_ids,
                    "updated_at": str(row["updated_at"]),
                }
            )
        normalized_episodes: list[dict] = []
        for row in episodes:
            try:
                episode_memory_ids = [str(item) for item in json.loads(str(row["memory_ids_json"] or "[]"))]
            except (TypeError, ValueError, json.JSONDecodeError):
                episode_memory_ids = []
            memory_ids.extend(episode_memory_ids)
            normalized_episodes.append(
                {
                    "id": str(row["id"]),
                    "key": str(row["episode_key"]),
                    "title": str(row["title"]),
                    "memory_ids": episode_memory_ids,
                    "importance": int(row["importance"]),
                    "start_at": str(row["start_at"]),
                    "end_at": str(row["end_at"]),
                    "updated_at": str(row["updated_at"]),
                }
            )
        return {
            "memory_ids": list(dict.fromkeys(memory_ids)),
            "entities": normalized_entities,
            "episodes": normalized_episodes,
        }

    def consolidate_expired_memories(self, contact_id: str, *, now: datetime | None = None) -> dict[str, int]:
        """提前 24 小时消化 daily 记忆；只做可追溯提炼，不让模型杜撰新事实。"""
        current = now or _now()
        current_iso = _iso(current)
        kept = 0
        archived = 0
        with self._connection(immediate=True) as connection:
            self._ensure_contact(connection, contact_id, include_disabled=True)
            rows = connection.execute(
                """
                SELECT * FROM contact_memories
                WHERE contact_id=? AND memory_layer='daily' AND status='published'
                  AND expires_at IS NOT NULL AND julianday(expires_at) <= julianday(?, '+1 day')
                ORDER BY created_at, id
                """,
                (contact_id, current_iso),
            ).fetchall()
            for row in rows:
                important = (
                    int(row["importance"]) >= 4
                    or abs(float(row["valence"])) >= 0.6
                    or float(row["arousal"]) >= 0.7
                )
                if important:
                    diary_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"dream|{contact_id}|{row['id']}"))
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO contact_memories(
                            id, contact_id, memory_type, content, importance, status,
                            source_event_ids_json, created_at, updated_at, memory_layer,
                            valence, arousal, initial_strength, expires_at, source_trace_id,
                            consolidated_from_json
                        ) VALUES (?, ?, ?, ?, ?, 'published', ?, ?, ?, 'diary', ?, ?, 1.0, NULL, ?, ?)
                        """,
                        (
                            diary_id,
                            contact_id,
                            str(row["memory_type"]),
                            str(row["content"]),
                            int(row["importance"]),
                            str(row["source_event_ids_json"]),
                            current_iso,
                            current_iso,
                            float(row["valence"]),
                            float(row["arousal"]),
                            str(row["source_trace_id"] or row["id"]),
                            json.dumps([str(row["id"])], ensure_ascii=False),
                        ),
                    )
                    kept += 1
                connection.execute(
                    "UPDATE contact_memories SET status='archived', updated_at=? WHERE id=? AND contact_id=?",
                    (current_iso, str(row["id"]), contact_id),
                )
                archived += 1
        return {"examined": archived, "kept_as_diary": kept, "archived": archived}

    def _fts_scores(self, contact_id: str, message: str) -> dict[str, float]:
        query = str(message or "").strip().replace('"', '""')
        if not query:
            return {}
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT memory_id, bm25(contact_memories_fts) AS rank
                    FROM contact_memories_fts
                    WHERE contact_memories_fts MATCH ? AND contact_id=?
                    LIMIT 50
                    """,
                    (f'"{query}"', contact_id),
                ).fetchall()
        except sqlite3.Error:
            return {}
        return {
            str(row["memory_id"]): 1.0 / (1.0 + abs(float(row["rank"] or 0.0)))
            for row in rows
        }

    async def _embedding_for_memory(
        self,
        *,
        contact_id: str,
        memory: dict,
        model_name: str,
        expected_dimensions: int,
    ) -> list[float]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT model, dimensions, vector_json
                FROM contact_memory_embeddings
                WHERE memory_id=? AND contact_id=?
                """,
                (memory["id"], contact_id),
            ).fetchone()
        if row is not None and str(row["model"]) == model_name and int(row["dimensions"]) == expected_dimensions:
            try:
                vector = json.loads(str(row["vector_json"]))
                if isinstance(vector, list) and len(vector) == expected_dimensions:
                    return [float(value) for value in vector]
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        vector = await llm_client.embed(str(memory["content"]))
        with self._connection(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO contact_memory_embeddings(
                    memory_id, contact_id, model, dimensions, vector_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    contact_id=excluded.contact_id,
                    model=excluded.model,
                    dimensions=excluded.dimensions,
                    vector_json=excluded.vector_json,
                    updated_at=excluded.updated_at
                """,
                (
                    memory["id"],
                    contact_id,
                    model_name,
                    len(vector),
                    json.dumps(vector, separators=(",", ":")),
                    _iso(_now()),
                ),
            )
        return vector

    def commit_relationship(
        self,
        *,
        contact_id: str,
        expected_version: int,
        state: InteractionState,
        comfort: float,
    ) -> ContactRelationshipSnapshot:
        now = _iso(_now())
        with self._connection(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE contact_relationships
                SET stage=?, trust=?, intimacy=?, affection=?, comfort=?, resentment=?, intimacy_context=?,
                    last_chat_at=?, version=version+1, updated_at=?
                WHERE contact_id=? AND version=?
                """,
                (
                    state.relationship.stage.value,
                    state.relationship.trust,
                    state.relationship.intimacy,
                    state.emotion.affection,
                    max(0.0, min(float(comfort), 100.0)),
                    state.relationship.resentment,
                    state.relationship.intimacy_context,
                    state.activity.last_chat_at.isoformat() if state.activity.last_chat_at else None,
                    now,
                    contact_id,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise ContactRuntimeConflict("联系人关系状态版本冲突")
        return self.get_relationship(contact_id)
