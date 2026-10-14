"""把旧全局模型配置复制为每个人物的独立初始配置。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
ENV_VALUES = dotenv_values(PROJECT_ROOT / ".env")
for master_key in ("RUNTIME_SETTINGS_MASTER_KEY", "WECHAT_BRIDGE_TOKEN", "OWNER_PASSWORD_HASH"):
    master_value = str(ENV_VALUES.get(master_key) or "").strip()
    if master_value and not os.getenv(master_key, "").strip():
        os.environ[master_key] = master_value

from core.runtime_settings import (
    CHARACTER_SETTING_KEYS,
    SECRET_SETTING_KEYS,
    get_runtime_settings_store,
)
from scripts.character_registry import list_characters


DEFAULTS = {
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
    "DEEPSEEK_MODEL": "deepseek-v4-pro",
    "VISION_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "VISION_MODEL": "qwen3.7-flash",
    "AUDIO_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "AUDIO_MODEL": "qwen3-asr-flash",
    "EXTERNAL_RSS_FEEDS_JSON": "[]",
    "EXTERNAL_REFRESH_MINUTES": "30",
}


def migrate() -> dict[str, object]:
    store = get_runtime_settings_store()
    migrated: dict[str, list[str]] = {}
    for record in list_characters(include_disabled=True):
        character_id = str(record["id"])
        values: dict[str, str] = {}
        for key in sorted(CHARACTER_SETTING_KEYS):
            if store.is_character_overridden(character_id, key) and (
                key in SECRET_SETTING_KEYS or store.get_character(character_id, key, "").strip()
            ):
                continue
            value = store.get(
                key,
                str(ENV_VALUES.get(key) or DEFAULTS.get(key, "")),
            )
            if not value:
                continue
            values[key] = value
        store.set_character_many(character_id, values, actor="migration")
        migrated[character_id] = sorted(values)
    return {"migrated": migrated, "secret_values_returned": False}


if __name__ == "__main__":
    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
