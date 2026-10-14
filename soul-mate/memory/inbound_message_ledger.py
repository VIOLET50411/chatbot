"""角色级入站消息账本。

账本按 ``character_id + MsgId`` 去重。它只保证本地生成流程至多由一个
处理者执行；外部消息网关的投递结果不能据此推断为 exactly-once。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from core.character_context import CharacterContext


_STATUS = Literal["processing", "failed", "completed"]
_ACTION = Literal["claimed", "processing", "completed"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_message_ledger (
    character_id TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'failed', 'completed')),
    reply_json TEXT NOT NULL DEFAULT '[]',
    error_summary TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (character_id, msg_id)
);

CREATE INDEX IF NOT EXISTS idx_inbound_message_ledger_status
ON inbound_message_ledger(character_id, status, updated_at DESC);
"""


class InboundMessageLedgerStateConflict(RuntimeError):
    """消息处理权已不属于当前调用方。"""


@dataclass(frozen=True, slots=True)
class InboundMessageClaim:
    """一次原子账本申请的结果。"""

    action: _ACTION
    reply_chunks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InboundMessageLedgerEntry:
    character_id: str
    msg_id: str
    status: _STATUS
    reply_chunks: tuple[str, ...]
    error_summary: str


def _database_path(context: CharacterContext) -> Path:
    return context.workspace_dir / "soul_mate.db"


def _connect(context: CharacterContext) -> sqlite3.Connection:
    context.workspace_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(_database_path(context), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _normalized_msg_id(msg_id: str) -> str:
    normalized = str(msg_id or "").strip()
    if not normalized:
        raise ValueError("MsgId 不能为空")
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_chunks(raw: str) -> tuple[str, ...]:
    try:
        values = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        values = []
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, str))


def _entry(row: sqlite3.Row) -> InboundMessageLedgerEntry:
    return InboundMessageLedgerEntry(
        character_id=str(row["character_id"]),
        msg_id=str(row["msg_id"]),
        status=str(row["status"]),
        reply_chunks=_decode_chunks(row["reply_json"]),
        error_summary=str(row["error_summary"] or ""),
    )


def claim_inbound_message(
    context: CharacterContext,
    msg_id: str,
) -> InboundMessageClaim:
    """原子申请处理权；失败记录可由后续同一 MsgId 重新申请。"""
    normalized_msg_id = _normalized_msg_id(msg_id)
    now = _now()
    connection = _connect(context)
    try:
        with connection:
            connection.executescript(_SCHEMA)
            inserted = connection.execute(
                """
                INSERT INTO inbound_message_ledger
                (character_id, msg_id, status, reply_json, error_summary, started_at, completed_at, updated_at)
                VALUES (?, ?, 'processing', '[]', '', ?, NULL, ?)
                ON CONFLICT(character_id, msg_id) DO NOTHING
                """,
                (context.character_id, normalized_msg_id, now, now),
            )
            if inserted.rowcount == 1:
                return InboundMessageClaim(action="claimed")

            row = connection.execute(
                """
                SELECT status, reply_json
                FROM inbound_message_ledger
                WHERE character_id=? AND msg_id=?
                """,
                (context.character_id, normalized_msg_id),
            ).fetchone()
            if row is None:
                raise InboundMessageLedgerStateConflict("入站消息账本记录丢失")
            if row["status"] == "completed":
                return InboundMessageClaim(
                    action="completed",
                    reply_chunks=_decode_chunks(row["reply_json"]),
                )
            if row["status"] == "processing":
                return InboundMessageClaim(action="processing")

            retried = connection.execute(
                """
                UPDATE inbound_message_ledger
                SET status='processing', reply_json='[]', error_summary='',
                    started_at=?, completed_at=NULL, updated_at=?
                WHERE character_id=? AND msg_id=? AND status='failed'
                """,
                (now, now, context.character_id, normalized_msg_id),
            )
            if retried.rowcount == 1:
                return InboundMessageClaim(action="claimed")

            current = connection.execute(
                """
                SELECT status, reply_json
                FROM inbound_message_ledger
                WHERE character_id=? AND msg_id=?
                """,
                (context.character_id, normalized_msg_id),
            ).fetchone()
            if current is not None and current["status"] == "completed":
                return InboundMessageClaim(
                    action="completed",
                    reply_chunks=_decode_chunks(current["reply_json"]),
                )
            return InboundMessageClaim(action="processing")
    finally:
        connection.close()


def complete_inbound_message(
    context: CharacterContext,
    msg_id: str,
    reply_chunks: list[str] | tuple[str, ...],
) -> None:
    """持久化已生成回复；后续重投递可直接复用，避免重调引擎。"""
    normalized_msg_id = _normalized_msg_id(msg_id)
    now = _now()
    encoded_chunks = json.dumps(list(reply_chunks), ensure_ascii=False)
    connection = _connect(context)
    try:
        with connection:
            connection.executescript(_SCHEMA)
            updated = connection.execute(
                """
                UPDATE inbound_message_ledger
                SET status='completed', reply_json=?, error_summary='',
                    completed_at=?, updated_at=?
                WHERE character_id=? AND msg_id=? AND status='processing'
                """,
                (encoded_chunks, now, now, context.character_id, normalized_msg_id),
            )
            if updated.rowcount != 1:
                raise InboundMessageLedgerStateConflict("入站消息无法完成：处理权已失效")
    finally:
        connection.close()


def fail_inbound_message(
    context: CharacterContext,
    msg_id: str,
    error: BaseException | str,
) -> None:
    """将当前失败显式落账，使后续重投递可以安全重试。"""
    normalized_msg_id = _normalized_msg_id(msg_id)
    summary = str(error)
    if isinstance(error, BaseException):
        summary = f"{type(error).__name__}: {summary}"
    now = _now()
    connection = _connect(context)
    try:
        with connection:
            connection.executescript(_SCHEMA)
            updated = connection.execute(
                """
                UPDATE inbound_message_ledger
                SET status='failed', reply_json='[]', error_summary=?,
                    completed_at=?, updated_at=?
                WHERE character_id=? AND msg_id=? AND status='processing'
                """,
                (summary[:500], now, now, context.character_id, normalized_msg_id),
            )
            if updated.rowcount != 1:
                raise InboundMessageLedgerStateConflict("入站消息无法标记失败：处理权已失效")
    finally:
        connection.close()


def get_inbound_message_entry(
    context: CharacterContext,
    msg_id: str,
) -> InboundMessageLedgerEntry | None:
    """读取测试与运维所需的单条账本状态。"""
    normalized_msg_id = _normalized_msg_id(msg_id)
    connection = _connect(context)
    try:
        with connection:
            connection.executescript(_SCHEMA)
            row = connection.execute(
                """
                SELECT character_id, msg_id, status, reply_json, error_summary
                FROM inbound_message_ledger
                WHERE character_id=? AND msg_id=?
                """,
                (context.character_id, normalized_msg_id),
            ).fetchone()
    finally:
        connection.close()
    return _entry(row) if row is not None else None
