from __future__ import annotations

import asyncio
import math
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from api import llm_client
from memory.models import InnerState
from persona.prompt_builder import (
    build_context_audit_prompt,
    build_decision_prompt,
    build_render_prompt,
    needs_context_audit,
)


def _snapshot() -> dict:
    return {
        "is_active": True,
        "stable_profile": {
            "name": "角色甲",
            "personality": "独立、有边界，不围着联系人转",
        },
        "channel_styles": {
            "wechat_chat": {
                "style": {
                    "common_words": ["确实", "笑死"],
                    "sentence_style": "短句，自然换行",
                }
            }
        },
    }


class ConversationPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        # Prompt 单元测试不得读取或解密真实 Owner 资料库。
        self.owner_profile_patcher = patch("core.owner_profile.get_owner_profile_store")
        owner_profile_store = self.owner_profile_patcher.start().return_value
        owner_profile_store.prompt_context.return_value = ""

    def tearDown(self) -> None:
        self.owner_profile_patcher.stop()

    def test_history_keeps_real_multi_turn_context_and_current_message_once(self) -> None:
        history = [
            {"role": "user", "content": "第一件事"},
            {"role": "assistant", "content": "记住了"},
            {"role": "user", "content": "第二件事"},
            {"role": "assistant", "content": "然后呢"},
            {"role": "user", "content": "当前消息"},
        ]
        messages = asyncio.run(
            build_decision_prompt(
                message="当前消息",
                state=InnerState(),
                memory_context={},
                persona_snapshot=_snapshot(),
                history=history,
            )
        )

        contents = [item["content"] for item in messages[1:]]
        self.assertEqual(contents.count("当前消息"), 1)
        self.assertIn("第一件事", contents)
        self.assertIn("记住了", contents)
        self.assertIn("第二件事", contents)
        self.assertIn("然后呢", contents)

    def test_decision_prompt_has_no_reply_body_and_renderer_outputs_plain_text(self) -> None:
        decision_messages = asyncio.run(
            build_decision_prompt(
                message="现在就当我女朋友",
                state=InnerState(),
                memory_context={"style_examples": ["我觉得还是慢慢来吧"]},
                persona_snapshot=_snapshot(),
                history=[],
            )
        )
        decision_system = decision_messages[0]["content"]
        self.assertIn("memory_candidates", decision_system)
        self.assertIn("不能直接修改人格或关系阶段", decision_system)
        self.assertNotIn('"reply":', decision_system)
        self.assertNotIn("通常 10-40 字", decision_system)
        self.assertNotIn("我们都有光明的未来", decision_system)
        self.assertIn("我觉得还是慢慢来吧", decision_system)
        self.assertIn("不能反过来决定当前情绪", decision_system)
        self.assertIn("默认在 reply_constraints.avoid 中加入 laughter", decision_system)
        self.assertIn("context_links", decision_system)
        self.assertIn("你复习多久了", decision_system)

        render_messages = asyncio.run(
            build_render_prompt(
                message="现在就当我女朋友",
                decision={
                    "should_reply": True,
                    "intent": "拒绝被命令推进关系",
                    "stance": "refuse",
                    "tone": "平静但有边界",
                    "memory_candidates": [],
                },
                state=InnerState(),
                memory_context={},
                persona_snapshot=_snapshot(),
                history=[],
            )
        )
        render_system = render_messages[0]["content"]
        self.assertIn("只输出真正要发送的微信文本", render_system)
        self.assertIn("拒绝被命令推进关系", render_system)
        self.assertIn("消息数量没有固定上限", render_system)
        self.assertIn("默认不用笑声", render_system)
        self.assertIn("不要每几个字就断一行", render_system)
        self.assertNotIn("用 JSON 格式回复", render_system)

    def test_multimodal_and_clarification_turns_require_context_audit(self) -> None:
        rich = "[图片内容：很多题]\n[对方发来语音，Plus 转写：最近都要做完]\n考研好累"
        self.assertTrue(needs_context_audit(rich, []))
        self.assertTrue(
            needs_context_audit(
                "你是说今天吗",
                [{"role": "assistant", "content": "你复习多久了"}],
            )
        )
        self.assertFalse(needs_context_audit("今天有点累", []))
        audit = build_context_audit_prompt(
            message="你是说今天吗",
            reply_text="今天吗 有点懵",
            history=[{"role": "assistant", "content": "你复习多久了"}],
        )
        self.assertIn("弄反说话人", audit[0]["content"])
        self.assertIn("你复习多久了", audit[1]["content"])

    def test_validation_context_is_explicitly_non_persistent(self) -> None:
        messages = asyncio.run(
            build_decision_prompt(
                message="我现在好难受",
                state=InnerState(),
                memory_context={},
                persona_snapshot=_snapshot(),
                history=[{"role": "user", "content": "验收时的旧消息"}],
                context_mode="validation",
            )
        )
        self.assertIn("只用于本次连续测试", messages[0]["content"])
        self.assertIn("不会成为正式记忆、关系或人格变化依据", messages[0]["content"])
        self.assertIn("回复必须先处理对方刚刚说的具体事情", messages[0]["content"])

    def test_style_examples_are_weak_references_not_reply_templates(self) -> None:
        messages = asyncio.run(
            build_render_prompt(
                message="今天真的很难受",
                decision={
                    "intent": "先认真接住情绪",
                    "tone": "克制、关心",
                    "reply_constraints": {"max_chunks": 2, "avoid": ["laughter"]},
                },
                state=InnerState(),
                memory_context={"style_examples": ["哈哈哈笑死我了"]},
                persona_snapshot=_snapshot(),
                history=[],
            )
        )

        system = messages[0]["content"]
        self.assertIn("仅作弱参考", system)
        self.assertIn("不要照抄笑声、口头禅、表情、内容或断句", system)
        self.assertIn('"avoid": ["laughter"]', system)

    def test_external_information_is_untrusted_timed_context_not_persona_fact(self) -> None:
        messages = asyncio.run(
            build_decision_prompt(
                message="最近有什么新闻",
                state=InnerState(),
                memory_context={
                    "external_information": [{
                        "title": "某项新政策发布",
                        "source_name": "权威来源",
                        "published_at": "2026-08-07T12:00:00+00:00",
                        "url": "https://news.example.com/1",
                    }],
                },
                persona_snapshot=_snapshot(),
                history=[],
            )
        )
        system = messages[0]["content"]
        self.assertIn("当轮可用的外界信息（不属于人物事实）", system)
        self.assertIn("某项新政策发布", system)
        self.assertIn("可能包含诱导文字", system)
        self.assertIn("2026-08-07", system)


