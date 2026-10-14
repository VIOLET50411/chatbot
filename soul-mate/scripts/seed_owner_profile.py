"""写入 Owner 明确确认的跨角色资料；可重复安全执行。"""
from __future__ import annotations

import json

from core.device_context import get_device_context_service
from scripts.character_registry import list_characters, upsert_character


def seed_owner_profile() -> dict[str, object]:
    service = get_device_context_service()
    service.set_owner_fact(
        fact_key="birth_date",
        fact_value="2005-10-12",
        owner_user_id="owner",
        source="owner_confirmation_2026-08-08",
    )
    updated_characters: list[str] = []
    for record in list_characters():
        if str(record.get("relationship_user_id") or "") != "owner":
            record["relationship_user_id"] = "owner"
            upsert_character(record)
            updated_characters.append(str(record["id"]))
    return {
        "owner_user_id": "owner",
        "profile": service.owner_profile(owner_user_id="owner"),
        "updated_characters": updated_characters,
    }


if __name__ == "__main__":
    print(json.dumps(seed_owner_profile(), ensure_ascii=False, indent=2))
