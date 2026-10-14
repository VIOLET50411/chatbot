"""角色互动状态的纯演化规则。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from core.interaction_state import (
    ActivityState,
    EmotionState,
    InteractionState,
    RelationshipState,
)
from memory.models import RelationshipStage

Sentiment = Literal["positive", "neutral", "negative"]
ConversationDepth = Literal["shallow", "normal", "deep"]


@dataclass(frozen=True, slots=True)
class ConversationSignal:
    sentiment: Sentiment = "neutral"
    depth: ConversationDepth = "shallow"
    intimacy_delta: float = 0.0
    intimacy_context: str = "normal"
    reason: str = "analysis_unavailable"


@dataclass(frozen=True, slots=True)
class StageRule:
    rule_id: str
    from_stage: RelationshipStage
    to_stage: RelationshipStage
    condition_mode: Literal["all", "any"] = "all"
    min_trust: float | None = None
    max_trust: float | None = None
    min_intimacy: float | None = None
    max_intimacy: float | None = None
    min_resentment: float | None = None
    max_resentment: float | None = None


@dataclass(frozen=True, slots=True)
class StateChange:
    state: InteractionState
    reason: str
    inputs: dict[str, object]


DEFAULT_STAGE_RULES: tuple[StageRule, ...] = (
    StageRule("downgrade_lover", RelationshipStage.LOVER, RelationshipStage.AMBIGUOUS, "any", max_trust=70, min_resentment=60),
    StageRule("downgrade_ambiguous", RelationshipStage.AMBIGUOUS, RelationshipStage.CLOSE, "any", max_intimacy=60, min_resentment=65),
    StageRule("downgrade_close", RelationshipStage.CLOSE, RelationshipStage.FAMILIAR, "any", max_trust=50, min_resentment=70),
    StageRule("downgrade_familiar", RelationshipStage.FAMILIAR, RelationshipStage.PROBING, "any", max_trust=30, min_resentment=75),
    StageRule("upgrade_stranger", RelationshipStage.STRANGER, RelationshipStage.PROBING, min_trust=30, min_intimacy=20),
    StageRule("upgrade_probing", RelationshipStage.PROBING, RelationshipStage.FAMILIAR, min_trust=50, min_intimacy=40),
    StageRule("upgrade_familiar", RelationshipStage.FAMILIAR, RelationshipStage.CLOSE, min_trust=70, min_intimacy=60),
    StageRule("upgrade_close", RelationshipStage.CLOSE, RelationshipStage.AMBIGUOUS, min_trust=78, min_intimacy=75, max_resentment=20),
    StageRule("upgrade_ambiguous", RelationshipStage.AMBIGUOUS, RelationshipStage.LOVER, min_trust=88, min_intimacy=85, max_resentment=15),
)

_TIME_EFFECTS: tuple[tuple[int, int, dict[str, float]], ...] = (
    (7, 9, {"energy": 0.5, "happiness": 0.3}),
    (10, 12, {"energy": 0.3}),
    (13, 15, {"energy": -0.8, "irritability": 0.2}),
    (18, 20, {"happiness": 0.2}),
    (20, 23, {"loneliness": 0.3}),
    (23, 26, {"energy": -1.5, "irritability": 0.3}),
)

_BASE_RELATIONSHIP_EFFECTS: dict[tuple[Sentiment, ConversationDepth], dict[str, float]] = {
    ("positive", "deep"): {"trust": 1.2, "intimacy": 0.8, "resentment": -1.0},
    ("positive", "normal"): {"trust": 0.6, "intimacy": 0.4, "resentment": -0.5},
    ("positive", "shallow"): {"trust": 0.2, "intimacy": 0.1},
    ("neutral", "normal"): {"trust": 0.1},
    ("negative", "normal"): {"trust": -0.5, "resentment": 2.0},
    ("negative", "deep"): {"trust": -1.0, "resentment": 4.0},
}


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _matches(rule: StageRule, relationship: RelationshipState) -> bool:
    checks = (
        (rule.min_trust, relationship.trust, lambda required, actual: actual >= required),
        (rule.max_trust, relationship.trust, lambda required, actual: actual <= required),
        (rule.min_intimacy, relationship.intimacy, lambda required, actual: actual >= required),
        (rule.max_intimacy, relationship.intimacy, lambda required, actual: actual <= required),
        (rule.min_resentment, relationship.resentment, lambda required, actual: actual >= required),
        (rule.max_resentment, relationship.resentment, lambda required, actual: actual <= required),
    )
    matched = [
        predicate(required, actual)
        for required, actual, predicate in checks
        if required is not None
    ]
    if not matched:
        return False
    return any(matched) if rule.condition_mode == "any" else all(matched)


def _apply_stage_policy(
    relationship: RelationshipState,
    rules: tuple[StageRule, ...],
) -> tuple[RelationshipState, str | None]:
    for rule in rules:
        if relationship.stage == rule.from_stage and _matches(rule, relationship):
            return replace(relationship, stage=rule.to_stage), rule.rule_id
    return relationship, None


def tick_state(
    state: InteractionState,
    *,
    occurred_at: datetime,
    rules: tuple[StageRule, ...] = DEFAULT_STAGE_RULES,
) -> StateChange:
    emotion_values = {
        "happiness": state.emotion.happiness,
        "energy": state.emotion.energy,
        "irritability": state.emotion.irritability,
        "affection": state.emotion.affection,
        "loneliness": state.emotion.loneliness,
    }
    for start_hour, end_hour, effects in _TIME_EFFECTS:
        end = end_hour % 24
        in_range = start_hour <= occurred_at.hour < end_hour if end_hour <= 24 else occurred_at.hour >= start_hour or occurred_at.hour < end
        if in_range:
            for field_name, delta in effects.items():
                emotion_values[field_name] = _clamp(emotion_values[field_name] + delta * 0.25, 0.0, 10.0)
            break

    if state.activity.last_chat_at:
        hours_since_chat = max(0.0, (occurred_at - state.activity.last_chat_at).total_seconds() / 3600)
        if hours_since_chat > 24:
            emotion_values["loneliness"] = _clamp(emotion_values["loneliness"] + 0.3, 0.0, 10.0)
            emotion_values["affection"] = _clamp(emotion_values["affection"] - 0.05, 0.0, 10.0)
        elif hours_since_chat > 6:
            emotion_values["loneliness"] = _clamp(emotion_values["loneliness"] + 0.15, 0.0, 10.0)
        elif hours_since_chat < 1:
            emotion_values["loneliness"] = _clamp(emotion_values["loneliness"] - 0.2, 0.0, 10.0)
    else:
        hours_since_chat = None

    relationship = replace(state.relationship, resentment=_clamp(state.relationship.resentment - 0.05, 0.0, 100.0))
    if 7 <= occurred_at.hour < 9 and relationship.intimacy_context == "late_night":
        relationship = replace(
            relationship,
            intimacy=_clamp(relationship.intimacy - 2.5, 0.0, 100.0),
            intimacy_context="normal",
        )
    relationship, rule_id = _apply_stage_policy(relationship, rules)
    next_state = replace(
        state,
        emotion=EmotionState(**emotion_values),
        relationship=relationship,
        activity=replace(state.activity, last_tick_at=occurred_at),
    )
    return StateChange(
        state=next_state,
        reason="periodic_tick",
        inputs={"occurred_at": occurred_at.isoformat(), "hours_since_chat": hours_since_chat, "stage_rule": rule_id},
    )


def apply_conversation(
    state: InteractionState,
    signal: ConversationSignal,
    *,
    occurred_at: datetime,
    rules: tuple[StageRule, ...] = DEFAULT_STAGE_RULES,
) -> StateChange:
    emotion = state.emotion
    relationship = state.relationship
    if signal.sentiment == "positive":
        emotion = replace(
            emotion,
            happiness=_clamp(emotion.happiness + 0.55, 0.0, 10.0),
            loneliness=_clamp(emotion.loneliness - 0.5, 0.0, 10.0),
            irritability=_clamp(emotion.irritability - 0.3, 0.0, 10.0),
        )
    elif signal.sentiment == "negative":
        emotion = replace(emotion, irritability=_clamp(emotion.irritability + 1.0, 0.0, 10.0))

    relationship_values = {
        "trust": relationship.trust,
        "intimacy": relationship.intimacy,
        "resentment": relationship.resentment,
    }
    for field_name, delta in _BASE_RELATIONSHIP_EFFECTS.get((signal.sentiment, signal.depth), {}).items():
        relationship_values[field_name] = _clamp(relationship_values[field_name] + delta, 0.0, 100.0)
    relationship_values["intimacy"] = _clamp(
        relationship_values["intimacy"] + _clamp(signal.intimacy_delta, -3.0, 3.0),
        0.0,
        100.0,
    )
    relationship = replace(
        relationship,
        **relationship_values,
        intimacy_context=signal.intimacy_context,
    )
    relationship, rule_id = _apply_stage_policy(relationship, rules)
    next_state = replace(
        state,
        emotion=emotion,
        relationship=relationship,
        activity=replace(state.activity, last_chat_at=occurred_at),
    )
    return StateChange(
        state=next_state,
        reason="conversation_evaluated",
        inputs={
            "occurred_at": occurred_at.isoformat(),
            "sentiment": signal.sentiment,
            "depth": signal.depth,
            "intimacy_delta": _clamp(signal.intimacy_delta, -3.0, 3.0),
            "intimacy_context": signal.intimacy_context,
            "analysis_reason": signal.reason,
            "stage_rule": rule_id,
        },
    )
