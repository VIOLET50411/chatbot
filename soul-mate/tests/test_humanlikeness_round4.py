from __future__ import annotations

import re
import unittest

from scripts.compile_du_review_revision import _cases as du_cases
from scripts.compile_ji_consultant_revision import _cases as ji_cases
from persona.prompt_builder import _format_available_media


class HumanlikenessRound4Tests(unittest.TestCase):
    def test_candidate_case_counts_are_stable(self) -> None:
        self.assertEqual(len(du_cases()), 40)
        self.assertEqual(len(ji_cases()), 46)

    def test_replies_do_not_reintroduce_rejected_ai_templates(self) -> None:
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
        for item in [*du_cases(), *ji_cases()]:
            reply = item["candidate_reply"]
            self.assertIsNone(re.search(r"不是[^\n。！？]{0,40}而是", reply))
            for phrase in rejected:
                self.assertNotIn(phrase, reply)

    def test_serious_scenes_do_not_add_laughter(self) -> None:
        serious = ("考试崩", "怕死", "彻底不存在", "委屈", "滚", "分手", "难过", "嫌弃")
        for item in [*du_cases(), *ji_cases()]:
            if any(marker in item["user_message"] for marker in serious):
                self.assertNotIn("哈哈", item["candidate_reply"])

    def test_ji_yu_remembers_owner_confirmed_consultant_context(self) -> None:
        indexed = {item["user_message"]: item["candidate_reply"] for item in ji_cases()}
        self.assertIn("意识会彻底消失", indexed["这两天又有点怕死 一想到以后彻底不存在就很难受"])
        self.assertIn("最近好一点了吗", indexed["[隔了一段时间，之前谈过死亡焦虑]"])
        self.assertIn("没资格喜欢她", indexed["我现在条件不够好 所以想变好一点再追真正喜欢的人"])
        self.assertIn("不会因为这些就看轻你", indexed["你知道我那么多不好的地方 会不会嫌弃我"])

    def test_du_jiaying_real_wechat_deferral_does_not_remove_review_cases(self) -> None:
        indexed = {item["user_message"]: item["candidate_reply"] for item in du_cases()}
        self.assertEqual(indexed["两天没找你"], "老师你最近这么忙的吗")
        self.assertIn("你再跟我说说", indexed["你根本不懂我"])

    def test_approved_private_media_is_described_without_forcing_source_language(self) -> None:
        content = _format_available_media([{
            "kind": "image",
            "summary": "重庆山城夜景和江边灯光",
            "presentation_mode": "private_roleplay",
        }])
        self.assertIn("重庆山城夜景和江边灯光", content)
        self.assertIn("可在私聊世界线中自然分享", content)
        self.assertNotIn("说明是网上看到", content)


if __name__ == "__main__":
    unittest.main()
