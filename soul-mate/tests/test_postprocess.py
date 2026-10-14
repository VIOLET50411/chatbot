from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.postprocess import humanize_reply
from memory.models import InnerState


class PostprocessTests(unittest.TestCase):
    def test_filler_only_reply_can_be_empty(self) -> None:
        self.assertEqual(humanize_reply("好的，", InnerState()), [])

    def test_short_explicit_lines_keep_semantic_blocks(self) -> None:
        self.assertEqual(
            humanize_reply("先吃点东西\n回来再看这道题", InnerState()),
            ["先吃点东西", "回来再看这道题"],
        )

    def test_more_than_three_lines_are_kept_when_each_line_is_complete(self) -> None:
        chunks = humanize_reply("第一句\n第二句\n第三句\n第四句\n第五句", InnerState())
        self.assertEqual(len(chunks), 5)
        joined = " ".join(chunks)
        for fragment in ("第一句", "第二句", "第三句", "第四句", "第五句"):
            self.assertIn(fragment, joined)

    def test_natural_number_of_complete_lines_is_not_forced_to_three(self) -> None:
        chunks = humanize_reply(
            "第一段完整意思\n第二段完整意思\n第三段完整意思\n第四段完整意思\n第五段完整意思",
            InnerState(),
        )
        self.assertEqual(len(chunks), 5)


if __name__ == "__main__":
    unittest.main()
