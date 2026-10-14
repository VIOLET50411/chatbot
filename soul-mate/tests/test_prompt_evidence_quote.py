from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from memory.models import InnerState
from persona.prompt_builder import build_decision_prompt


class PromptStyleTests(unittest.TestCase):
    def test_chat_style_profile_json_is_parsed(self) -> None:
        from persona.prompt_builder import _format_style

        style = {
            "chat_style_profile": json.dumps(
                {
                    "common_words": ["老师", "哈哈哈"],
                    "sentence_style": "短句连发",
                    "emoji_habits": ["哈哈哈"],
                    "special_expressions": ["好羡慕"],
                },
                ensure_ascii=False,
            )
        }
        rendered = _format_style(style)
        self.assertIn("老师", rendered)
        self.assertIn("短句连发", rendered)


class PromptEvidenceQuoteTests(unittest.TestCase):
    def test_conversation_checkpoint_is_injected_before_recent_history(self) -> None:
        messages = asyncio.run(
            build_decision_prompt(
                message="继续刚才的话题",
                state=InnerState(),
                memory_context={
                    "conversation_checkpoint": {
                        "summary_text": "最近在准备考试，已经连续复习几天",
                        "open_topics": ["今天还剩多少题"],
                        "promises": ["之后再回访复习状态"],
                    }
                },
                persona_snapshot={"is_active": True, "stable_profile": {"name": "角色"}},
                history=[],
            )
        )
        content = messages[0]["content"]
        self.assertIn("最近在准备考试，已经连续复习几天", content)
        self.assertIn("今天还剩多少题", content)

    def test_reply_contract_does_not_turn_three_chunks_into_a_persona_rule(self) -> None:
        messages = asyncio.run(
            build_decision_prompt(
                message="鎴戝垰鍒氭兂鍒颁竴浠朵簨",
                state=InnerState(),
                memory_context={},
                persona_snapshot={"is_active": True, "stable_profile": {"name": "瑙掕壊"}},
                history=[],
            )
        )
        instruction = messages[0]["content"]
        self.assertNotIn('"max_chunks": 3', instruction)

    def test_memory_instruction_requires_current_user_verbatim_evidence_quote(self) -> None:
        messages = asyncio.run(
            build_decision_prompt(
                message="我喜欢手冲咖啡。",
                state=InnerState(),
                memory_context={},
                persona_snapshot={"is_active": True, "stable_profile": {"name": "角色"}},
                history=[],
            )
        )
        instruction = messages[0]["content"]
        self.assertIn('"evidence_quote"', instruction)
        self.assertIn("当前用户消息", instruction)
        self.assertIn("逐字短引", instruction)

    def test_persona_profile_sections_are_injected_from_active_snapshot(self) -> None:
        messages = asyncio.run(
            build_decision_prompt(
                message="最近怎么样",
                state=InnerState(),
                memory_context={},
                persona_snapshot={
                    "is_active": True,
                    "stable_profile": {
                        "name": "角色甲",
                        "occupation": "测试职业",
                        "social_boundaries": "不喜欢被催消息",
                        "current_life": "最近在准备考试",
                        "relationship_with_user": "普通同学",
                    },
                },
                history=[],
            )
        )
        content = messages[0]["content"]
        self.assertIn("价值观与边界", content)
        self.assertIn("不喜欢被催消息", content)
        self.assertIn("近期状态", content)
        self.assertIn("关系背景", content)


if __name__ == "__main__":
    unittest.main()
