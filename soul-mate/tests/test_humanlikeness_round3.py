from __future__ import annotations

import re
import unittest

from scripts.compile_humanlikeness_round3 import _du_cases, _ji_cases


class HumanlikenessRound3Tests(unittest.TestCase):
    def test_both_candidates_have_exactly_forty_fresh_cases(self) -> None:
        self.assertEqual(len(_du_cases()), 40)
        self.assertEqual(len(_ji_cases()), 40)

    def test_replies_avoid_owner_rejected_ai_language(self) -> None:
        rejected = (
            "我理解你的感受",
            "我在",
            "听起来你",
            "接住",
            "接偏",
            "被接住",
            "拍板",
            "收一下",
        )
        for item in [*_du_cases(), *_ji_cases()]:
            reply = item["candidate_reply"]
            self.assertIsNone(re.search(r"不是[^\n。！？]{0,40}而是", reply))
            self.assertNotIn("！", reply)
            for phrase in rejected:
                self.assertNotIn(phrase, reply)

    def test_serious_scenes_do_not_use_laughter(self) -> None:
        serious = ("考试崩", "怕死", "委屈", "滚", "分手", "难过")
        for item in [*_du_cases(), *_ji_cases()]:
            if any(marker in item["user_message"] for marker in serious):
                self.assertNotIn("哈哈", item["candidate_reply"])

    def test_ji_yu_has_accurate_11408_examples(self) -> None:
        content = "\n".join(item["candidate_reply"] for item in _ji_cases())
        self.assertIn("数据结构和计组", content)
        self.assertIn("高数 线代 概率论", content)


if __name__ == "__main__":
    unittest.main()
