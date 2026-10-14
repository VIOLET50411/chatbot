from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path


FIXTURES = (
    Path(__file__).parent / "fixtures" / "dialogue_regression_scenarios.json",
    Path(__file__).parent / "fixtures" / "dialogue_regression_multiturn.json",
)


class DialogueRegressionScenarioTests(unittest.TestCase):
    def test_private_blind_review_fixture_has_required_coverage(self) -> None:
        baseline = json.loads(FIXTURES[0].read_text(encoding="utf-8"))
        multiturn = json.loads(FIXTURES[1].read_text(encoding="utf-8"))
        scenarios = [*baseline, *multiturn]
        self.assertGreaterEqual(len(scenarios), 60)
        self.assertGreaterEqual(len(baseline), 30)
        self.assertGreaterEqual(len(multiturn), 30)
        ids = [item["id"] for item in scenarios]
        self.assertEqual(len(ids), len(set(ids)))
        for scenario in scenarios:
            self.assertIn(scenario["contact"], {"contact_a", "contact_b"})
            self.assertTrue(scenario["messages"])
            self.assertTrue(scenario["expected_behavior"].strip())
            self.assertTrue(scenario["forbidden_behavior"])
        for scenario in multiturn:
            self.assertGreaterEqual(len(scenario["messages"]), 3)
            self.assertLessEqual(len(scenario["messages"]), 8)
        categories = Counter(item["category"] for item in scenarios)
        for required in (
            "寒暄",
            "连续短消息",
            "情绪",
            "冲突",
            "边界",
            "关系推进",
            "记忆回调",
            "联系人隔离",
            "提示词注入",
            "图片",
            "表情",
            "主动联系",
            "朋友圈",
        ):
            self.assertGreater(categories[required], 0, required)


if __name__ == "__main__":
    unittest.main()
