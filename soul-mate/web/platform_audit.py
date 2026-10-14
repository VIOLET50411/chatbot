"""平台级统一审计仓储。

与角色工作空间无关的平台操作（Owner 会话、危险操作确认等）统一记录在
`SOUL_MATE_ROOT/data/platform/platform_audit.db` 的一张只追加表中。

设计要点：
- 数据库路径必须落在项目目录内，防止配置注入把审计写到任意位置。
- `admin_audit_event` 表只支持插入与只读查询；本模块不提供更新或删除接口。
- 记录前会拒绝任何在 `before_json` / `after_json` / `reason` 中出现的
  疑似令牌、密码等敏感字段，避免误将其写入审计。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Sequence

__all__ = [
    "AdminAuditEvent",
    "AuditPayloadSecurityError",
    "PlatformAuditStore",
]


_SENSITIVE_KEY_MARKERS: frozenset[str] = frozenset(
    {"password", "passwd", "token", "secret", "csrf", "authorization", "cookie"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_audit_event (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    character_id TEXT,
    resource_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    request_id TEXT NOT NULL DEFAULT '',
    source_ip TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_event_created
ON admin_audit_event(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_event_scope
ON admin_audit_event(scope_type, character_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_event_actor
ON admin_audit_event(actor_id, created_at DESC);
"""


class AuditPayloadSecurityError(ValueError):
    """审计载荷中检测到疑似令牌或密码字段，拒绝写入。"""


@dataclass(frozen=True, slots=True)
class AdminAuditEvent:
    """一条不可变的平台审计记录。"""

    id: str
    actor_id: str
    action: str
    scope_type: str
    character_id: str | None
    resource_id: str | None
    reason: str
    before_json: Mapping[str, Any]
    after_json: Mapping[str, Any]
    request_id: str
    source_ip: str
    outcome: str
    created_at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _project_root() -> Path:
    configured = os.getenv("SOUL_MATE_ROOT", "")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _resolve_audit_db_path() -> Path:
    """解析审计数据库路径，并确保其落在项目目录之内。"""
    root = _project_root()
    candidate = (root / "data" / "platform" / "platform_audit.db").resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("平台审计数据库必须位于项目目录内") from exc
    return candidate


def _scan_for_sensitive_keys(value: Any, path: str = "") -> str | None:
    """递归扫描映射结构，返回第一个疑似敏感字段的路径，否则返回 None。"""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key).lower()
            nested_path = f"{path}.{key}" if path else str(key)
            if any(marker in key_text for marker in _SENSITIVE_KEY_MARKERS):
                return nested_path
            found = _scan_for_sensitive_keys(nested, nested_path)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            found = _scan_for_sensitive_keys(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _encode_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)


def _decode_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_to_event(row: sqlite3.Row) -> AdminAuditEvent:
    return AdminAuditEvent(
        id=row["id"],
        actor_id=row["actor_id"],
        action=row["action"],
        scope_type=row["scope_type"],
        character_id=row["character_id"],
        resource_id=row["resource_id"],
        reason=row["reason"],
        before_json=_decode_json(row["before_json"]),
        after_json=_decode_json(row["after_json"]),
        request_id=row["request_id"],
        source_ip=row["source_ip"],
        outcome=row["outcome"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class PlatformAuditStore:
    """`admin_audit_event` 表的只追加仓储。

    不暴露更新或删除接口；`record` 之外只提供只读查询方法。
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else _resolve_audit_db_path()
        self._schema_lock = Lock()
        self._schema_ready = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        if not self._schema_ready:
            with self._schema_lock:
                connection.executescript(_SCHEMA)
                connection.commit()
                self._schema_ready = True
        return connection

    def record(
        self,
        *,
        actor_id: str,
        action: str,
        scope_type: str,
        outcome: str,
        character_id: str | None = None,
        resource_id: str | None = None,
        reason: str = "",
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        request_id: str = "",
        source_ip: str = "",
    ) -> AdminAuditEvent:
        """追加一条平台审计记录。

        `before` / `after` / `reason` 中若含疑似令牌或密码字段会拒绝写入，
        调用方必须先自行剔除敏感数据。
        """
        before_payload = dict(before or {})
        after_payload = dict(after or {})
        for label, payload in (("before", before_payload), ("after", after_payload)):
            hit = _scan_for_sensitive_keys(payload)
            if hit is not None:
                raise AuditPayloadSecurityError(
                    f"审计 {label} 载荷包含疑似敏感字段：{hit}"
                )
        if any(marker in reason.lower() for marker in ("password=", "token=")):
            raise AuditPayloadSecurityError("审计 reason 中疑似包含令牌或密码")

        event = AdminAuditEvent(
            id=str(uuid.uuid4()),
            actor_id=actor_id,
            action=action,
            scope_type=scope_type,
            character_id=character_id,
            resource_id=resource_id,
            reason=reason,
            before_json=before_payload,
            after_json=after_payload,
            request_id=request_id,
            source_ip=source_ip,
            outcome=outcome,
            created_at=_utcnow(),
        )
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO admin_audit_event (
                    id, actor_id, action, scope_type, character_id, resource_id,
                    reason, before_json, after_json, request_id, source_ip,
                    outcome, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.actor_id,
                    event.action,
                    event.scope_type,
                    event.character_id,
                    event.resource_id,
                    event.reason,
                    _encode_json(event.before_json),
                    _encode_json(event.after_json),
                    event.request_id,
                    event.source_ip,
                    event.outcome,
                    event.created_at.isoformat(),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return event

    def list_events(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        scope_type: str | None = None,
        character_id: str | None = None,
        action: str | None = None,
        actor_id: str | None = None,
        outcome: str | None = None,
    ) -> list[AdminAuditEvent]:
        """按过滤条件分页读取审计记录，按时间倒序排列。"""
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1 到 500 之间")
        if offset < 0:
            raise ValueError("offset 不能为负数")

        clauses: list[str] = []
        params: list[Any] = []
        if scope_type is not None:
            clauses.append("scope_type = ?")
            params.append(scope_type)
        if character_id is not None:
            clauses.append("character_id = ?")
            params.append(character_id)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if actor_id is not None:
            clauses.append("actor_id = ?")
            params.append(actor_id)
        if outcome is not None:
            clauses.append("outcome = ?")
            params.append(outcome)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT * FROM admin_audit_event
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        finally:
            connection.close()
        return [_row_to_event(row) for row in rows]

    def count(
        self,
        *,
        scope_type: str | None = None,
        character_id: str | None = None,
        action: str | None = None,
        actor_id: str | None = None,
        outcome: str | None = None,
    ) -> int:
        """按过滤条件统计审计记录总数。"""
        clauses: list[str] = []
        params: list[Any] = []
        if scope_type is not None:
            clauses.append("scope_type = ?")
            params.append(scope_type)
        if character_id is not None:
            clauses.append("character_id = ?")
            params.append(character_id)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if actor_id is not None:
            clauses.append("actor_id = ?")
            params.append(actor_id)
        if outcome is not None:
            clauses.append("outcome = ?")
            params.append(outcome)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT COUNT(*) AS total FROM admin_audit_event {where}",
                params,
            ).fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

