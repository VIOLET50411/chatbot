"""角色互动状态的运行时服务。"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from core.character_context import CharacterContext
from core.interaction_reducer import (
    ConversationSignal,
    StateChange,
    apply_conversation,
    tick_state,
)
from core.interaction_state import InteractionState, to_legacy_inner_state
from memory.interaction_state_store import (
    StateVersionConflict,
    commit_interaction_transition,
    load_interaction_state,
)
from memory.models import InnerState

_MAX_COMMIT_RETRIES = 3


async def get_interaction_state(context: CharacterContext) -> InteractionState:
    return await asyncio.to_thread(load_interaction_state, context)


async def get_legacy_inner_state(context: CharacterContext) -> InnerState:
    state = await get_interaction_state(context)
    return to_legacy_inner_state(state)


async def _evolve(
    context: CharacterContext,
    evolve: Callable[[InteractionState], StateChange],
) -> StateChange:
    conflict: StateVersionConflict | None = None
    for _ in range(_MAX_COMMIT_RETRIES):
        current = await get_interaction_state(context)
        change = evolve(current)
        try:
            committed = await asyncio.to_thread(
                commit_interaction_transition,
                context,
                expected=current,
                next_state=change.state,
                reason=change.reason,
                inputs=change.inputs,
            )
            return replace(change, state=committed)
        except StateVersionConflict as exc:
            conflict = exc
    raise conflict or StateVersionConflict("互动状态提交冲突")


async def apply_periodic_tick(
    context: CharacterContext,
    *,
    occurred_at: datetime | None = None,
) -> StateChange:
    now = occurred_at or datetime.now()
    return await _evolve(context, lambda state: tick_state(state, occurred_at=now))


async def apply_conversation_signal(
    context: CharacterContext,
    signal: ConversationSignal,
    *,
    occurred_at: datetime | None = None,
) -> StateChange:
    now = occurred_at or datetime.now()
    return await _evolve(
        context,
        lambda state: apply_conversation(state, signal, occurred_at=now),
    )


async def apply_conversation_signal_at_version(
    context: CharacterContext,
    signal: ConversationSignal,
    *,
    expected_version: int,
    occurred_at: datetime | None = None,
) -> StateChange:
    """在审核时锁定互动状态基线，拒绝在版本漂移后重新计算并提交。

    普通会话允许在并发冲突后基于最新状态重算；审核提案不能这样做，
    否则“已审核的事实 + 既定状态版本”的因果边界会被悄悄打破。
    """
    now = occurred_at or datetime.now()
    current = await get_interaction_state(context)
    if current.version != expected_version:
        raise StateVersionConflict(
            f"角色 {context.character_id} 的互动状态已从版本 {expected_version} 变化为 {current.version}"
        )
    change = apply_conversation(current, signal, occurred_at=now)
    committed = await asyncio.to_thread(
        commit_interaction_transition,
        context,
        expected=current,
        next_state=change.state,
        reason=change.reason,
        inputs=change.inputs,
    )
    return replace(change, state=committed)