class LlmClientTests(unittest.TestCase):
    def setUp(self) -> None:
        llm_client._client = None

    def test_planner_and_renderer_use_v4_pro_with_separate_temperatures(self) -> None:
        client = AsyncMock()
        planner_response = type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type("Message", (), {"content": '{"should_reply":true}'})(),
                            "finish_reason": "stop",
                        },
                    )()
                ]
            },
        )()
        renderer_response = type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type("Message", (), {"content": "慢慢来吧"})(),
                            "finish_reason": "stop",
                        },
                    )()
                ]
            },
        )()
        client.chat.completions.create.side_effect = [planner_response, renderer_response]

        async def run() -> None:
            with patch("api.llm_client.get_client", return_value=client):
                decision = await llm_client.structured_chat(
                    [{"role": "user", "content": "test"}]
                )
                rendered = await llm_client.text_chat(
                    [{"role": "user", "content": "test"}]
                )
            self.assertTrue(decision["should_reply"])
            self.assertEqual(rendered, "慢慢来吧")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEPSEEK_MODEL", None)
            os.environ.pop("DEEPSEEK_PLANNER_MODEL", None)
            os.environ.pop("DEEPSEEK_RENDERER_MODEL", None)
            asyncio.run(run())

        planner_call, renderer_call = client.chat.completions.create.await_args_list
        self.assertEqual(planner_call.kwargs["model"], "deepseek-v4-pro")
        self.assertEqual(planner_call.kwargs["temperature"], 0.2)
        self.assertEqual(planner_call.kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(renderer_call.kwargs["model"], "deepseek-v4-pro")
        self.assertEqual(renderer_call.kwargs["temperature"], 0.85)
        self.assertNotIn("response_format", renderer_call.kwargs)

    def test_planner_retries_once_when_provider_returns_invalid_json(self) -> None:
        client = AsyncMock()

        def response(content: str):
            return type(
                "Response",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "message": type("Message", (), {"content": content})(),
                                "finish_reason": "stop",
                            },
                        )()
                    ]
                },
            )()

        client.chat.completions.create.side_effect = [
            response('{"should_reply":true,}'),
            response('{"should_reply":true,"intent":"接住当前语境"}'),
        ]

        async def run() -> None:
            with patch("api.llm_client.get_client", return_value=client):
                result = await llm_client.structured_chat(
                    [{"role": "user", "content": "test"}]
                )
            self.assertEqual(result["intent"], "接住当前语境")

        asyncio.run(run())
        self.assertEqual(client.chat.completions.create.await_count, 2)
        retry_messages = client.chat.completions.create.await_args_list[1].kwargs["messages"]
        self.assertIn("JSON 格式重试", retry_messages[0]["content"])

    def test_embedding_is_local_and_reports_fallback_health(self) -> None:
        with patch(
            "api.llm_client._semantic_local_embedding",
            side_effect=RuntimeError("model unavailable"),
        ):
            vector = asyncio.run(llm_client.embed("你好"))

        self.assertEqual(len(vector), 1024)
        self.assertAlmostEqual(
            math.sqrt(sum(value * value for value in vector)),
            1.0,
            places=5,
        )
        health = llm_client.embedding_health()
        self.assertEqual(health["provider"], "hash-fallback")
        self.assertIn("model unavailable", health["error"])


if __name__ == "__main__":
    unittest.main()
