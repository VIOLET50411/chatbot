"""版本化 Owner 资料、人物知情状态与重要目标。"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from core.runtime_settings import decrypt_sensitive_text, encrypt_sensitive_text


logger = logging.getLogger(__name__)

PROFILE_CATEGORIES = frozenset({
    "explicit_fact",
    "conditional_rule",
    "stable_preference",
    "sensitive_self_report",
    "model_inference",
    "rejected_inference",
})
SENSITIVITY_LEVELS = frozenset({"normal", "private", "high"})
ENTRY_STATUSES = frozenset({"active", "outdated", "deleted"})
DISCLOSURE_STATES = frozenset({"unknown", "context_retrieved", "confirmed_known"})
KNOWLEDGE_PROFILE_CATEGORIES = frozenset({
    "explicit_fact",
    "stable_preference",
    "sensitive_self_report",
})
RESPONSE_RULE_CATEGORIES = frozenset({"conditional_rule", "rejected_inference"})
_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _database_path() -> Path:
    configured = Path(os.getenv("OWNER_PROFILE_DB", "data/platform/owner_profile.db")).expanduser()
    if not configured.is_absolute():
        configured = Path(__file__).resolve().parents[1] / configured
    return configured.resolve()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, maximum: int, *, required: bool = False) -> str:
    normalized = str(value or "").strip()
    if required and not normalized:
        raise ValueError("资料字段不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"资料字段不能超过 {maximum} 个字符")
    return normalized


class OwnerProfileStore:
    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path).resolve() if database_path else _database_path()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner_profile_entries (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    profile_key TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    current_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner_user_id, profile_key)
                );
                CREATE INDEX IF NOT EXISTS idx_owner_profile_entries_owner
                ON owner_profile_entries(owner_user_id, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS owner_profile_versions (
                    id TEXT PRIMARY KEY,
                    entry_id TEXT NOT NULL REFERENCES owner_profile_entries(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    raw_quote TEXT NOT NULL,
                    encrypted INTEGER NOT NULL DEFAULT 0 CHECK (encrypted IN (0, 1)),
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                    sensitivity TEXT NOT NULL,
                    topic_tags_json TEXT NOT NULL,
                    valid_from TEXT,
                    valid_until TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    restored_from_version INTEGER,
                    UNIQUE(entry_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_owner_profile_versions_entry
                ON owner_profile_versions(entry_id, version DESC);

                CREATE TABLE IF NOT EXISTS character_disclosures (
                    character_id TEXT NOT NULL,
                    entry_id TEXT NOT NULL REFERENCES owner_profile_entries(id) ON DELETE CASCADE,
                    state TEXT NOT NULL DEFAULT 'unknown',
                    evidence TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(character_id, entry_id)
                );

                CREATE TABLE IF NOT EXISTS owner_goals (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    character_id TEXT NOT NULL DEFAULT '',
                    contact_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL,
                    due_at TEXT,
                    next_step TEXT NOT NULL,
                    followup_policy TEXT NOT NULL,
                    encrypted INTEGER NOT NULL DEFAULT 0 CHECK (encrypted IN (0, 1)),
                    last_followed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_owner_goals_owner
                ON owner_goals(owner_user_id, status, updated_at DESC);
                """
            )
            goal_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(owner_goals)").fetchall()
            }
            if "character_id" not in goal_columns:
                connection.execute("ALTER TABLE owner_goals ADD COLUMN character_id TEXT NOT NULL DEFAULT ''")
            if "contact_id" not in goal_columns:
                connection.execute("ALTER TABLE owner_goals ADD COLUMN contact_id TEXT NOT NULL DEFAULT ''")
            if "encrypted" not in goal_columns:
                connection.execute(
                    "ALTER TABLE owner_goals ADD COLUMN encrypted INTEGER NOT NULL DEFAULT 0"
                )
            if "last_followed_at" not in goal_columns:
                connection.execute("ALTER TABLE owner_goals ADD COLUMN last_followed_at TEXT")

    @staticmethod
    def _validate_category(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in PROFILE_CATEGORIES:
            raise ValueError("资料类型无效")
        return normalized

    @staticmethod
    def _validate_sensitivity(value: str) -> str:
        normalized = str(value or "normal").strip().lower()
        if normalized not in SENSITIVITY_LEVELS:
            raise ValueError("敏感级别无效")
        return normalized

    @staticmethod
    def _normalize_tags(tags: list[str] | None) -> list[str]:
        result = list(dict.fromkeys(str(item or "").strip() for item in (tags or []) if str(item or "").strip()))
        if len(result) > 30 or any(len(item) > 40 for item in result):
            raise ValueError("资料话题标签过多或过长")
        return result

    def upsert_entry(
        self,
        *,
        profile_key: str,
        category: str,
        title: str,
        content: str,
        raw_quote: str = "",
        source: str,
        confidence: float = 1.0,
        sensitivity: str = "normal",
        topic_tags: list[str] | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        owner_user_id: str = "owner",
        actor: str = "owner",
        restored_from_version: int | None = None,
    ) -> dict[str, Any]:
        key = str(profile_key or "").strip().lower()
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError("资料键只能包含小写字母、数字、点、下划线和短横线")
        normalized_category = self._validate_category(category)
        normalized_title = _text(title, 200, required=True)
        normalized_content = _text(content, 40_000, required=True)
        normalized_quote = _text(raw_quote, 300_000)
        normalized_source = _text(source, 500, required=True)
        normalized_sensitivity = self._validate_sensitivity(sensitivity)
        normalized_tags = self._normalize_tags(topic_tags)
        normalized_owner = _text(owner_user_id, 80, required=True)
        normalized_actor = _text(actor, 128, required=True)
        normalized_confidence = float(confidence)
        if not 0 <= normalized_confidence <= 1:
            raise ValueError("资料置信度必须在 0 到 1 之间")
        existing = self.get_entry_by_key(
            key,
            owner_user_id=normalized_owner,
            reveal_sensitive=True,
        )
        if existing is not None and existing["status"] == "active":
            same = (
                existing["category"] == normalized_category
                and existing["title"] == normalized_title
                and existing["content"] == normalized_content
                and existing["raw_quote"] == normalized_quote
                and existing["source"] == normalized_source
                and existing["confidence"] == normalized_confidence
                and existing["sensitivity"] == normalized_sensitivity
                and existing["topic_tags"] == normalized_tags
                and existing["valid_from"] == str(valid_from or "")
                and existing["valid_until"] == str(valid_until or "")
                and restored_from_version is None
            )
            if same:
                return self.get_entry(str(existing["id"]), reveal_sensitive=False) or {}
        encrypted = normalized_sensitivity in {"private", "high"}
        stored_content = encrypt_sensitive_text(normalized_content) if encrypted else normalized_content
        stored_quote = encrypt_sensitive_text(normalized_quote) if encrypted else normalized_quote
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM owner_profile_entries WHERE owner_user_id=? AND profile_key=?",
                (normalized_owner, key),
            ).fetchone()
            if current is None:
                entry_id = str(uuid.uuid4())
                version = 1
                connection.execute(
                    """
                    INSERT INTO owner_profile_entries(
                        id, owner_user_id, profile_key, category, status,
                        current_version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', 1, ?, ?)
                    """,
                    (entry_id, normalized_owner, key, normalized_category, now, now),
                )
            else:
                entry_id = str(current["id"])
                version = int(current["current_version"]) + 1
                connection.execute(
                    """
                    UPDATE owner_profile_entries
                    SET category=?, status='active', current_version=?, updated_at=?
                    WHERE id=?
                    """,
                    (normalized_category, version, now, entry_id),
                )
            connection.execute(
                """
                INSERT INTO owner_profile_versions(
                    id, entry_id, version, title, content, raw_quote, encrypted,
                    source, confidence, sensitivity, topic_tags_json,
                    valid_from, valid_until, created_by, created_at, restored_from_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    entry_id,
                    version,
                    normalized_title,
                    stored_content,
                    stored_quote,
                    int(encrypted),
                    normalized_source,
                    normalized_confidence,
                    normalized_sensitivity,
                    json.dumps(normalized_tags, ensure_ascii=False),
                    valid_from,
                    valid_until,
                    normalized_actor,
                    now,
                    restored_from_version,
                ),
            )
        return self.get_entry(entry_id, reveal_sensitive=False) or {}

    @staticmethod
    def _view(row: sqlite3.Row, *, reveal_sensitive: bool) -> dict[str, Any]:
        encrypted = bool(row["encrypted"])
        redacted = encrypted and not reveal_sensitive
        content = "" if redacted else str(row["content"])
        raw_quote = "" if redacted else str(row["raw_quote"])
        if encrypted and reveal_sensitive:
            content = decrypt_sensitive_text(content)
            raw_quote = decrypt_sensitive_text(raw_quote)
        return {
            "id": str(row["id"]),
            "owner_user_id": str(row["owner_user_id"]),
            "profile_key": str(row["profile_key"]),
            "category": str(row["category"]),
            "status": str(row["status"]),
            "version": int(row["version"]),
            "title": str(row["title"]),
            "content": content,
            "raw_quote": raw_quote,
            "redacted": redacted,
            "source": str(row["source"]),
            "confidence": float(row["confidence"]),
            "sensitivity": str(row["sensitivity"]),
            "topic_tags": json.loads(str(row["topic_tags_json"] or "[]")),
            "valid_from": str(row["valid_from"] or ""),
            "valid_until": str(row["valid_until"] or ""),
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "restored_from_version": row["restored_from_version"],
        }

    @staticmethod
    def _current_query() -> str:
        return """
            SELECT e.id, e.owner_user_id, e.profile_key, e.category, e.status,
                   e.updated_at, v.*
            FROM owner_profile_entries e
            JOIN owner_profile_versions v
              ON v.entry_id=e.id AND v.version=e.current_version
        """

    def get_entry(self, entry_id: str, *, reveal_sensitive: bool = False) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                self._current_query() + " WHERE e.id=?",
                (str(entry_id or "").strip(),),
            ).fetchone()
        return self._view(row, reveal_sensitive=reveal_sensitive) if row else None

    def get_entry_by_key(
        self,
        profile_key: str,
        *,
        owner_user_id: str = "owner",
        reveal_sensitive: bool = False,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                self._current_query() + " WHERE e.owner_user_id=? AND e.profile_key=?",
                (owner_user_id, str(profile_key or "").strip().lower()),
            ).fetchone()
        return self._view(row, reveal_sensitive=reveal_sensitive) if row else None

    def list_entries(
        self,
        *,
        owner_user_id: str = "owner",
        include_deleted: bool = False,
        reveal_sensitive: bool = False,
    ) -> list[dict[str, Any]]:
        condition = "" if include_deleted else " AND e.status!='deleted'"
        with self._connection() as connection:
            rows = connection.execute(
                self._current_query()
                + f" WHERE e.owner_user_id=?{condition} ORDER BY e.updated_at DESC, e.profile_key",
                (owner_user_id,),
            ).fetchall()
        return [self._view(row, reveal_sensitive=reveal_sensitive) for row in rows]

    def list_versions(self, entry_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            entry = connection.execute(
                "SELECT * FROM owner_profile_entries WHERE id=?", (entry_id,)
            ).fetchone()
            if entry is None:
                raise KeyError("Owner 资料不存在")
            rows = connection.execute(
                """
                SELECT e.id, e.owner_user_id, e.profile_key, e.category, e.status,
                       e.updated_at, v.*
                FROM owner_profile_entries e
                JOIN owner_profile_versions v ON v.entry_id=e.id
                WHERE e.id=? ORDER BY v.version DESC
                """,
                (entry_id,),
            ).fetchall()
        return [self._view(row, reveal_sensitive=False) for row in rows]

    def restore_version(self, entry_id: str, version: int, *, actor: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT e.owner_user_id, e.profile_key, e.category, v.*
                FROM owner_profile_entries e
                JOIN owner_profile_versions v ON v.entry_id=e.id
                WHERE e.id=? AND v.version=?
                """,
                (entry_id, int(version)),
            ).fetchone()
        if row is None:
            raise KeyError("Owner 资料版本不存在")
        encrypted = bool(row["encrypted"])
        content = decrypt_sensitive_text(str(row["content"])) if encrypted else str(row["content"])
        raw_quote = decrypt_sensitive_text(str(row["raw_quote"])) if encrypted else str(row["raw_quote"])
        return self.upsert_entry(
            profile_key=str(row["profile_key"]),
            category=str(row["category"]),
            title=str(row["title"]),
            content=content,
            raw_quote=raw_quote,
            source=str(row["source"]),
            confidence=float(row["confidence"]),
            sensitivity=str(row["sensitivity"]),
            topic_tags=json.loads(str(row["topic_tags_json"] or "[]")),
            valid_from=str(row["valid_from"] or "") or None,
            valid_until=str(row["valid_until"] or "") or None,
            owner_user_id=str(row["owner_user_id"]),
            actor=actor,
            restored_from_version=int(version),
        )

    def set_status(self, entry_id: str, status: str) -> dict[str, Any]:
        normalized = str(status or "").strip().lower()
        if normalized not in ENTRY_STATUSES:
            raise ValueError("Owner 资料状态无效")
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE owner_profile_entries SET status=?, updated_at=? WHERE id=?",
                (normalized, _now(), entry_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("Owner 资料不存在")
        return self.get_entry(entry_id) or {}

    def set_disclosure(
        self,
        *,
        character_id: str,
        entry_id: str,
        state: str,
        evidence: str = "",
        actor: str = "owner",
    ) -> dict[str, Any]:
        normalized_state = str(state or "").strip().lower()
        if normalized_state not in DISCLOSURE_STATES:
            raise ValueError("人物知情状态无效")
        if self.get_entry(entry_id) is None:
            raise KeyError("Owner 资料不存在")
        normalized_character = _text(character_id, 64, required=True)
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO character_disclosures(
                    character_id, entry_id, state, evidence, updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(character_id, entry_id) DO UPDATE SET
                    state=excluded.state,
                    evidence=excluded.evidence,
                    updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                """,
                (
                    normalized_character,
                    entry_id,
                    normalized_state,
                    _text(evidence, 2_000),
                    _text(actor, 128, required=True),
                    now,
                ),
            )
        return {
            "character_id": normalized_character,
            "entry_id": entry_id,
            "state": normalized_state,
            "evidence": _text(evidence, 2_000),
            "updated_at": now,
        }

    def list_disclosures(self, *, character_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT e.id AS entry_id, e.profile_key, v.title,
                       COALESCE(d.state, 'unknown') AS state,
                       COALESCE(d.evidence, '') AS evidence,
                       COALESCE(d.updated_at, '') AS disclosure_updated_at
                FROM owner_profile_entries e
                JOIN owner_profile_versions v
                  ON v.entry_id=e.id AND v.version=e.current_version
                LEFT JOIN character_disclosures d
                  ON d.entry_id=e.id AND d.character_id=?
                WHERE e.status='active'
                  AND e.category IN ('explicit_fact', 'stable_preference', 'sensitive_self_report')
                ORDER BY e.updated_at DESC
                """,
                (character_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def prompt_context(
        self,
        *,
        character_id: str,
        current_message: str,
        include_unknown_topics: bool = False,
    ) -> str:
        query = str(current_message or "").casefold()
        entries = self.list_entries(reveal_sensitive=False)
        disclosure_by_entry = {
            str(item["entry_id"]): str(item["state"])
            for item in self.list_disclosures(character_id=character_id)
        }
        known: list[str] = []
        unknown_relevant: list[str] = []
        response_rules: list[str] = []

        def reveal(entry: dict[str, Any]) -> dict[str, Any] | None:
            if not entry.get("redacted"):
                return entry
            try:
                return self.get_entry(str(entry["id"]), reveal_sensitive=True)
            except ValueError:
                # 密钥配置异常时必须停止使用这一条资料，不能让整条聊天链路崩溃，
                # 也不能把密文或猜测内容交给模型。
                logger.error("Owner 敏感资料无法解密，已从本轮提示中跳过 entry=%s", entry.get("id"))
                return None

        for entry in entries:
            if entry["category"] in RESPONSE_RULE_CATEGORIES:
                revealed = reveal(entry)
                if revealed is not None:
                    response_rules.append(f"- {revealed['title']}：{revealed['content']}")
                continue
            if entry["category"] not in KNOWLEDGE_PROFILE_CATEGORIES:
                # 模型推测只供 Owner 审核，既不是人物已经知道的事实，也不是运行规则。
                continue
            tags = [str(tag).casefold() for tag in entry.get("topic_tags", [])]
            relevant = bool(query and any(tag and tag in query for tag in tags))
            state = disclosure_by_entry.get(str(entry["id"]), "unknown")
            if state == "confirmed_known":
                if entry["sensitivity"] == "normal" or relevant:
                    revealed = reveal(entry)
                    if revealed is not None:
                        known.append(f"- {revealed['title']}：{revealed['content']}")
            elif relevant and include_unknown_topics:
                unknown_relevant.append(f"- {entry['title']}")
        sections: list[str] = []
        if response_rules:
            sections.append(
                "## Owner 已确认的回应偏好与禁用推断\n"
                + "\n".join(response_rules[:16])
                + "\n这些是系统怎样回应 Owner 的规则，不要说成角色从聊天中知道的经历。"
            )
        if known:
            sections.append("## 这个人物已经知道的 Owner 资料\n" + "\n".join(known[:12]))
        if unknown_relevant:
            sections.append(
                "## 可能相关但这个人物尚不知道的资料\n"
                + "\n".join(unknown_relevant[:5])
                + "\n只能自然确认对方是否愿意聊，不能说出资料内容，也不能假装以前听说过。"
            )
        return "\n\n".join(sections)

    def upsert_goal(
        self,
        *,
        title: str,
        detail: str,
        status: str = "active",
        due_at: str | None = None,
        next_step: str = "",
        followup_policy: str = "情绪强烈时先安慰；明确说只听我说时暂停当轮建议",
        owner_user_id: str = "owner",
        character_id: str = "",
        contact_id: str = "",
        sensitivity: str = "normal",
        goal_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_status = str(status or "active").strip().lower()
        if normalized_status not in {"active", "paused", "completed", "cancelled"}:
            raise ValueError("目标状态无效")
        identifier = str(goal_id or "").strip()
        normalized_character = str(character_id or "").strip().lower()
        normalized_contact = str(contact_id or "").strip()
        normalized_sensitivity = self._validate_sensitivity(sensitivity)
        if not identifier:
            with self._connection() as connection:
                same_title = connection.execute(
                    """
                    SELECT id FROM owner_goals
                    WHERE owner_user_id=? AND character_id=? AND contact_id=? AND title=?
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (owner_user_id, normalized_character, normalized_contact, str(title or "").strip()),
                ).fetchone()
            identifier = str(same_title["id"]) if same_title else str(uuid.uuid4())
        now = _now()
        normalized_detail = _text(detail, 5_000)
        normalized_next_step = _text(next_step, 1_000)
        normalized_policy = _text(followup_policy, 2_000, required=True)
        encrypted = normalized_sensitivity != "normal"
        if encrypted:
            normalized_detail = encrypt_sensitive_text(normalized_detail)
            normalized_next_step = encrypt_sensitive_text(normalized_next_step)
            normalized_policy = encrypt_sensitive_text(normalized_policy)
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT created_at, last_followed_at FROM owner_goals WHERE id=?",
                (identifier,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO owner_goals(
                    id, owner_user_id, character_id, contact_id, title, detail, status, due_at,
                    next_step, followup_policy, encrypted, last_followed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    character_id=excluded.character_id, contact_id=excluded.contact_id, title=excluded.title,
                    detail=excluded.detail, status=excluded.status,
                    due_at=excluded.due_at, next_step=excluded.next_step,
                    followup_policy=excluded.followup_policy, encrypted=excluded.encrypted,
                    last_followed_at=NULL, updated_at=excluded.updated_at
                """,
                (
                    identifier,
                    _text(owner_user_id, 80, required=True),
                    normalized_character,
                    normalized_contact,
                    _text(title, 200, required=True),
                    normalized_detail,
                    normalized_status,
                    due_at,
                    normalized_next_step,
                    normalized_policy,
                    int(encrypted),
                    str(existing["last_followed_at"]) if existing and existing["last_followed_at"] else None,
                    str(existing["created_at"]) if existing else now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM owner_goals WHERE id=?", (identifier,)).fetchone()
        return self._goal_view(dict(row)) if row else {}

    @staticmethod
    def _goal_view(goal: dict[str, Any]) -> dict[str, Any]:
        result = dict(goal)
        if not bool(result.get("encrypted")):
            return result
        try:
            for key in ("detail", "next_step", "followup_policy"):
                result[key] = decrypt_sensitive_text(str(result.get(key) or ""))
        except ValueError:
            logger.error("Owner 跟进目标无法解密，已隐藏 goal=%s", result.get("id"))
            result.update({"detail": "", "next_step": "", "followup_policy": ""})
            result["redacted"] = True
        return result

    def list_goals(
        self,
        *,
        owner_user_id: str = "owner",
        character_id: str | None = None,
        contact_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_character = None if character_id is None else str(character_id or "").strip().lower()
        normalized_contact = None if contact_id is None else str(contact_id or "").strip()
        with self._connection() as connection:
            if normalized_character is None and normalized_contact is None:
                rows = connection.execute(
                    "SELECT * FROM owner_goals WHERE owner_user_id=? ORDER BY updated_at DESC",
                    (owner_user_id,),
                ).fetchall()
            elif normalized_contact is None:
                rows = connection.execute(
                    """
                    SELECT * FROM owner_goals
                    WHERE owner_user_id=? AND character_id IN ('', ?)
                    ORDER BY updated_at DESC
                    """,
                    (owner_user_id, normalized_character),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM owner_goals
                    WHERE owner_user_id=? AND character_id IN ('', ?) AND contact_id=?
                    ORDER BY updated_at DESC
                    """,
                    (owner_user_id, normalized_character or "", normalized_contact),
                ).fetchall()
        return [self._goal_view(dict(row)) for row in rows]

    def due_followup_goals(
        self,
        *,
        character_id: str,
        contact_id: str,
        now: datetime,
        owner_user_id: str = "owner",
    ) -> list[dict[str, Any]]:
        due: list[dict[str, Any]] = []
        for goal in self.list_goals(
            owner_user_id=owner_user_id,
            character_id=character_id,
            contact_id=contact_id,
        ):
            if goal.get("status") != "active" or not goal.get("due_at") or goal.get("redacted"):
                continue
            try:
                due_at = datetime.fromisoformat(str(goal["due_at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
            if due_at.astimezone(timezone.utc) > now.astimezone(timezone.utc):
                continue
            followed = str(goal.get("last_followed_at") or "")
            if followed:
                try:
                    followed_at = datetime.fromisoformat(followed.replace("Z", "+00:00"))
                except ValueError:
                    followed_at = None
                if followed_at is not None:
                    if followed_at.tzinfo is None:
                        followed_at = followed_at.replace(tzinfo=timezone.utc)
                    # upsert_goal 会在目标内容或截止时间变化时清空 last_followed_at。
                    # 因此这里应与本次截止时间比较，不能受数据库实际写入时间影响。
                    if followed_at.astimezone(timezone.utc) >= due_at.astimezone(timezone.utc):
                        continue
            due.append(goal)
        return due

    def mark_goal_followed(self, goal_id: str, *, followed_at: datetime | None = None) -> None:
        timestamp = (followed_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE owner_goals SET last_followed_at=? WHERE id=?",
                (timestamp, str(goal_id or "").strip()),
            )
            if cursor.rowcount != 1:
                raise KeyError("跟进目标不存在")


_STORE: OwnerProfileStore | None = None


def get_owner_profile_store() -> OwnerProfileStore:
    global _STORE
    path = _database_path()
    if _STORE is None or _STORE.database_path != path:
        _STORE = OwnerProfileStore(path)
    return _STORE
