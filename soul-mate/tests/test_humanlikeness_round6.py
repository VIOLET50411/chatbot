from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.compile_ji_review_round5 import _cases as round5_cases
from scripts.compile_ji_review_round6 import _cases as round6_cases


class JiRound6Tests(unittest.TestCase):
    def test_only_last_rejected_reply_changes(self) -> None:
        before = {item["user_message"]: item for item in round5_cases()}
        after = {item["user_message"]: item for item in round6_cases()}
        changed = [key for key in before if before[key] != after[key]]
        self.assertEqual(changed, ["我现在又累又饿还不想动"])
        self.assertEqual(after[changed[0]]["candidate_reply"], "那先点个吃的\n吃完再说")


if __name__ == "__main__":
    unittest.main()
