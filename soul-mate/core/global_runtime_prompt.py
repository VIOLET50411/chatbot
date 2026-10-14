"""不进入人格快照的全局运行提示词及可回滚版本。"""
from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.character_context import resolve_workspace_path


@dataclass(frozen=True, slots=True)
class RuntimePromptVersion:
    id: str
    prompt: str
    created_by: str
    created_at: str
    restored_from_id: str | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class CharacterRuntimePromptVersion:
    id: str
    character_id: str
    prompt: str
    created_by: str
    created_at: str
    restored_from_id: str | None
    is_active: bool


_SCHEMA = """
CREATE TABLE IF NOT EXISTS global_runtime_prompt_versions (
    id TEXT PRIMARY KEY,
    prompt_text TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    restored_from_id TEXT,
    is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_global_runtime_prompt_active
ON global_runtime_prompt_versions(is_active) WHERE is_active=1;
CREATE INDEX IF NOT EXISTS idx_global_runtime_prompt_created
ON global_runtime_prompt_versions(created_at DESC);
CREATE TABLE IF NOT EXISTS character_runtime_prompt_versions (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    restored_from_id TEXT,
    is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_character_runtime_prompt_active
ON character_runtime_prompt_versions(character_id) WHERE is_active=1;
CREATE INDEX IF NOT EXISTS idx_character_runtime_prompt_created
ON character_runtime_prompt_versions(character_id, created_at DESC);
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


def _version(row: sqlite3.Row) -> RuntimePromptVersion:
    return RuntimePromptVersion(
        id=str(row["id"]),
        prompt=str(row["prompt_text"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        restored_from_id=str(row["restored_from_id"]) if row["restored_from_id"] else None,
        is_active=bool(row["is_active"]),
    )


def _character_version(row: sqlite3.Row) -> CharacterRuntimePromptVersion:
    return CharacterRuntimePromptVersion(
        id=str(row["id"]),
        character_id=str(row["character_id"]),
        prompt=str(row["prompt_text"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        restored_from_id=str(row["restored_from_id"]) if row["restored_from_id"] else None,
        is_active=bool(row["is_active"]),
    )


def get_active_prompt_version() -> RuntimePromptVersion | None:
    if not _database_path().is_file():
        return None
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM global_runtime_prompt_versions WHERE is_active=1 LIMIT 1"
        ).fetchone()
        return _version(row) if row else None
    finally:
        connection.close()


def get_active_prompt() -> str:
    current = get_active_prompt_version()
    return current.prompt if current else ""


def list_prompt_versions(*, limit: int = 30) -> list[RuntimePromptVersion]:
    if not _database_path().is_file():
        return []
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM global_runtime_prompt_versions
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        return [_version(row) for row in rows]
    finally:
        connection.close()


def save_active_prompt(
    prompt: str,
    *,
    created_by: str,
    restored_from_id: str | None = None,
) -> RuntimePromptVersion:
    normalized = str(prompt or "").strip()
    if len(normalized) > 12_000:
        raise ValueError("总提示词不能超过 12000 个字符")
    actor = str(created_by or "owner").strip()[:128] or "owner"
    current = get_active_prompt_version()
    if current is not None and current.prompt == normalized and restored_from_id is None:
        return current
    version_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    connection = _connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE global_runtime_prompt_versions SET is_active=0 WHERE is_active=1"
            )
            connection.execute(
                """
                INSERT INTO global_runtime_prompt_versions(
                    id, prompt_text, created_by, created_at, restored_from_id, is_active
                ) VALUES (?, ?, ?, ?, ?, 1)
                """,
                (version_id, normalized, actor, created_at, restored_from_id),
            )
            row = connection.execute(
                "SELECT * FROM global_runtime_prompt_versions WHERE id=?", (version_id,)
            ).fetchone()
            assert row is not None
            return _version(row)
    finally:
        connection.close()


def restore_prompt_version(version_id: str, *, created_by: str) -> RuntimePromptVersion:
    normalized_id = str(version_id or "").strip()
    if not normalized_id:
        raise KeyError("总提示词版本不存在")
    if not _database_path().is_file():
        raise KeyError("总提示词版本不存在")
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM global_runtime_prompt_versions WHERE id=?", (normalized_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise KeyError("总提示词版本不存在")
    return save_active_prompt(
        str(row["prompt_text"]),
        created_by=created_by,
        restored_from_id=normalized_id,
    )


def get_active_character_prompt_version(character_id: str) -> CharacterRuntimePromptVersion | None:
    normalized = str(character_id or "").strip().lower()
    if not normalized or not _database_path().is_file():
        return None
    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT * FROM character_runtime_prompt_versions
            WHERE character_id=? AND is_active=1 LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        return _character_version(row) if row else None
    finally:
        connection.close()


def get_active_character_prompt(character_id: str) -> str:
    current = get_active_character_prompt_version(character_id)
    return current.prompt if current else ""


def list_character_prompt_versions(
    character_id: str,
    *,
    limit: int = 30,
) -> list[CharacterRuntimePromptVersion]:
    normalized = str(character_id or "").strip().lower()
    if not normalized or not _database_path().is_file():
        return []
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM character_runtime_prompt_versions
            WHERE character_id=? ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (normalized, max(1, min(int(limit), 100))),
        ).fetchall()
        return [_character_version(row) for row in rows]
    finally:
        connection.close()


def save_active_character_prompt(
    character_id: str,
    prompt: str,
    *,
    created_by: str,
    restored_from_id: str | None = None,
) -> CharacterRuntimePromptVersion:
    normalized_character = str(character_id or "").strip().lower()
    if not normalized_character:
        raise ValueError("人物 ID 不能为空")
    normalized_prompt = str(prompt or "").strip()
    if len(normalized_prompt) > 12_000:
        raise ValueError("人物运行补充不能超过 12000 个字符")
    current = get_active_character_prompt_version(normalized_character)
    if current is not None and current.prompt == normalized_prompt and restored_from_id is None:
        return current
    version_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    connection = _connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE character_runtime_prompt_versions
                SET is_active=0 WHERE character_id=? AND is_active=1
                """,
                (normalized_character,),
            )
            connection.execute(
                """
                INSERT INTO character_runtime_prompt_versions(
                    id, character_id, prompt_text, created_by,
                    created_at, restored_from_id, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    version_id,
                    normalized_character,
                    normalized_prompt,
                    str(created_by or "owner").strip()[:128] or "owner",
                    created_at,
                    restored_from_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM character_runtime_prompt_versions WHERE id=?",
                (version_id,),
            ).fetchone()
            assert row is not None
            return _character_version(row)
    finally:
        connection.close()


def restore_character_prompt_version(
    character_id: str,
    version_id: str,
    *,
    created_by: str,
) -> CharacterRuntimePromptVersion:
    normalized_character = str(character_id or "").strip().lower()
    normalized_id = str(version_id or "").strip()
    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT * FROM character_runtime_prompt_versions
            WHERE id=? AND character_id=?
            """,
            (normalized_id, normalized_character),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise KeyError("人物运行补充版本不存在")
    return save_active_character_prompt(
        normalized_character,
        str(row["prompt_text"]),
        created_by=created_by,
        restored_from_id=normalized_id,
    )
