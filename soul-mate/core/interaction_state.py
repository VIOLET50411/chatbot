"""角色互动状态的不可变领域模型。

该模型把情绪、关系和活动元数据分开，避免任意模块直接修改一组混合字段。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from memory.models import InnerState, RelationshipStage


def _bounded(value: object, lower: float, upper: float, fallback: float) -> float:
    try:
        return max(lower, min(upper, float(value)))
    except (TypeError, ValueError):
        return fallback


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _parse_stage(value: object) -> RelationshipStage:
    try:
        return RelationshipStage(str(value))
    except ValueError:
        return RelationshipStage.STRANGER


@dataclass(frozen=True, slots=True)
class EmotionState:
    happiness: float = 6.0
    energy: float = 7.0
    irritability: float = 2.0
    affection: float = 5.0
    loneliness: float = 3.0


@dataclass(frozen=True, slots=True)
class RelationshipState:
    stage: RelationshipStage = RelationshipStage.STRANGER
    trust: float = 20.0
    intimacy: float = 10.0
    resentment: float = 0.0
    intimacy_context: str = "normal"


@dataclass(frozen=True, slots=True)
class ActivityState:
    current_activity: str = "没什么特别的"
    last_chat_at: datetime | None = None
    last_tick_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class InteractionState:
    emotion: EmotionState = field(default_factory=EmotionState)
    relationship: RelationshipState = field(default_factory=RelationshipState)
    activity: ActivityState = field(default_factory=ActivityState)
    version: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StateTransition:
    transition_id: int
    reason: str
    base_version: int
    result_version: int
    inputs: Mapping[str, Any]
    before: InteractionState
    after: InteractionState
    created_at: datetime


def state_to_payload(state: InteractionState) -> dict[str, object]:
    return {
        "emotion": {
            "happiness": state.emotion.happiness,
            "energy": state.emotion.energy,
            "irritability": state.emotion.irritability,
            "affection": state.emotion.affection,
            "loneliness": state.emotion.loneliness,
        },
        "relationship": {
            "stage": state.relationship.stage.value,
            "trust": state.relationship.trust,
            "intimacy": state.relationship.intimacy,
            "resentment": state.relationship.resentment,
            "intimacy_context": state.relationship.intimacy_context,
        },
        "activity": {
            "current_activity": state.activity.current_activity,
            "last_chat_at": state.activity.last_chat_at.isoformat()
            if state.activity.last_chat_at
            else None,
            "last_tick_at": state.activity.last_tick_at.isoformat()
            if state.activity.last_tick_at
            else None,
        },
    }


def state_from_payload(
    payload: Mapping[str, object],
    *,
    version: int = 0,
    updated_at: datetime | None = None,
) -> InteractionState:
    emotion = payload.get("emotion")
    relationship = payload.get("relationship")
    activity = payload.get("activity")
    emotion_data = emotion if isinstance(emotion, Mapping) else {}
    relationship_data = relationship if isinstance(relationship, Mapping) else {}
    activity_data = activity if isinstance(activity, Mapping) else {}

    return InteractionState(
        emotion=EmotionState(
            happiness=_bounded(emotion_data.get("happiness"), 0.0, 10.0, 6.0),
            energy=_bounded(emotion_data.get("energy"), 0.0, 10.0, 7.0),
            irritability=_bounded(emotion_data.get("irritability"), 0.0, 10.0, 2.0),
            affection=_bounded(emotion_data.get("affection"), 0.0, 10.0, 5.0),
            loneliness=_bounded(emotion_data.get("loneliness"), 0.0, 10.0, 3.0),
        ),
        relationship=RelationshipState(
            stage=_parse_stage(relationship_data.get("stage")),
            trust=_bounded(relationship_data.get("trust"), 0.0, 100.0, 20.0),
            intimacy=_bounded(relationship_data.get("intimacy"), 0.0, 100.0, 10.0),
            resentment=_bounded(relationship_data.get("resentment"), 0.0, 100.0, 0.0),
            intimacy_context=str(relationship_data.get("intimacy_context") or "normal"),
        ),
        activity=ActivityState(
            current_activity=str(activity_data.get("current_activity") or "没什么特别的"),
            last_chat_at=_parse_datetime(activity_data.get("last_chat_at")),
            last_tick_at=_parse_datetime(activity_data.get("last_tick_at")),
        ),
        version=max(0, int(version)),
        updated_at=updated_at,
    )


def from_legacy_inner_state(legacy: InnerState) -> InteractionState:
    return InteractionState(
        emotion=EmotionState(
            happiness=_bounded(legacy.happiness, 0.0, 10.0, 6.0),
            energy=_bounded(legacy.energy, 0.0, 10.0, 7.0),
            irritability=_bounded(legacy.irritability, 0.0, 10.0, 2.0),
            affection=_bounded(legacy.affection, 0.0, 10.0, 5.0),
            loneliness=_bounded(legacy.loneliness, 0.0, 10.0, 3.0),
        ),
        relationship=RelationshipState(
            stage=_parse_stage(legacy.stage.value),
            trust=_bounded(legacy.trust, 0.0, 100.0, 20.0),
            intimacy=_bounded(legacy.intimacy, 0.0, 100.0, 10.0),
            resentment=_bounded(legacy.resentment, 0.0, 100.0, 0.0),
            intimacy_context=str(legacy.intimacy_context or "normal"),
        ),
        activity=ActivityState(
            current_activity=legacy.current_activity or "没什么特别的",
            last_chat_at=legacy.last_chat_time,
            last_tick_at=legacy.last_tick_time,
        ),
    )


def to_legacy_inner_state(state: InteractionState) -> InnerState:
    return InnerState(
        happiness=state.emotion.happiness,
        energy=state.emotion.energy,
        irritability=state.emotion.irritability,
        affection=state.emotion.affection,
        loneliness=state.emotion.loneliness,
        stage=state.relationship.stage,
        trust=state.relationship.trust,
        intimacy=state.relationship.intimacy,
        resentment=state.relationship.resentment,
        current_activity=state.activity.current_activity,
        intimacy_context=state.relationship.intimacy_context,
        last_chat_time=state.activity.last_chat_at,
        last_tick_time=state.activity.last_tick_at,
    )
