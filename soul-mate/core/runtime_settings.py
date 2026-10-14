"""Owner 可在管理台调整的全局运行配置。

普通配置与加密后的密钥保存在 ``data/platform/runtime_settings.db``。
环境变量仍作为首次启动和灾难恢复的默认值；数据库中的显式设置优先。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from cryptography.fernet import Fernet, InvalidToken


logger = logging.getLogger(__name__)

SECRET_SETTING_KEYS = frozenset({
    "DEEPSEEK_API_KEY",
    "VISION_API_KEY",
    "AUDIO_API_KEY",
    "X_BEARER_TOKEN",
    "SOCIAL_BROWSER_PROXY_TOKEN",
    "PWA_CHAT_TOKEN",
})

EDITABLE_SETTING_KEYS = frozenset({
    *SECRET_SETTING_KEYS,
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_PLANNER_MODEL",
    "DEEPSEEK_RENDERER_MODEL",
    "VISION_BASE_URL",
    "VISION_MODEL",
    "AUDIO_BASE_URL",
    "AUDIO_MODEL",
    "EXTERNAL_RSS_FEEDS_JSON",
    "EXTERNAL_REFRESH_MINUTES",
    "X_FOLLOW_USERNAMES",
    "SOCIAL_BROWSER_PROXY_URL",
    "SOCIAL_BROWSER_X_ENABLED",
    "SOCIAL_BROWSER_XHS_ENABLED",
    "PROACTIVE_ACTIVE_START_HOUR",
    "PROACTIVE_ACTIVE_END_HOUR",
    "PROACTIVE_CHECK_IN_AFTER_HOURS",
    "PROACTIVE_COOLDOWN_HOURS",
    "PROACTIVE_MAX_DAILY",
    "VPS_TARGET_URL",
    "PWA_CHAT_TOKEN",
})

CHARACTER_SETTING_KEYS = frozenset({
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_PLANNER_MODEL",
    "DEEPSEEK_RENDERER_MODEL",
    "VISION_API_KEY",
    "VISION_BASE_URL",
    "VISION_MODEL",
    "AUDIO_API_KEY",
    "AUDIO_BASE_URL",
    "AUDIO_MODEL",
    "EXTERNAL_RSS_FEEDS_JSON",
    "EXTERNAL_REFRESH_MINUTES",
    "X_BEARER_TOKEN",
    "X_FOLLOW_USERNAMES",
})


def _data_path() -> Path:
    configured = Path(os.getenv("DATA_DIR", "data"))
    root = configured if configured.is_absolute() else Path(__file__).resolve().parents[1] / configured
    return root.resolve() / "platform" / "runtime_settings.db"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _master_material() -> tuple[str, str]:
    candidates = (
        ("dedicated", os.getenv("RUNTIME_SETTINGS_MASTER_KEY", "").strip()),
        ("bridge-token", os.getenv("WECHAT_BRIDGE_TOKEN", "").strip()),
        ("owner-hash", os.getenv("OWNER_PASSWORD_HASH", "").strip()),
    )
    for mode, value in candidates:
        if len(value) >= 24:
            return mode, value
    return "local-fallback", "soul-mate-local-runtime-settings-fallback"


def _fernet() -> tuple[Fernet, str]:
    mode, material = _master_material()
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())
    return Fernet(key), mode


def encrypt_sensitive_text(value: str) -> str:
    cipher, _ = _fernet()
    return cipher.encrypt(str(value).encode("utf-8")).decode("ascii")


def decrypt_sensitive_text(value: str) -> str:
    cipher, _ = _fernet()
    try:
        return cipher.decrypt(str(value).encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise ValueError("敏感资料无法解密") from exc


class RuntimeSettingsStore:
    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path) if database_path else _data_path()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
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
                CREATE TABLE IF NOT EXISTS owner_runtime_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    encrypted INTEGER NOT NULL DEFAULT 0 CHECK (encrypted IN (0, 1)),
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS character_runtime_settings (
                    character_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    encrypted INTEGER NOT NULL DEFAULT 0 CHECK (encrypted IN (0, 1)),
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(character_id, key)
                );
                CREATE INDEX IF NOT EXISTS idx_character_runtime_settings_character
                ON character_runtime_settings(character_id, updated_at DESC);
                """
            )

    @property
    def encryption_mode(self) -> str:
        return _master_material()[0]

    def get(self, key: str, default: str = "") -> str:
        normalized = str(key or "").strip().upper()
        if normalized not in EDITABLE_SETTING_KEYS:
            return default
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value, encrypted FROM owner_runtime_settings WHERE key=?",
                (normalized,),
            ).fetchone()
        if row is None:
            return os.getenv(normalized, default)
        value = str(row["value"] or "")
        if not bool(row["encrypted"]):
            return value
        cipher, _ = _fernet()
        try:
            return cipher.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            logger.error("运行密钥无法解密 key=%s", normalized)
            return ""

    def is_overridden(self, key: str) -> bool:
        normalized = str(key or "").strip().upper()
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM owner_runtime_settings WHERE key=?",
                (normalized,),
            ).fetchone() is not None

    def is_configured(self, key: str) -> bool:
        return bool(self.get(key, "").strip())

    def get_character(self, character_id: str, key: str, default: str = "") -> str:
        normalized_character = str(character_id or "").strip().lower()
        normalized_key = str(key or "").strip().upper()
        if not normalized_character or normalized_key not in CHARACTER_SETTING_KEYS:
            return self.get(normalized_key, default)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value, encrypted FROM character_runtime_settings WHERE character_id=? AND key=?",
                (normalized_character, normalized_key),
            ).fetchone()
        if row is None:
            return self.get(normalized_key, default)
        value = str(row["value"] or "")
        if not bool(row["encrypted"]):
            return value
        try:
            return decrypt_sensitive_text(value)
        except ValueError:
            logger.error(
                "人物运行密钥无法解密 character=%s key=%s",
                normalized_character,
                normalized_key,
            )
            return ""

    def is_character_overridden(self, character_id: str, key: str) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM character_runtime_settings WHERE character_id=? AND key=?",
                (str(character_id or "").strip().lower(), str(key or "").strip().upper()),
            ).fetchone() is not None

    def is_character_configured(self, character_id: str, key: str) -> bool:
        return bool(self.get_character(character_id, key, "").strip())

    def set_many(self, values: dict[str, str], *, actor: str) -> None:
        normalized_values: dict[str, str] = {}
        for key, value in values.items():
            normalized = str(key or "").strip().upper()
            if normalized not in EDITABLE_SETTING_KEYS:
                raise ValueError(f"不允许修改运行设置：{normalized}")
            normalized_values[normalized] = str(value or "").strip()
        if not normalized_values:
            return
        cipher, _ = _fernet()
        now = _iso_now()
        with self._connection() as connection:
            for key, value in normalized_values.items():
                encrypted = key in SECRET_SETTING_KEYS
                stored = cipher.encrypt(value.encode("utf-8")).decode("ascii") if encrypted else value
                connection.execute(
                    """
                    INSERT INTO owner_runtime_settings(key, value, encrypted, updated_by, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value,
                        encrypted=excluded.encrypted,
                        updated_by=excluded.updated_by,
                        updated_at=excluded.updated_at
                    """,
                    (key, stored, int(encrypted), str(actor or "owner")[:128], now),
                )

    def clear(self, keys: list[str], *, actor: str) -> None:
        normalized = [str(key or "").strip().upper() for key in keys]
        if any(key not in EDITABLE_SETTING_KEYS for key in normalized):
            raise ValueError("包含不允许清除的运行设置")
        if not normalized:
            return
        cipher, _ = _fernet()
        empty_value = cipher.encrypt(b"").decode("ascii")
        now = _iso_now()
        with self._connection() as connection:
            for key in normalized:
                connection.execute(
                    """
                    INSERT INTO owner_runtime_settings(key, value, encrypted, updated_by, updated_at)
                    VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value,
                        encrypted=1,
                        updated_by=excluded.updated_by,
                        updated_at=excluded.updated_at
                    """,
                    (key, empty_value, str(actor or "owner")[:128], now),
                )

    def set_character_many(self, character_id: str, values: dict[str, str], *, actor: str) -> None:
        normalized_character = str(character_id or "").strip().lower()
        if not normalized_character:
            raise ValueError("人物 ID 不能为空")
        normalized_values: dict[str, str] = {}
        for key, value in values.items():
            normalized = str(key or "").strip().upper()
            if normalized not in CHARACTER_SETTING_KEYS:
                raise ValueError(f"不允许按人物修改运行设置：{normalized}")
            normalized_values[normalized] = str(value or "").strip()
        if not normalized_values:
            return
        now = _iso_now()
        with self._connection() as connection:
            for key, value in normalized_values.items():
                encrypted = key in SECRET_SETTING_KEYS
                stored = encrypt_sensitive_text(value) if encrypted else value
                connection.execute(
                    """
                    INSERT INTO character_runtime_settings(
                        character_id, key, value, encrypted, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(character_id, key) DO UPDATE SET
                        value=excluded.value,
                        encrypted=excluded.encrypted,
                        updated_by=excluded.updated_by,
                        updated_at=excluded.updated_at
                    """,
                    (
                        normalized_character,
                        key,
                        stored,
                        int(encrypted),
                        str(actor or "owner")[:128],
                        now,
                    ),
                )

    def clear_character(self, character_id: str, keys: list[str], *, actor: str) -> None:
        normalized = [str(key or "").strip().upper() for key in keys]
        if any(key not in CHARACTER_SETTING_KEYS or key not in SECRET_SETTING_KEYS for key in normalized):
            raise ValueError("包含不可按人物清除的密钥")
        if not normalized:
            return
        self.set_character_many(
            character_id,
            {key: "" for key in normalized},
            actor=actor,
        )


_STORE: RuntimeSettingsStore | None = None


def get_runtime_settings_store() -> RuntimeSettingsStore:
    global _STORE
    path = _data_path()
    if _STORE is None or _STORE.database_path.resolve() != path.resolve():
        _STORE = RuntimeSettingsStore(path)
    return _STORE


def runtime_value(key: str, default: str = "", *, character_id: str | None = None) -> str:
    normalized = str(key or "").strip().upper()
    store = get_runtime_settings_store()
    if normalized not in CHARACTER_SETTING_KEYS:
        return store.get(normalized, default)
    selected = str(character_id or "").strip().lower()
    if not selected:
        try:
            from core.character_context import get_character_context

            context = get_character_context()
            selected = context.character_id if context else ""
        except (ImportError, RuntimeError):
            selected = ""
    return store.get_character(selected, normalized, default) if selected else store.get(normalized, default)
