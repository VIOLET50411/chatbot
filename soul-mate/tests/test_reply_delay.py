from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.reply_delay import calculate_reply_delay


class ReplyDelayTests(unittest.TestCase):
    def test_character_cap_wins_over_environment(self) -> None:
        state = SimpleNamespace(intimacy=0.0, current_activity="")
        context = SimpleNamespace(reply_delay_max_seconds=120)
        with patch.dict(os.environ, {"SOUL_MATE_MAX_REPLY_DELAY_SECONDS": "0"}):
            with patch("core.reply_delay.random.uniform", return_value=7200.0):
                delay = calculate_reply_delay(state, context=context)
        self.assertLessEqual(delay, 120)
        self.assertGreaterEqual(delay, 0)

    def test_zero_cap_returns_immediate(self) -> None:
        state = SimpleNamespace(intimacy=0.0, current_activity="")
        context = SimpleNamespace(reply_delay_max_seconds=0)
        with patch("core.reply_delay.random.uniform", return_value=7200.0):
            delay = calculate_reply_delay(state, context=context)
        self.assertEqual(delay, 0)

    def test_enabled_delay_is_always_between_three_and_ninety_seconds(self) -> None:
        state = SimpleNamespace(intimacy=0.0, current_activity="考试中")
        context = SimpleNamespace(reply_delay_max_seconds=120)
        with patch("core.reply_delay.random.uniform", return_value=7200.0):
            self.assertEqual(calculate_reply_delay(state, context=context), 90)
        state = SimpleNamespace(intimacy=100.0, current_activity="闲着")
        with patch("core.reply_delay.random.uniform", return_value=0.0):
            self.assertEqual(calculate_reply_delay(state, context=context), 3)

    def test_availability_ping_is_capped_at_eighteen_seconds(self) -> None:
        state = SimpleNamespace(intimacy=0.0, current_activity="考试中")
        context = SimpleNamespace(reply_delay_max_seconds=120)
        with patch("core.reply_delay.random.uniform", return_value=7200.0):
            self.assertEqual(calculate_reply_delay(state, "在吗？", context=context), 18)

    def test_other_very_short_message_is_capped_at_thirty_seconds(self) -> None:
        state = SimpleNamespace(intimacy=0.0, current_activity="考试中")
        context = SimpleNamespace(reply_delay_max_seconds=120)
        with patch("core.reply_delay.random.uniform", return_value=7200.0):
            self.assertEqual(calculate_reply_delay(state, "怎么了", context=context), 30)


if __name__ == "__main__":
    unittest.main()
