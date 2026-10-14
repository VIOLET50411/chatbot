"""多角色注册表与微信账号绑定。

每个角色拥有独立工作空间和数据库。注册表只保存运行路由信息，不保存人格正文、
联系人私聊或密钥。旧版只有 ``default`` 一项的文件会被无损升级。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _default_registry_file() -> Path:
    return Path(os.getenv("DATA_DIR", "./data")) / "registry" / "characters.json"


_CONFIGURED_REGISTRY_DIR = os.getenv("CHARACTER_REGISTRY_DIR", "").strip()
_REGISTRY_DIR = (
    Path(_CONFIGURED_REGISTRY_DIR).expanduser()
    if _CONFIGURED_REGISTRY_DIR
    else _default_registry_file().parent
)
_REGISTRY_FILE = _REGISTRY_DIR / "characters.json"
_INITIAL_REGISTRY_DIR = _REGISTRY_DIR
_INITIAL_REGISTRY_FILE = _REGISTRY_FILE
_REGISTRY_LOCK = threading.RLock()


def _registry_paths() -> tuple[Path, Path]:
    """每次读取当前环境，避免测试或切换数据目录后继续写入旧注册表。"""
    # 兼容显式注入路径的调用方与旧测试；未注入时跟随当前 DATA_DIR。
    if _REGISTRY_DIR != _INITIAL_REGISTRY_DIR or _REGISTRY_FILE != _INITIAL_REGISTRY_FILE:
        return Path(_REGISTRY_DIR), Path(_REGISTRY_FILE)
    configured = os.getenv("CHARACTER_REGISTRY_DIR", "").strip()
    directory = Path(configured).expanduser() if configured else _default_registry_file().parent
    return directory, directory / "characters.json"


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_id(value: object, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} 只能包含小写字母、数字、点、下划线和短横线")
    return normalized


def _default_record() -> dict[str, object]:
    return {
        "id": "default",
        "display_name": "杜佳颖",
        "data_dir": "data",
        "account_id": "du-jiaying",
        "account_label": "杜佳颖微信",
        "wechat_window_nickname": "",
        "wechat_id": "",
        "owner_remark": "",
        "binding_enabled": False,
        "identity_confirmed": False,
        "binding_version": 1,
        "binding_updated_at": _now(),
        "enabled": True,
        "persona_status": "active",
        "relationship_user_id": os.getenv("RELATIONSHIP_USER_ID", ""),
        "cold_reply_enabled": True,
        "reply_delay_max_seconds": _as_int(os.getenv("SOUL_MATE_MAX_REPLY_DELAY_SECONDS")),
        "shallow_relationship_ignore_probability": None,
        "created_at": _now(),
        "updated_at": _now(),
    }


def _normalize(record: dict[str, object]) -> dict[str, object]:
    defaults = _default_record()
    character_id = _validate_id(record.get("id") or defaults["id"], field_name="角色 ID")
    account_id = _validate_id(
        record.get("account_id") or ("du-jiaying" if character_id == "default" else character_id),
        field_name="微信账号 ID",
    )
    display_name = str(record.get("display_name") or defaults["display_name"]).strip()
    if not display_name:
        raise ValueError("角色名称不能为空")
    data_dir = str(record.get("data_dir") or ("data" if character_id == "default" else f"data/characters/{character_id}")).strip()
    if not data_dir:
        raise ValueError("角色工作空间不能为空")
    persona_status = str(
        record.get("persona_status") or ("active" if character_id == "default" else "draft")
    ).strip().lower()
    if persona_status not in {"draft", "review", "active", "paused"}:
        raise ValueError("人格状态无效")
    return {
        "id": character_id,
        "display_name": display_name,
        "data_dir": data_dir,
        "account_id": account_id,
        "account_label": str(record.get("account_label") or f"{display_name}微信").strip(),
        "wechat_window_nickname": str(record.get("wechat_window_nickname") or "").strip(),
        "wechat_id": str(record.get("wechat_id") or "").strip(),
        "owner_remark": str(record.get("owner_remark") or "").strip(),
        "binding_enabled": _as_bool(record.get("binding_enabled"), False),
        "identity_confirmed": _as_bool(record.get("identity_confirmed"), False),
        "binding_version": max(1, _as_int(record.get("binding_version")) or 1),
        "binding_updated_at": str(
            record.get("binding_updated_at")
            or record.get("updated_at")
            or defaults["binding_updated_at"]
        ),
        "enabled": _as_bool(record.get("enabled"), True),
        "persona_status": persona_status,
        "relationship_user_id": str(record.get("relationship_user_id") or "").strip(),
        "cold_reply_enabled": _as_bool(record.get("cold_reply_enabled"), True),
        "reply_delay_max_seconds": _as_int(record.get("reply_delay_max_seconds")),
        "shallow_relationship_ignore_probability": _as_float(
            record.get("shallow_relationship_ignore_probability")
        ),
        "created_at": str(record.get("created_at") or defaults["created_at"]),
        "updated_at": str(record.get("updated_at") or record.get("created_at") or defaults["updated_at"]),
    }


def _validate_unique(records: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    normalized = [_normalize(record) for record in records]
    ids = [str(record["id"]) for record in normalized]
    accounts = [str(record["account_id"]) for record in normalized]
    data_dirs = [str(record["data_dir"]).replace("\\", "/").rstrip("/").lower() for record in normalized]
    window_nicknames = [
        str(record["wechat_window_nickname"]).casefold()
        for record in normalized
        if str(record["wechat_window_nickname"]).strip()
    ]
    wechat_ids = [
        str(record["wechat_id"]).casefold()
        for record in normalized
        if str(record["wechat_id"]).strip()
    ]
    if len(ids) != len(set(ids)):
        raise ValueError("角色 ID 不能重复")
    if len(accounts) != len(set(accounts)):
        raise ValueError("微信账号 ID 不能重复绑定")
    if len(data_dirs) != len(set(data_dirs)):
        raise ValueError("不同角色不能共用同一工作空间")
    if len(window_nicknames) != len(set(window_nicknames)):
        raise ValueError("微信窗口昵称不能重复绑定")
    if len(wechat_ids) != len(set(wechat_ids)):
        raise ValueError("微信号不能重复绑定")
    for record in normalized:
        if bool(record["binding_enabled"]):
            if not str(record["wechat_window_nickname"]).strip():
                raise ValueError("启用微信绑定前必须填写窗口昵称")
            if not str(record["wechat_id"]).strip():
                raise ValueError("启用微信绑定前必须填写微信号")
            if not str(record["owner_remark"]).strip():
                raise ValueError("启用微信绑定前必须填写 Owner 的微信备注")
    return normalized


def _load() -> list[dict[str, object]]:
    _, registry_file = _registry_paths()
    if not registry_file.is_file():
        return [_default_record()]
    try:
        raw = json.loads(registry_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("角色注册表无法读取") from exc
    records = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    return _validate_unique(records or [_default_record()])


def _save(records: Iterable[dict[str, object]]) -> None:
    normalized = _validate_unique(records)
    registry_dir, registry_file = _registry_paths()
    registry_dir.mkdir(parents=True, exist_ok=True)
    temporary = registry_file.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(registry_file)


def list_characters(*, include_disabled: bool = True) -> list[dict[str, object]]:
    with _REGISTRY_LOCK:
        records = _load()
        _save(records)
        return [dict(item) for item in records if include_disabled or bool(item["enabled"])]


def get_character(character_id: str = "default") -> dict[str, object] | None:
    normalized = str(character_id or "").strip().lower()
    return next((item for item in list_characters() if item["id"] == normalized), None)


def get_character_by_account(account_id: str, *, enabled_only: bool = True) -> dict[str, object] | None:
    normalized = str(account_id or "").strip().lower()
    return next(
        (
            item
            for item in list_characters(include_disabled=not enabled_only)
            if item["account_id"] == normalized
        ),
        None,
    )


def upsert_character(record: dict[str, object]) -> dict[str, object]:
    candidate = _normalize(record)
    with _REGISTRY_LOCK:
        records = _load()
        merged: list[dict[str, object]] = []
        replaced = False
        for current in records:
            if current["id"] != candidate["id"]:
                merged.append(current)
                continue
            candidate["created_at"] = current["created_at"]
            candidate["updated_at"] = _now()
            merged.append(candidate)
            replaced = True
        if not replaced:
            candidate["updated_at"] = _now()
            merged.append(candidate)
        _save(merged)
    return dict(candidate)


_BINDING_FIELDS = (
    "wechat_window_nickname",
    "wechat_id",
    "owner_remark",
    "binding_enabled",
    "identity_confirmed",
)


def get_wechat_binding(character_id: str) -> dict[str, object] | None:
    record = get_character(character_id)
    if record is None:
        return None
    return {
        "character_id": str(record["id"]),
        "display_name": str(record["display_name"]),
        "account_id": str(record["account_id"]),
        "window_nickname": str(record["wechat_window_nickname"]),
        "wechat_id": str(record["wechat_id"]),
        "owner_remark": str(record["owner_remark"]),
        "enabled": bool(record["binding_enabled"]),
        "identity_confirmed": bool(record["identity_confirmed"]),
        "binding_version": int(record["binding_version"]),
        "updated_at": str(record["binding_updated_at"]),
    }


def upsert_wechat_binding(
    character_id: str,
    binding: dict[str, object],
    *,
    expected_version: int | None = None,
) -> dict[str, object]:
    normalized_id = _validate_id(character_id, field_name="角色 ID")
    with _REGISTRY_LOCK:
        records = _load()
        current = next((item for item in records if item["id"] == normalized_id), None)
        if current is None:
            raise KeyError("人物不存在")
        current_version = int(current["binding_version"])
        if expected_version is not None and current_version != int(expected_version):
            raise RuntimeError("微信绑定已被其他操作更新，请刷新后重试")
        candidate = dict(current)
        candidate.update(
            {
                "wechat_window_nickname": str(binding.get("window_nickname") or "").strip(),
                "wechat_id": str(binding.get("wechat_id") or "").strip(),
                "owner_remark": str(binding.get("owner_remark") or "").strip(),
                "binding_enabled": _as_bool(binding.get("enabled"), False),
                "identity_confirmed": _as_bool(binding.get("identity_confirmed"), False),
            }
        )
        changed = any(candidate[field] != current[field] for field in _BINDING_FIELDS)
        if changed:
            candidate["binding_version"] = current_version + 1
            candidate["binding_updated_at"] = _now()
            candidate["updated_at"] = candidate["binding_updated_at"]
        merged = [candidate if item["id"] == normalized_id else item for item in records]
        _save(merged)
    return get_wechat_binding(normalized_id) or {}


def list_wechat_bindings(*, approved_only: bool = False) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for record in list_characters(include_disabled=True):
        approved = (
            bool(record["enabled"])
            and str(record["persona_status"]) == "active"
            and bool(record["binding_enabled"])
            and bool(record["identity_confirmed"])
        )
        if approved_only and not approved:
            continue
        binding = get_wechat_binding(str(record["id"]))
        if binding is not None:
            result.append({**binding, "approved": approved})
    return result


def wechat_binding_configuration_version(*, approved_only: bool = True) -> str:
    payload = [
        {
            "account_id": item["account_id"],
            "character_id": item["character_id"],
            "window_nickname": item["window_nickname"],
            "wechat_id": item["wechat_id"],
            "owner_remark": item["owner_remark"],
            "binding_version": item["binding_version"],
        }
        for item in list_wechat_bindings(approved_only=approved_only)
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def get_single_character() -> dict[str, object]:
    """旧调用兼容：返回管理台默认角色，不再因存在多角色而报错。"""
    selected_id = os.getenv("ADMIN_DEFAULT_CHARACTER_ID", "default").strip().lower() or "default"
    records = list_characters(include_disabled=False)
    selected = next((item for item in records if item["id"] == selected_id), None)
    if selected is not None:
        return selected
    if records:
        return records[0]
    raise RuntimeError("没有已启用的角色")


def get_character_context(character_id: str = "default"):
    from core.character_context import CharacterContext

    record = get_character(character_id)
    return CharacterContext.from_record(record) if record else None


def get_character_context_by_account(account_id: str):
    from core.character_context import CharacterContext

    record = get_character_by_account(account_id)
    return CharacterContext.from_record(record) if record else None


def get_admin_selected_character() -> dict[str, object]:
    return get_single_character()
