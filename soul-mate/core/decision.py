"""已停用的旧回复决策入口。"""
from __future__ import annotations

from memory.models import InnerState


def _legacy_error() -> RuntimeError:
    return RuntimeError("旧随机回复决策已停用，请使用 reply_policy.plan_reply")


async def decide_reply(message: str, state: InnerState) -> dict:
    raise _legacy_error()


def get_reply_delay_seconds(delay_type: str, message_length: int = 20) -> float:
    raise _legacy_error()
