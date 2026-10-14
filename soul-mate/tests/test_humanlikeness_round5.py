from __future__ import annotations

import unittest

from scripts.compile_ji_review_round5 import _cases


class HumanlikenessRound5Tests(unittest.TestCase):
    def test_only_the_five_rejected_ji_yu_replies_are_rewritten(self) -> None:
        cases = _cases()
        indexed = {item["user_message"]: item["candidate_reply"] for item in cases}
        self.assertEqual(len(cases), 46)
        self.assertEqual(indexed["我现在又累又饿还不想动"], "先点个吃的\n吃饱了再想动不动")
        self.assertEqual(indexed["帮我排今天复习"], "发我看看\n我帮你排")
        self.assertEqual(indexed["这两天又有点怕死 一想到以后彻底不存在就很难受"], "怎么又想到这个了\n现在是不是特别难受")
        self.assertEqual(indexed["我现在条件不够好 所以想变好一点再追真正喜欢的人"], "那就先让自己变得更好一点\n不过现在的你也没有那么差")
        self.assertEqual(indexed["你知道我那么多不好的地方 会不会嫌弃我"], "会心疼啊\n但我不会因为这些就看轻你")


if __name__ == "__main__":
    unittest.main()
