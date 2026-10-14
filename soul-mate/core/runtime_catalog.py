"""从角色注册表构建单进程内的隔离运行空间。"""
from __future__ import annotations

from dataclasses import dataclass

from bridge.account_router import bridge_store_for_account
from bridge.v1_store import BridgeStore
from core.character_context import CharacterContext
from scripts.character_registry import list_characters


@dataclass(frozen=True, slots=True)
class CharacterRuntime:
    context: CharacterContext
    account_id: str
    account_label: str
    persona_status: str
    bridge_store: BridgeStore


def list_character_runtimes(*, active_only: bool = False) -> list[CharacterRuntime]:
    runtimes: list[CharacterRuntime] = []
    for record in list_characters(include_disabled=False):
        persona_status = str(record.get("persona_status") or "draft")
        if active_only and persona_status != "active":
            continue
        context = CharacterContext.from_record(record)
        account_id = str(record["account_id"])
        runtimes.append(
            CharacterRuntime(
                context=context,
                account_id=account_id,
                account_label=str(record.get("account_label") or context.display_name),
                persona_status=persona_status,
                bridge_store=bridge_store_for_account(account_id),
            )
        )
    return runtimes
