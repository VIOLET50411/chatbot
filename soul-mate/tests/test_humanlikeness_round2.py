from __future__ import annotations

import sys
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.compile_humanlikeness_round2 import _du_cases, _ji_cases


class HumanlikenessRound2Tests(unittest.TestCase):
    def test_both_candidates_have_enough_review_cases(self) -> None:
        self.assertGreaterEqual(len(_du_cases()), 40)
        self.assertGreaterEqual(len(_ji_cases()), 40)

    def test_candidate_replies_avoid_known_global_patterns(self) -> None:
        for item in [*_du_cases(), *_ji_cases()]:
            reply = item["candidate_reply"]
            self.assertIsNone(re.search(r"不是[^\n。！？]{0,40}而是", reply))
            self.assertNotIn("！", reply)
            self.assertNotIn("我理解你的感受", reply)
            self.assertNotIn("我只问这一次", reply)
            self.assertNotIn("这句有点伤人", reply)

    def test_serious_review_cases_do_not_use_laughter(self) -> None:
        serious = ("崩", "难受", "怕死", "委屈", "滚", "分手")
        for item in [*_du_cases(), *_ji_cases()]:
            if any(marker in item["user_message"] for marker in serious):
                self.assertNotIn("哈哈", item["candidate_reply"])

if __name__ == "__main__":
    unittest.main()
