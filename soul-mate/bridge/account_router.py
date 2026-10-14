"""把宿主机微信账号路由到唯一角色工作空间。"""
from __future__ import annotations

import re

from bridge.v1_store import BridgeStore
from core.character_context import CharacterContext
from scripts.character_registry import get_character_by_account


DEFAULT_BRIDGE_ACCOUNT_ID = "du-jiaying"
_ACCOUNT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class UnknownBridgeAccount(LookupError):
    """桥接账号没有绑定到已启用角色。"""


def normalize_account_id(value: object) -> str:
    normalized = str(value or DEFAULT_BRIDGE_ACCOUNT_ID).strip().lower()
    if not _ACCOUNT_ID_PATTERN.fullmatch(normalized):
        raise UnknownBridgeAccount("微信账号 ID 无效")
    return normalized


def character_context_for_account(account_id: object) -> CharacterContext:
    normalized = normalize_account_id(account_id)
    record = get_character_by_account(normalized)
    if record is None:
        raise UnknownBridgeAccount("微信账号尚未绑定到可用角色")
    return CharacterContext.from_record(record)


def bridge_store_for_account(account_id: object) -> BridgeStore:
    normalized = normalize_account_id(account_id)
    record = get_character_by_account(normalized)
    if record is None:
        # 保留旧代理和隔离测试的默认路径；非默认账号必须先在注册表绑定。
        if normalized != DEFAULT_BRIDGE_ACCOUNT_ID:
            raise UnknownBridgeAccount("微信账号尚未绑定到可用角色")
        store = BridgeStore.from_environment()
    elif str(record["id"]) == "default":
        # 默认角色继续跟随 DATA_DIR，确保现有杜佳颖数据库原地使用。
        store = BridgeStore.from_environment()
    else:
        context = CharacterContext.from_record(record)
        store = BridgeStore(context.workspace_dir / "soul_mate.db")
    store.initialize_schema()
    return store
