"""PWA 与人物联系人进度的显式绑定。

绑定只保存人物 ID 到该人物数据库内联系人 ID 的映射，不保存聊天正文。
PWA 必须经过这层绑定，不能默认挑选白名单中的第一个好友。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.character_context import resolve_workspace_path


@dataclass(frozen=True, slots=True)
class PwaContactBinding:
    character_id: str
    contact_id: str
    updated_by: str
    updated_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pwa_contact_bindings (
    character_id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _database_path() -> Path:
    configured = Path(os.getenv("DATA_DIR", "data"))
    if configured.is_absolute():
        data_dir = configured.resolve()
        root = resolve_workspace_path(".")
        try:
            data_dir.relative_to(root)
        except ValueError as exc:
            raise ValueError("DATA_DIR 必须位于项目目录内") from exc
    else:
        data_dir = resolve_workspace_path(configured)
    return data_dir / "platform" / "runtime_settings.db"


def _connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.executescript(_SCHEMA)
    return connection


def get_pwa_contact_binding(character_id: str) -> PwaContactBinding | None:
    normalized = str(character_id or "").strip().lower()
    if not normalized or not _database_path().is_file():
        return None
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM pwa_contact_bindings WHERE character_id=?",
            (normalized,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return PwaContactBinding(
        character_id=str(row["character_id"]),
        contact_id=str(row["contact_id"]),
        updated_by=str(row["updated_by"]),
        updated_at=str(row["updated_at"]),
    )


def set_pwa_contact_binding(
    character_id: str,
    contact_id: str,
    *,
    updated_by: str,
) -> PwaContactBinding:
    normalized_character = str(character_id or "").strip().lower()
    normalized_contact = str(contact_id or "").strip()
    if not normalized_character or not normalized_contact:
        raise ValueError("人物和联系人不能为空")
    actor = str(updated_by or "owner").strip()[:128] or "owner"
    updated_at = datetime.now(timezone.utc).isoformat()
    connection = _connect()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO pwa_contact_bindings(character_id, contact_id, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(character_id) DO UPDATE SET
                    contact_id=excluded.contact_id,
                    updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                """,
                (normalized_character, normalized_contact, actor, updated_at),
            )
    finally:
        connection.close()
    return PwaContactBinding(normalized_character, normalized_contact, actor, updated_at)
