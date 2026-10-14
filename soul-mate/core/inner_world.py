"""互动状态的兼容入口。

旧调用方只能读取投影视图或提交明确事件，不能再直接修改旧状态表。
"""
from memory.models import InnerState

from core.character_context import CharacterContext, character_scope
from core.interaction_analysis import analyze_conversation
from core.interaction_state import to_legacy_inner_state
from memory.interaction_service import (
    apply_conversation_signal,
    apply_periodic_tick,
    get_interaction_state,
)


async def tick(context: CharacterContext) -> None:
    """在明确角色作用域内提交一次可审计的时间事件。"""
    with character_scope(context):
        await apply_periodic_tick(context)


async def update_after_conversation(
    context: CharacterContext,
    user_message: str,
    reply_text: str,
    persona: dict | None,
) -> InnerState:
    """基于已发布档案分析本轮互动，并提交状态演化结果。"""
    with character_scope(context):
        current_state = await get_interaction_state(context)
        signal = await analyze_conversation(
            user_message=user_message,
            reply_text=reply_text,
            profile=persona or {},
            state=current_state,
        )
        change = await apply_conversation_signal(context, signal)
        return to_legacy_inner_state(change.state)



def describe_mood(state: InnerState) -> str:
    """把情绪数值转成可注入 prompt 的文字描述"""
    parts = []
    if state.happiness > 7:
        parts.append("心情不错")
    elif state.happiness < 4:
        parts.append("心情不太好")
    if state.energy < 3:
        parts.append("有点累")
    if state.irritability > 6:
        parts.append("有点烦")
    if state.loneliness > 7:
        parts.append("有点孤独")
    return "、".join(parts) if parts else "状态平常"
