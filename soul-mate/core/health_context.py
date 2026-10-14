"""用户健康数据的独立存储与角色侧只读观察。

健康数据属于设备佩戴者（例如杨秉宪），不属于任何角色的内心状态。
角色仅能通过其 `relationship_user_id` 读取关联用户的数据摘要，用于关心或判断是否
适合打扰，不能将其写入角色工作空间或作为人格结论。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.character_context import (
    CharacterContext,
    get_character_context,
)


def _project_root() -> Path:
    configured = os.getenv("SOUL_MATE_ROOT", "")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _health_root() -> Path:
    configured = os.getenv("HEALTH_DATA_DIR", "data/users")
    candidate = Path(configured).expanduser()
    candidate = candidate if candidate.is_absolute() else _project_root() / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(_project_root())
    except ValueError as exc:
        raise ValueError("健康数据目录必须位于项目目录内") from exc
    return resolved


def _default_subject_user_id() -> str:
    return (
        os.getenv("HEALTH_SUBJECT_USER_ID", "").strip()
        or os.getenv("TARGET_WXID", "").strip()
    )


def _resolve_subject_user_id(
    user_id: str | None = None,
    context: CharacterContext | None = None,
) -> str:
    explicit = (user_id or "").strip()
    if explicit:
        return explicit

    if context is not None:
        related = context.relationship_user_id.strip()
        if related:
            return related

    bound_context = get_character_context()
    if bound_context is not None:
        related = bound_context.relationship_user_id.strip()
        if related:
            return related

    fallback = _default_subject_user_id()
    if fallback:
        return fallback

    raise ValueError("健康数据必须绑定明确的用户主体")


def _health_files(
    user_id: str | None = None,
    context: CharacterContext | None = None,
) -> tuple[Path, Path]:
    subject_user_id = _resolve_subject_user_id(user_id, context)
    # 文件路径只保存稳定散列，避免把微信 ID 暴露为目录名。
    subject_key = hashlib.sha256(subject_user_id.encode("utf-8")).hexdigest()[:32]
    subject_dir = _health_root() / subject_key
    return subject_dir / "health_live.json", subject_dir / "health_history.jsonl"


def _migrate_legacy_health(
    context: CharacterContext,
    live_file: Path,
    history_file: Path,
) -> None:
    """首次读取时保留旧角色工作空间中的健康记录，避免升级造成数据消失。"""
    legacy_live = context.workspace_dir / "health_live.json"
    legacy_history = context.workspace_dir / "health_history.jsonl"

    if legacy_live.is_file() and not live_file.exists():
        live_file.parent.mkdir(parents=True, exist_ok=True)
        live_file.write_bytes(legacy_live.read_bytes())

    if not legacy_history.is_file():
        return

    legacy_lines = [
        line for line in legacy_history.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not legacy_lines:
        return

    history_file.parent.mkdir(parents=True, exist_ok=True)
    current_lines = (
        set(history_file.read_text(encoding="utf-8").splitlines())
        if history_file.exists()
        else set()
    )
    missing_lines = [line for line in legacy_lines if line not in current_lines]
    if missing_lines:
        with history_file.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(missing_lines) + "\n")


def _normalize_ts(data: dict) -> dict:
    ts = data.get("ts")
    if ts is None:
        return {**data, "ts": int(time.time())}
    if isinstance(ts, str):
        try:
            return {**data, "ts": int(datetime.fromisoformat(ts).timestamp())}
        except ValueError:
            pass
    return data


def save_health(data: dict, *, user_id: str | None = None) -> None:
    """写入某个设备佩戴者的原始健康数据，不要求角色上下文。"""
    live_file, history_file = _health_files(user_id=user_id)
    normalized = _normalize_ts(data)
    live_file.parent.mkdir(parents=True, exist_ok=True)
    live_file.write_text(json.dumps(normalized, ensure_ascii=False), encoding="utf-8")
    with history_file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(normalized, ensure_ascii=False) + "\n")


def load_health(context: CharacterContext | None = None) -> Optional[dict]:
    """读取角色关联用户的健康数据；未建立关系时不暴露任何记录。"""
    relationship_context = context or get_character_context()
    if relationship_context is None or not relationship_context.relationship_user_id.strip():
        return None

    live_file, history_file = _health_files(context=relationship_context)
    _migrate_legacy_health(relationship_context, live_file, history_file)
    try:
        return json.loads(live_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_health_history(
    n: int = 30,
    context: CharacterContext | None = None,
) -> list[dict]:
    relationship_context = context or get_character_context()
    if relationship_context is None or not relationship_context.relationship_user_id.strip():
        return []

    live_file, history_file = _health_files(context=relationship_context)
    _migrate_legacy_health(relationship_context, live_file, history_file)
    try:
        lines = history_file.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(line) for line in lines[-n:] if line.strip()]
    except (OSError, ValueError):
        return []


def build_health_context(data: dict) -> str:
    def _extract(raw, *keys):
        value = raw
        for key in keys:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value

    bpm_avg = data.get("bpm_avg") or _extract(data, "heart", "longValues", "bpm_avg")
    bpm_max = data.get("bpm_max") or _extract(data, "hr_max", "longValues", "bpm_max")
    steps = data.get("steps") or _extract(data, "steps", "longValues", "count_total")
    calories = data.get("calories") or _extract(data, "calories", "doubleValues", "energy_total")
    sleep_ms = data.get("sleep_ms") or _extract(data, "sleep", "longValues", "duration")

    parts = []
    if bpm_avg is not None:
        parts.append(f"心率均值 {int(bpm_avg)} bpm")
    if bpm_max is not None:
        parts.append(f"峰值 {int(bpm_max)} bpm")
    if steps is not None:
        parts.append(f"今日步数 {int(steps)} 步")
    if calories is not None:
        parts.append(f"活动消耗 {float(calories):.0f} kcal")
    if sleep_ms is not None:
        parts.append(f"昨日睡眠 {float(sleep_ms) / 3_600_000:.1f}h")

    if not parts:
        return ""
    return "【对方可穿戴设备健康数据，仅作关心参考】" + "，".join(parts)

