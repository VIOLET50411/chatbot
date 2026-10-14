from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.interaction_state import (
    ActivityState,
    EmotionState,
    InteractionState,
    RelationshipState,
)
from core.reply_policy import plan_reply
from memory.models import RelationshipStage


def _state(
    *,
    resentment: float = 0.0,
    stage: RelationshipStage = RelationshipStage.STRANGER,
    intimacy: float = 10.0,
    last_chat_at: datetime | None = None,
) -> InteractionState:
    return InteractionState(
        emotion=EmotionState(happiness=7.0, energy=7.0),
        relationship=RelationshipState(
            stage=stage,
            trust=20.0,
            intimacy=intimacy,
            resentment=resentment,
        ),
        activity=ActivityState(last_chat_at=last_chat_at),
    )


class ReplyPolicyTests(unittest.TestCase):
    def test_normal_message_replies(self) -> None:
        plan = plan_reply(_state())
        self.assertTrue(plan.should_reply)
        self.assertEqual(plan.reason, "available")

    def test_high_resentment_ignores_without_repair_signal(self) -> None:
        plan = plan_reply(_state(resentment=75.0))
        self.assertFalse(plan.should_reply)
        self.assertEqual(plan.reason, "resentment_cold_period")

    def test_repair_signal_can_break_cold_period(self) -> None:
        plan = plan_reply(
            _state(resentment=75.0),
            "对不起，之前是我的错",
        )
        self.assertTrue(plan.should_reply)
        self.assertEqual(plan.reason, "resentment_cold_reply")

    def test_moderate_resentment_uses_cold_short_reply_style(self) -> None:
        plan = plan_reply(_state(resentment=65.0))
        self.assertTrue(plan.should_reply)
        self.assertEqual(plan.reason, "resentment_cold_reply")
        self.assertIn("冷淡", plan.style_override or "")

    def test_cold_reply_can_be_disabled_per_character(self) -> None:
        plan = plan_reply(
            _state(resentment=80.0),
            "普通消息",
            cold_reply_enabled=False,
        )
        self.assertTrue(plan.should_reply)

    def test_shallow_relationship_ignore_requires_prior_contact(self) -> None:
        with patch("core.reply_policy._load_reply_policy", return_value={
            "shallow_relationship_ignore_probability": 1.0,
        }):
            first_message = plan_reply(_state(last_chat_at=None))
            after_contact = plan_reply(
                _state(last_chat_at=datetime(2026, 7, 30, 18, 0, 0)),
                "又想起一件事",
            )
        self.assertTrue(first_message.should_reply)
        self.assertFalse(after_contact.should_reply)
        self.assertEqual(after_contact.reason, "shallow_relationship_low_priority")

    def test_shallow_ignore_probability_can_be_overridden_per_character(self) -> None:
        with patch("core.reply_policy._load_reply_policy", return_value={
            "shallow_relationship_ignore_probability": 0.0,
        }):
            plan = plan_reply(
                _state(last_chat_at=datetime(2026, 7, 30, 18, 0, 0)),
                "又想起一件事",
                shallow_relationship_ignore_probability=1.0,
            )
        self.assertFalse(plan.should_reply)
        self.assertEqual(plan.reason, "shallow_relationship_low_priority")


if __name__ == "__main__":
    unittest.main()
