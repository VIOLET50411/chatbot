"""关系阶段的展示配置与停用入口。"""
from __future__ import annotations

from memory.models import InnerState, RelationshipStage


STAGE_BEHAVIOR = {
    RelationshipStage.STRANGER: {
        "reply_style": "礼貌保持距离，不聊私人话题",
        "proactive_freq": "几乎不主动",
        "nickname": ["你", "{name}"],
        "shows_anger": False,
        "shows_affection": False,
    },
    RelationshipStage.PROBING: {
        "reply_style": "自然，开始有个人观点，偶尔吐槽",
        "proactive_freq": "偶尔，每天0-1次",
        "nickname": ["{name}", "你"],
        "shows_anger": "轻微不满",
        "shows_affection": False,
    },
    RelationshipStage.FAMILIAR: {
        "reply_style": "放松，有自己立场，会怼，会关心",
        "proactive_freq": "经常，每天1-3次",
        "nickname": ["{nickname}", "{name}"],
        "shows_anger": True,
        "shows_affection": "偶尔",
    },
    RelationshipStage.CLOSE: {
        "reply_style": "完全自然，深入话题，情绪完整表达",
        "proactive_freq": "频繁，随时想聊就聊",
        "nickname": ["各种昵称"],
        "shows_anger": True,
        "shows_affection": True,
    },
    RelationshipStage.AMBIGUOUS: {
        "reply_style": "亲密，试探边界，在意细节，可能吃醋",
        "proactive_freq": "很频繁，找借口聊天",
        "nickname": ["亲昵昵称"],
        "shows_anger": "吃醋型",
        "shows_affection": "经常",
    },
    RelationshipStage.LOVER: {
        "reply_style": "完全亲密，无保留，深度关心，也有各自空间",
        "proactive_freq": "自然频繁",
        "nickname": ["宝", "亲爱的", "自选"],
        "shows_anger": True,
        "shows_affection": True,
    },
}


def _legacy_error() -> RuntimeError:
    return RuntimeError("旧关系状态机已停用，请通过 interaction_service 提交结构化互动信号")


async def eval_relationship_impact(
    user_msg: str,
    reply_text: str,
    persona: dict,
    state: InnerState,
) -> dict:
    raise _legacy_error()


def apply_conversation_effects(
    state: InnerState,
    sentiment: str,
    depth: str,
) -> InnerState:
    raise _legacy_error()


def check_stage_transition(state: InnerState) -> RelationshipStage | None:
    raise _legacy_error()
