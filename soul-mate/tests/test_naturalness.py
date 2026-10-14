from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.naturalness import find_reply_violations


class NaturalnessTests(unittest.TestCase):
    def test_ambiguous_study_duration_question_is_rejected(self) -> None:
        violations = find_reply_violations(
            user_message="你看我最近得做这么多题 考研好累",
            reply_text="考研确实挺费精力的\n你复习多久了",
            decision={},
        )
        self.assertTrue(any("时间范围" in item for item in violations))

    def test_follow_up_clarification_must_answer_previous_question_scope(self) -> None:
        wrong = find_reply_violations(
            user_message="你是说今天吗",
            reply_text="今天吗\n有点懵 脑子还在背书模式里",
            decision={},
        )
        right = find_reply_violations(
            user_message="你是说今天吗",
            reply_text="对 我问今天已经学多久了",
            decision={},
        )
        self.assertTrue(any("上一问范围" in item for item in wrong))
        self.assertFalse(any("上一问范围" in item for item in right))

    def test_laughter_is_blocked_in_serious_context(self) -> None:
        result = find_reply_violations(
            user_message="这次考试没考好，我有点难受",
            reply_text="哈哈哈先别想太多",
        )
        self.assertIn("严肃或负面场景中出现了笑声", result)

    def test_decision_can_explicitly_avoid_laughter(self) -> None:
        result = find_reply_violations(
            user_message="刚刚发生了一件事",
            reply_text="笑死，你继续说",
            decision={"reply_constraints": {"avoid": ["laughter"]}},
        )
        self.assertIn("严肃或负面场景中出现了笑声", result)

    def test_genuine_light_laughter_is_not_globally_banned(self) -> None:
        self.assertEqual(
            find_reply_violations(
                user_message="我刚把外卖拿成了室友的",
                reply_text="哈哈哈然后呢",
            ),
            [],
        )

    def test_template_and_customer_service_phrases_are_blocked(self) -> None:
        result = find_reply_violations(
            user_message="今天有点累",
            reply_text="我理解你的感受，不是你不努力，而是压力太大",
        )
        self.assertEqual(len(result), 2)

    def test_numbered_customer_service_summary_is_blocked(self) -> None:
        result = find_reply_violations(
            user_message="怎么复习",
            reply_text="1. 先列计划\n2. 再开始背书",
        )
        self.assertIn("普通聊天被整理成了编号总结", result)

    def test_numbered_steps_are_allowed_when_explicitly_requested(self) -> None:
        result = find_reply_violations(
            user_message="给我列一下具体步骤",
            reply_text="1. 先备份\n2. 再开始修改",
        )
        self.assertEqual(result, [])

    def test_detached_location_ack_is_rewritten(self) -> None:
        result = find_reply_violations(
            user_message="你看这个",
            reply_text="嗯 在哪呀",
        )
        self.assertIn("普通地点追问前加了显得疏离的‘嗯’", result)

    def test_direct_location_question_remains_natural(self) -> None:
        self.assertEqual(
            find_reply_violations(user_message="你看这个", reply_text="在哪呀"),
            [],
        )

    def test_substantive_message_cannot_receive_only_a_short_question(self) -> None:
        result = find_reply_violations(
            user_message="我现在好难受，刚刚又想到死亡的事情了",
            reply_text="你怎么了？",
        )
        self.assertTrue(any("只提出一个很短的问题" in item for item in result))

    def test_initial_stage_does_not_jump_to_romantic_claim(self) -> None:
        result = find_reply_violations(
            user_message="你看我画的这个",
            reply_text="我喜欢你",
            relationship_stage="初识",
            intimacy=10,
        )
        self.assertIn("初识阶段无当前语境依据地推进亲密关系", result)

    def test_explicit_romantic_user_message_can_be_answered_naturally(self) -> None:
        result = find_reply_violations(
            user_message="我喜欢你 你愿意和我试试吗",
            reply_text="我也有点喜欢你 但我想慢一点确认",
            relationship_stage="初识",
            intimacy=10,
        )
        self.assertNotIn("初识阶段无当前语境依据地推进亲密关系", result)

    def test_serious_topic_cannot_be_evaded_with_meta_boundary(self) -> None:
        result = find_reply_violations(
            user_message="我又开始害怕死亡了",
            reply_text="这个话题对我来说有点突然 也不知道怎么接",
        )
        self.assertTrue(any("严肃话题被元话语带过" in item for item in result))


if __name__ == "__main__":
    unittest.main()
