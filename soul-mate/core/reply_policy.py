"""入站消息的确定性回复呈现策略。"""
from __future__ import annotations

import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from core.interaction_state import InteractionState
from memory.models import RelationshipStage


_REPAIR_SIGNALS = (
    "对不起",
    "抱歉",
    "我错了",
    "别不理我",
    "道歉",
    "是我的错",
    "我会改",
)


@dataclass(frozen=True, slots=True)
class ReplyPlan:
    should_reply: bool
    reason: str
    delay_type: str
    style_override: str | None = None
    schedule_later: int | None = None

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


def plan_reply(
    state: InteractionState,
    message: str = "",
    *,
    cold_reply_enabled: bool = True,
    shallow_relationship_ignore_probability: float | None = None,
) -> ReplyPlan:
    policy = _load_reply_policy()
    emotion = state.emotion

    if cold_reply_enabled and state.relationship.resentment >= _number(
        policy, "resentment_ignore_threshold", 70.0
    ):
        if not _has_repair_signal(message):
            return ReplyPlan(
                should_reply=False,
                reason="resentment_cold_period",
                delay_type="none",
            )

    if cold_reply_enabled and state.relationship.resentment >= _number(
        policy, "resentment_cold_threshold", 60.0
    ):
        return ReplyPlan(
            should_reply=True,
            reason="resentment_cold_reply",
            delay_type="slow",
            style_override="冷淡、简短、不主动延续话题，但仍有基本礼貌",
        )

    if (
        cold_reply_enabled
        and state.activity.last_chat_at is not None
        and state.relationship.stage in {
            RelationshipStage.STRANGER,
            RelationshipStage.PROBING,
        }
        and state.relationship.intimacy < 25
        and _should_ignore_by_message(
            message,
            (
                shallow_relationship_ignore_probability
                if shallow_relationship_ignore_probability is not None
                else _number(policy, "shallow_relationship_ignore_probability", 0.03)
            ),
        )
    ):
        return ReplyPlan(
            should_reply=False,
            reason="shallow_relationship_low_priority",
            delay_type="none",
        )

    if emotion.energy < 2.0:
        return ReplyPlan(
            should_reply=True,
            reason="low_energy",
            delay_type="slow",
            style_override="很累，极简短，可以只回一个表情",
        )
    if emotion.irritability > 7.0:
        return ReplyPlan(
            should_reply=True,
            reason="high_irritability",
            delay_type="slow",
            style_override="语气克制、简短，不升级冲突",
        )
    if emotion.energy > 7.0 and emotion.happiness > 6.0:
        return ReplyPlan(True, "available", "instant")
    if emotion.energy < 4.0 or emotion.irritability > 5.0:
        return ReplyPlan(True, "reduced_availability", "slow")
    return ReplyPlan(True, "available", "normal")


def _load_reply_policy() -> dict:
    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    try:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    policy = cfg.get("reply_policy")
    return policy if isinstance(policy, dict) else {}


def _number(policy: dict, key: str, default: float) -> float:
    try:
        return float(policy.get(key, default))
    except (TypeError, ValueError):
        return default


def _has_repair_signal(message: str) -> bool:
    return any(signal in message for signal in _REPAIR_SIGNALS)


def _should_ignore_by_message(message: str, probability: float) -> bool:
    if probability <= 0:
        return False
    if probability >= 1:
        return True
    digest = zlib.crc32(message.encode("utf-8")) & 0xFFFFFFFF
    return digest % 10000 < int(probability * 10000)
