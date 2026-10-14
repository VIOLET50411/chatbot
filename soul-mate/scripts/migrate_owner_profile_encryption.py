"""Re-encrypt Owner profile versions created by the legacy local script key."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.runtime_settings import decrypt_sensitive_text, encrypt_sensitive_text


LEGACY_LOCAL_MATERIAL = "soul-mate-local-runtime-settings-fallback"


def _cipher(material: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())
    return Fernet(key)


def _legacy_decrypt(value: str) -> str:
    return _cipher(LEGACY_LOCAL_MATERIAL).decrypt(value.encode("ascii")).decode("utf-8")


def migrate(database_path: str | Path | None = None) -> dict[str, int]:
    if len(os.getenv("RUNTIME_SETTINGS_MASTER_KEY", "").strip()) < 24:
        raise RuntimeError("请先配置稳定的 RUNTIME_SETTINGS_MASTER_KEY")
    target = Path(database_path) if database_path else PROJECT_ROOT / "data" / "platform" / "owner_profile.db"
    connection = sqlite3.connect(target, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, content, raw_quote FROM owner_profile_versions WHERE encrypted=1 ORDER BY id"
        ).fetchall()
        replacements: list[tuple[str, str, str]] = []
        already_current = 0
        for row in rows:
            content = str(row["content"])
            raw_quote = str(row["raw_quote"])
            try:
                decrypt_sensitive_text(content)
                decrypt_sensitive_text(raw_quote)
                already_current += 1
                continue
            except ValueError:
                pass
            try:
                plain_content = _legacy_decrypt(content)
                plain_quote = _legacy_decrypt(raw_quote)
            except (InvalidToken, UnicodeError, ValueError) as exc:
                raise RuntimeError(f"资料版本 {row['id']} 无法使用已知旧密钥迁移") from exc
            replacements.append(
                (
                    encrypt_sensitive_text(plain_content),
                    encrypt_sensitive_text(plain_quote),
                    str(row["id"]),
                )
            )

        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            "UPDATE owner_profile_versions SET content=?, raw_quote=? WHERE id=?",
            replacements,
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "encrypted_versions": len(rows),
        "migrated_versions": len(replacements),
        "already_current": already_current,
    }


if __name__ == "__main__":
    load_dotenv(PROJECT_ROOT / ".env")
    print(json.dumps(migrate(), ensure_ascii=False))
