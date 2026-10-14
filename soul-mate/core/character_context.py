"""角色运行上下文。

一个回调、定时任务或后台操作必须先绑定所属角色，所有角色业务数据才可被读取。
这避免了通过全局“当前激活角色”切换工作空间导致的并发串数据。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping


class CharacterContextNotBoundError(RuntimeError):
    """当前执行路径没有明确角色归属。"""


def _project_root() -> Path:
    configured = os.getenv("SOUL_MATE_ROOT", "")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def resolve_workspace_path(value: str | Path) -> Path:
    """解析角色工作空间，并禁止注册表指向项目目录之外。"""
    root = _project_root()
    candidate = Path(value).expanduser()
    candidate = candidate if candidate.is_absolute() else root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("角色工作空间必须位于项目目录内") from exc
    return resolved


def _as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: object, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class CharacterContext:
    character_id: str
    display_name: str
    workspace_dir: Path
    relationship_user_id: str = ""
    cold_reply_enabled: bool = True
    reply_delay_max_seconds: int | None = None
    shallow_relationship_ignore_probability: float | None = None

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "CharacterContext":
        return cls(
            character_id=str(record["id"]),
            display_name=str(record.get("display_name", "未命名角色")),
            workspace_dir=resolve_workspace_path(str(record["data_dir"])),
            relationship_user_id=str(record.get("relationship_user_id", "")),
            cold_reply_enabled=_as_bool(record.get("cold_reply_enabled", True)),
            reply_delay_max_seconds=_as_int(
                record.get("reply_delay_max_seconds"),
            ),
            shallow_relationship_ignore_probability=_as_float(
                record.get("shallow_relationship_ignore_probability"),
            ),
        )


_current_context: ContextVar[CharacterContext | None] = ContextVar(
    "soul_mate_character_context",
    default=None,
)


def get_character_context() -> CharacterContext | None:
    return _current_context.get()


def require_character_context() -> CharacterContext:
    context = get_character_context()
    if context is None:
        raise CharacterContextNotBoundError(
            "角色业务调用必须在已绑定的 CharacterContext 中执行"
        )
    return context


def bind_character_context(context: CharacterContext) -> Token[CharacterContext | None]:
    return _current_context.set(context)


def reset_character_context(token: Token[CharacterContext | None]) -> None:
    _current_context.reset(token)


@contextmanager
def character_scope(context: CharacterContext) -> Iterator[CharacterContext]:
    token = bind_character_context(context)
    try:
        yield context
    finally:
        reset_character_context(token)
