from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from bridge.v1_store import BridgeStore, OutboundActionNotFound, UnknownContact
from core.character_context import CharacterContext
from core.contact_runtime import ContactConversationService
from core.contact_runtime_store import MEMORY_LAYERS, ContactRuntimeStore
from core.interaction_reducer import ConversationSignal
from memory.models import InnerState
from persona.review_cases import PersonaReviewStore
from scripts.init_db import init


def _snapshot() -> dict:
    return {
        "is_active": True,
        "stable_profile": {
            "name": "角色甲",
            "personality": "独立、有边界",
        },
        "channel_styles": {
            "wechat_chat": {
                "style": {
                    "common_words": ["确实"],
                    "sentence_style": "自然短句",
                }
            }
        },
    }


class ContactRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        root = Path(self.temporary_dir.name)
        self.context = CharacterContext("default", "角色甲", root / "persona")
        init(self.context.workspace_dir)
        self.bridge_store = BridgeStore(root / "core" / "soul_mate.db")
        self.bridge_store.initialize_schema()
        self.alice = self.bridge_store.create_contact(
            external_id="wxid-alice",
            display_name="Alice",
        )
        self.bob = self.bridge_store.create_contact(
            external_id="wxid-bob",
            display_name="Bob",
        )
        self.runtime_store = ContactRuntimeStore(self.bridge_store.database_path)
        self.service = ContactConversationService(
            character_context=self.context,
            bridge_store=self.bridge_store,
            runtime_store=self.runtime_store,
        )

    def test_two_contacts_keep_history_memory_relationship_and_outbox_isolated(self) -> None:
        decisions = [
            {
                "should_reply": True,
                "intent": "回应 Alice",
                "memory_candidates": [
                    {
                        "type": "preference",
                        "content": "Alice 喜欢手冲咖啡",
                        "evidence_quote": "手冲咖啡",
                        "importance": 4,
                    }
                ],
            },
            {
                "should_reply": True,
                "intent": "回应 Bob",
                "memory_candidates": [],
            },
        ]

        async def run() -> None:
            with (
                patch("core.contact_runtime.get_active_snapshot", return_value=_snapshot()),
                patch("core.contact_runtime.get_legacy_inner_state", new=AsyncMock(return_value=InnerState())),
                patch("core.contact_runtime.llm_client.structured_chat", new=AsyncMock(side_effect=decisions)),
                patch("core.contact_runtime.llm_client.text_chat", new=AsyncMock(side_effect=["我也挺喜欢", "知道了"])),
                patch("core.contact_runtime.analyze_conversation", new=AsyncMock(return_value=ConversationSignal())),
                patch("core.contact_runtime.postprocess.humanize_reply", side_effect=lambda text, state: [text]),
                patch("core.contact_runtime.calculate_reply_delay", return_value=0),
                patch("core.contact_runtime.calculate_typing_sequence", return_value=[]),
            ):
                await self.service.process(
                    contact_id=self.alice.id,
                    message="我最近喜欢手冲咖啡",
                    source_event_ids=["alice-event-1"],
                    batch_id="alice-batch-1",
                )
                await self.service.process(
                    contact_id=self.bob.id,
                    message="我今天有点累",
                    source_event_ids=["bob-event-1"],
                    batch_id="bob-batch-1",
                )

        asyncio.run(run())

        alice_history = self.runtime_store.get_recent_messages(self.alice.id)
        bob_history = self.runtime_store.get_recent_messages(self.bob.id)
        self.assertEqual([item["content"] for item in alice_history], ["我最近喜欢手冲咖啡", "我也挺喜欢"])
        self.assertEqual([item["content"] for item in bob_history], ["我今天有点累", "知道了"])
        self.assertNotIn("Bob", str(self.runtime_store.list_memories(self.alice.id)))
        self.assertEqual(len(self.runtime_store.list_memories(self.alice.id)), 1)
        self.assertEqual(self.runtime_store.list_memories(self.bob.id), [])

        alice_action = self.bridge_store.get_action_by_dedupe_key("reply:alice-batch-1")
        bob_action = self.bridge_store.get_action_by_dedupe_key("reply:bob-batch-1")
        self.assertEqual(alice_action.external_contact_id, "wxid-alice")
        self.assertIn("chunk_delays_seconds", alice_action.payload)
        self.assertNotIn("chunk_delays", alice_action.payload)
        self.assertEqual(bob_action.external_contact_id, "wxid-bob")

    def test_validation_mode_replies_without_changing_history_memory_or_relationship(self) -> None:
        before_relationship = self.runtime_store.get_relationship(self.alice.id)

        async def run() -> None:
            with (
                patch("core.contact_runtime.get_active_snapshot", return_value=_snapshot()),
                patch("core.contact_runtime.get_legacy_inner_state", new=AsyncMock(return_value=InnerState())),
                patch(
                    "core.contact_runtime.llm_client.structured_chat",
                    new=AsyncMock(
                        side_effect=[
                            {
                                "should_reply": True,
                                "intent": "回应验收消息",
                                "memory_candidates": [
                                    {
                                        "type": "event",
                                        "content": "验收时看到一只猫",
                                        "evidence_quote": "一只猫",
                                        "importance": 4,
                                    }
                                ],
                            },
                            {"pass": True, "issues": [], "rewrite_instruction": ""},
                        ]
                    ),
                ),
                patch("core.contact_runtime.llm_client.text_chat", new=AsyncMock(return_value="在哪呀 挺可爱的")),
                patch("core.contact_runtime.analyze_conversation", new=AsyncMock()) as analyze,
                patch("core.contact_runtime.postprocess.humanize_reply", return_value=["在哪呀", "挺可爱的"]),
                patch("core.contact_runtime.calculate_reply_delay", return_value=0),
                patch("core.contact_runtime.calculate_typing_sequence", return_value=[]),
            ):
                await self.service.process(
                    contact_id=self.alice.id,
                    message="你看这个\n[图片内容：一只猫]\n[对方发来语音，Plus 转写：在店里看到的]",
                    source_event_ids=["validation-text", "validation-image", "validation-voice"],
                    batch_id="validation-batch",
                    context_mode="validation",
                )
                analyze.assert_not_awaited()

        asyncio.run(run())

        self.assertEqual(self.runtime_store.get_recent_messages(self.alice.id), [])
        self.assertEqual(self.runtime_store.list_memories(self.alice.id), [])
        self.assertEqual(self.runtime_store.get_relationship(self.alice.id), before_relationship)
        action = self.bridge_store.get_action_by_dedupe_key("reply:validation-batch")
        self.assertEqual(action.payload["context_mode"], "validation")

    def test_validation_mode_keeps_only_in_memory_context_for_follow_up_clarification(self) -> None:
        before_relationship = self.runtime_store.get_relationship(self.alice.id)
        decision_prompts: list[list[dict]] = []
        responses = iter([
            {"should_reply": True, "intent": "接住具体题量和疲惫", "memory_candidates": []},
            {"pass": True, "issues": [], "rewrite_instruction": ""},
            {"should_reply": True, "intent": "澄清上一问的范围", "memory_candidates": []},
            {"pass": True, "issues": [], "rewrite_instruction": ""},
        ])

        async def structured(messages, **_kwargs):
            if "隐藏决策任务" in str(messages[0].get("content") or ""):
                decision_prompts.append(messages)
            return next(responses)

        async def run() -> None:
            with (
                patch("core.contact_runtime.get_active_snapshot", return_value=_snapshot()),
                patch("core.contact_runtime.get_legacy_inner_state", new=AsyncMock(return_value=InnerState())),
                patch("core.contact_runtime.llm_client.structured_chat", new=AsyncMock(side_effect=structured)),
                patch(
                    "core.contact_runtime.llm_client.text_chat",
                    new=AsyncMock(side_effect=["这些题都得这几天做完吗", "对 我问这些题准备几天做完"]),
                ),
                patch("core.contact_runtime.analyze_conversation", new=AsyncMock()) as analyze,
                patch("core.contact_runtime.postprocess.humanize_reply", side_effect=lambda text, _state: [text]),
                patch("core.contact_runtime.calculate_reply_delay", return_value=0),
                patch("core.contact_runtime.calculate_typing_sequence", return_value=[]),
            ):
                await self.service.process(
                    contact_id=self.alice.id,
                    message=(
                        "[图片内容：一页需要完成的大量题目]\n"
                        "[对方发来语音，Plus 转写：你看我最近得做这么多题。]\n"
                        "考研好累"
                    ),
                    source_event_ids=["validation-image", "validation-voice", "validation-text"],
                    batch_id="validation-rich-batch",
                    context_mode="validation",
                )
                await self.service.process(
                    contact_id=self.alice.id,
                    message="你是说今天吗",
                    source_event_ids=["validation-follow-up"],
                    batch_id="validation-follow-up-batch",
                    context_mode="validation",
                )
                analyze.assert_not_awaited()

        asyncio.run(run())

        second_prompt = str(decision_prompts[1])
        self.assertIn("考研好累", second_prompt)
        self.assertIn("这些题都得这几天做完吗", second_prompt)
        self.assertIn("你是说今天吗", second_prompt)
        self.assertEqual(self.runtime_store.get_recent_messages(self.alice.id), [])
        self.assertEqual(self.runtime_store.list_memories(self.alice.id), [])
        self.assertEqual(self.runtime_store.get_relationship(self.alice.id), before_relationship)

    def test_multimodal_context_audit_rewrites_generic_last_line_only_reply(self) -> None:
        planner = AsyncMock(side_effect=[
            {"should_reply": True, "intent": "回应疲惫", "memory_candidates": []},
            {
                "pass": False,
                "issues": ["只回应情绪，没有承接图片和语音中的具体任务量"],
                "rewrite_instruction": "自然提到这些题很多，再接住疲惫",
            },
            {"pass": True, "issues": [], "rewrite_instruction": ""},
        ])
        renderer = AsyncMock(side_effect=[
            "考研确实挺费精力的",
            "这也太多了 这些都得最近做完吗",
        ])

        async def run() -> None:
            with (
                patch("core.contact_runtime.get_active_snapshot", return_value=_snapshot()),
                patch("core.contact_runtime.get_legacy_inner_state", new=AsyncMock(return_value=InnerState())),
                patch("core.contact_runtime.llm_client.structured_chat", new=planner),
                patch("core.contact_runtime.llm_client.text_chat", new=renderer),
                patch("core.contact_runtime.analyze_conversation", new=AsyncMock()),
                patch("core.contact_runtime.postprocess.humanize_reply", side_effect=lambda text, _state: [text]),
                patch("core.contact_runtime.calculate_reply_delay", return_value=0),
                patch("core.contact_runtime.calculate_typing_sequence", return_value=[]),
            ):
                await self.service.process(
                    contact_id=self.alice.id,
                    message=(
                        "[图片内容：屏幕上列了很多需要完成的题]\n"
                        "[对方发来语音，Plus 转写：你看我最近得做这么多题。]\n"
                        "考研好累"
                    ),
                    source_event_ids=["audit-image", "audit-voice", "audit-text"],
                    batch_id="audit-rich-batch",
                    context_mode="validation",
                )

        asyncio.run(run())
        action = self.bridge_store.get_action_by_dedupe_key("reply:audit-rich-batch")
        self.assertEqual(action.payload["chunks"], ["这也太多了 这些都得最近做完吗"])
        self.assertEqual(renderer.await_count, 2)
        self.assertEqual(planner.await_count, 3)

    def test_short_question_is_rewritten_before_it_can_reach_outbox(self) -> None:
        planner = AsyncMock(side_effect=[
            {"should_reply": True, "intent": "先接住难受", "memory_candidates": []},
            {"pass": True, "issues": [], "rewrite_instruction": ""},
        ])
        renderer = AsyncMock(side_effect=["你怎么了？", "我听到了 这种时候先别逼自己继续想，缓一会儿也行"])

        async def run() -> None:
            with (
                patch("core.contact_runtime.get_active_snapshot", return_value=_snapshot()),
                patch("core.contact_runtime.get_legacy_inner_state", new=AsyncMock(return_value=InnerState())),
                patch("core.contact_runtime.llm_client.structured_chat", new=planner),
                patch("core.contact_runtime.llm_client.text_chat", new=renderer),
                patch("core.contact_runtime.analyze_conversation", new=AsyncMock()),
                patch("core.contact_runtime.postprocess.humanize_reply", side_effect=lambda text, _state: [text]),
                patch("core.contact_runtime.calculate_reply_delay", return_value=0),
                patch("core.contact_runtime.calculate_typing_sequence", return_value=[]),
            ):
                await self.service.process(
                    contact_id=self.alice.id,
                    message="我现在好难受",
                    source_event_ids=["short-question-event"],
                    batch_id="short-question-batch",
                    context_mode="validation",
                )

        asyncio.run(run())
        action = self.bridge_store.get_action_by_dedupe_key("reply:short-question-batch")
        self.assertEqual(action.payload["chunks"], ["我听到了 这种时候先别逼自己继续想，缓一会儿也行"])
        self.assertEqual(renderer.await_count, 2)

    def test_prompt_for_contact_never_contains_other_contact_data(self) -> None:
        self.runtime_store.save_message(
            contact_id=self.bob.id,
            role="user",
            content="Bob 的私密旧消息",
        )
        self.runtime_store.save_memory_candidates(
            contact_id=self.bob.id,
            user_message="我住在上海",
            candidates=[
                {
                    "type": "event",
                    "content": "Bob 住在上海",
                    "evidence_quote": "住在上海",
                    "importance": 4,
                }
            ],
            source_event_ids=["bob-private-event"],
        )
        review_store = PersonaReviewStore(self.bridge_store.database_path)
        review_store.seed_cases(
            candidate_snapshot_id="candidate-style",
            character_id="default",
            cases=[
                {
                    "category": "日常",
                    "user_message": f"你好 {index}",
                    "context_note": "初识",
                    "candidate_reply": f"慢慢说 {index}",
                }
                for index in range(40)
            ],
        )
        first_case = review_store.list_cases("candidate-style")["items"][0]
        review_store.rate_case(
            first_case["id"],
            rating="realistic",
            reasons=[],
            note="Owner 确认",
            actor="owner",
        )
        captured_messages: list[list[dict]] = []

        async def planner(messages):
            captured_messages.append(messages)
            return {"should_reply": True, "intent": "普通回应", "memory_candidates": []}

        async def run() -> None:
            with (
                patch("core.contact_runtime.get_active_snapshot", return_value=_snapshot()),
                patch("core.contact_runtime.get_legacy_inner_state", new=AsyncMock(return_value=InnerState())),
                patch("core.contact_runtime.llm_client.structured_chat", new=AsyncMock(side_effect=planner)),
                patch("core.contact_runtime.llm_client.text_chat", new=AsyncMock(return_value="嗯")),
                patch("core.contact_runtime.analyze_conversation", new=AsyncMock(return_value=ConversationSignal())),
                patch("core.contact_runtime.postprocess.humanize_reply", return_value=["嗯"]),
                patch("core.contact_runtime.calculate_reply_delay", return_value=0),
                patch("core.contact_runtime.calculate_typing_sequence", return_value=[]),
            ):
                await self.service.process(
                    contact_id=self.alice.id,
                    message="你好",
                    source_event_ids=["alice-event-private-test"],
                    batch_id="alice-batch-private-test",
                )

        asyncio.run(run())
        serialized = str(captured_messages)
        self.assertNotIn("Bob 的私密旧消息", serialized)
        self.assertNotIn("Bob 住在上海", serialized)
        self.assertIn("慢慢说 0", serialized)
        self.assertNotIn("慢慢说 1", serialized)

    def test_all_eight_memory_layers_are_validated_and_contact_isolated(self) -> None:
        for layer in sorted(MEMORY_LAYERS):
            self.runtime_store.save_layer_memory(
                contact_id=self.alice.id,
                layer=layer,
                content=f"Alice 的 {layer} 记忆",
                source_event_ids=[f"source-{layer}"],
            )
        alice = self.runtime_store.list_memories(self.alice.id)
        self.assertEqual({item["layer"] for item in alice}, MEMORY_LAYERS)
        self.assertEqual(self.runtime_store.list_memories(self.bob.id), [])
        daily = next(item for item in alice if item["layer"] == "daily")
        self.assertTrue(daily["expires_at"])
        self.assertTrue(all(item["source_trace_id"] for item in alice))
        with self.assertRaises(ValueError):
            self.runtime_store.save_layer_memory(
                contact_id=self.alice.id,
                layer="unknown",
                content="不允许的层级",
            )

    def test_memory_recall_is_marked_only_after_the_reply_uses_it(self) -> None:
        memory_id = self.runtime_store.save_layer_memory(
            contact_id=self.alice.id,
            layer="core",
            content="Alice 喜欢城市散步",
            source_event_ids=["alice-recall-event"],
        )

        async def run() -> None:
            with (
                patch("core.contact_runtime_store.llm_client.embed", new=AsyncMock(return_value=[1.0, 0.0, 0.0])),
                patch(
                    "core.contact_runtime_store.ContactRuntimeStore._embedding_for_memory",
                    new=AsyncMock(return_value=[1.0, 0.0, 0.0]),
                ),
            ):
                context = await self.runtime_store.retrieve_context(
                    self.alice.id,
                    "城市散步",
                )
                self.assertEqual(context["relevant_memories"][0]["id"], memory_id)

        asyncio.run(run())
        self.assertEqual(self.runtime_store.list_memories(self.alice.id)[0]["recall_count"], 0)
        self.assertEqual(
            self.runtime_store.mark_memory_used(
                contact_id=self.alice.id,
                memory_ids=[memory_id],
                source_message_id="alice-recall-reply",
            ),
            1,
        )
        self.assertEqual(self.runtime_store.list_memories(self.alice.id)[0]["recall_count"], 1)

    def test_memory_associations_are_grouped_by_contact_and_daily_episode(self) -> None:
        alice_memory = self.runtime_store.save_layer_memory(
            contact_id=self.alice.id,
            layer="daily",
            content="Alice 今天去城市散步喝咖啡",
            source_event_ids=["alice-association-event"],
        )
        bob_memory = self.runtime_store.save_layer_memory(
            contact_id=self.bob.id,
            layer="daily",
            content="Bob 今天去城市散步喝咖啡",
            source_event_ids=["bob-association-event"],
        )

        alice = self.runtime_store.list_memory_associations(self.alice.id)
        bob = self.runtime_store.list_memory_associations(self.bob.id)

        self.assertTrue(alice["entities"])
        self.assertTrue(alice["episodes"])
        self.assertIn(alice_memory, alice["memory_ids"])
        self.assertIn(bob_memory, bob["memory_ids"])
        self.assertNotIn(bob_memory, alice["memory_ids"])

    def test_conversation_checkpoint_keeps_summary_and_open_topics_per_contact(self) -> None:
        self.runtime_store.save_message(
            contact_id=self.alice.id,
            role="user",
            content="Alice 正在准备考试",
            message_id="alice-checkpoint-message",
        )
        self.runtime_store.save_conversation_checkpoint(
            contact_id=self.alice.id,
            covered_until_message_id="alice-checkpoint-message",
            summary_text="Alice 正在准备考试，最近感到疲惫",
            open_topics=["考试进度"],
            promises=["下次继续问复习状态"],
        )

        checkpoint = self.runtime_store.get_conversation_checkpoint(self.alice.id)
        self.assertEqual(checkpoint["summary_text"], "Alice 正在准备考试，最近感到疲惫")
        self.assertEqual(checkpoint["open_topics"], ["考试进度"])
        self.assertEqual(checkpoint["promises"], ["下次继续问复习状态"])
        self.assertIsNone(self.runtime_store.get_conversation_checkpoint(self.bob.id))

    def test_checkpoint_builder_uses_compact_model_and_keeps_recent_messages_out(self) -> None:
        for index in range(6):
            self.runtime_store.save_message(
                contact_id=self.alice.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"checkpoint-message-{index}",
                message_id=f"checkpoint-message-{index}",
            )

        async def run() -> None:
            with patch(
                "core.contact_runtime_store.llm_client.lightweight_chat",
                new=AsyncMock(return_value='{"summary":"前面讨论了考试安排","open_topics":["复习进度"],"promises":[]}'),
            ) as compact:
                checkpoint_id = await self.runtime_store.create_conversation_checkpoint(
                    self.alice.id,
                    min_messages=4,
                    keep_recent=2,
                )
                self.assertTrue(checkpoint_id)
                compact.assert_awaited_once()

        asyncio.run(run())
        checkpoint = self.runtime_store.get_conversation_checkpoint(self.alice.id)
        self.assertEqual(checkpoint["summary_text"], "前面讨论了考试安排")
        self.assertEqual(checkpoint["covered_until_message_id"], "checkpoint-message-3")

    def test_checkpoint_failure_does_not_write_fallback_or_move_waterline(self) -> None:
        for index in range(6):
            self.runtime_store.save_message(
                contact_id=self.alice.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"failed-checkpoint-{index}",
                message_id=f"failed-checkpoint-{index}",
            )

        async def run() -> None:
            with patch(
                "core.contact_runtime_store.llm_client.lightweight_chat",
                new=AsyncMock(side_effect=RuntimeError("摘要服务暂时不可用")),
            ):
                checkpoint_id = await self.runtime_store.create_conversation_checkpoint(
                    self.alice.id,
                    min_messages=4,
                    keep_recent=2,
                )
                self.assertIsNone(checkpoint_id)

        asyncio.run(run())
        self.assertIsNone(self.runtime_store.get_conversation_checkpoint(self.alice.id))

    def test_runtime_marks_only_selected_memory_as_used_after_reply_generation(self) -> None:
        memory_id = self.runtime_store.save_layer_memory(
            contact_id=self.alice.id,
            layer="core",
            content="Alice 喜欢城市散步",
            source_event_ids=["alice-used-memory"],
        )

        async def run() -> None:
            with (
                patch("core.contact_runtime.get_active_snapshot", return_value=_snapshot()),
                patch("core.contact_runtime.get_legacy_inner_state", new=AsyncMock(return_value=InnerState())),
                patch(
                    "core.contact_runtime_store.llm_client.embed",
                    new=AsyncMock(return_value=[1.0, 0.0, 0.0]),
                ),
                patch(
                    "core.contact_runtime_store.ContactRuntimeStore._embedding_for_memory",
                    new=AsyncMock(return_value=[1.0, 0.0, 0.0]),
                ),
                patch(
                    "core.contact_runtime.llm_client.structured_chat",
                    new=AsyncMock(return_value={"should_reply": True, "memory_candidates": []}),
                ),
                patch("core.contact_runtime.llm_client.text_chat", new=AsyncMock(return_value="记得")),
                patch("core.contact_runtime.analyze_conversation", new=AsyncMock(return_value=ConversationSignal())),
                patch("core.contact_runtime.postprocess.humanize_reply", return_value=["记得"]),
                patch("core.contact_runtime.calculate_reply_delay", return_value=0),
                patch("core.contact_runtime.calculate_typing_sequence", return_value=[]),
            ):
                await self.service.process(
                    contact_id=self.alice.id,
                    message="城市散步",
                    source_event_ids=["alice-used-memory-event"],
                    batch_id="alice-used-memory-batch",
                )

        asyncio.run(run())
        memory = next(item for item in self.runtime_store.list_memories(self.alice.id) if item["id"] == memory_id)
        self.assertEqual(memory["recall_count"], 1)

    def test_light_topic_followup_is_delayed_and_serious_topic_is_not_overwritten(self) -> None:
        from memory.models import RelationshipStage

        now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
        self.runtime_store.note_topic_state(
            contact_id=self.alice.id,
            message="今天咖啡店遇到一只猫",
            source_message_id="alice-topic-1",
            relationship_stage=RelationshipStage.FAMILIAR,
            now=now,
        )
        pending = self.runtime_store.get_pending_topic(self.alice.id, now=now + timedelta(minutes=10))
        self.assertIsNone(pending)
        pending = self.runtime_store.get_pending_topic(self.alice.id, now=now + timedelta(hours=2))
        self.assertIsNotNone(pending)
        self.assertEqual(pending["source_message_id"], "alice-topic-1")

        self.runtime_store.note_topic_state(
            contact_id=self.alice.id,
            message="我这次考试崩了，真的很难受",
            source_message_id="alice-topic-serious",
            relationship_stage=RelationshipStage.FAMILIAR,
            now=now + timedelta(minutes=1),
        )
        self.assertIsNone(self.runtime_store.get_pending_topic(self.alice.id, now=now + timedelta(hours=2)))

    def test_contact_memory_layers_and_nightly_consolidation_are_isolated(self) -> None:
        self.runtime_store.save_memory_candidates(
            contact_id=self.alice.id,
            user_message="我今天终于通过了重要考试 而且特别激动",
            candidates=[{
                "type": "event",
                "content": "Alice 通过了一场重要考试",
                "evidence_quote": "通过了重要考试",
                "importance": 5,
                "valence": 0.9,
                "arousal": 0.9,
            }],
            source_event_ids=["alice-memory-layer-event"],
        )
        self.runtime_store.save_memory_candidates(
            contact_id=self.bob.id,
            user_message="我喜欢古典音乐",
            candidates=[{
                "type": "preference",
                "content": "Bob 喜欢古典音乐",
                "evidence_quote": "喜欢古典音乐",
                "importance": 4,
            }],
            source_event_ids=["bob-memory-layer-event"],
        )

        alice_memory = self.runtime_store.list_memories(self.alice.id)[0]
        bob_memory = self.runtime_store.list_memories(self.bob.id)[0]
        self.assertEqual(alice_memory["layer"], "daily")
        self.assertEqual(alice_memory["valence"], 0.9)
        self.assertEqual(bob_memory["layer"], "core")
        self.assertEqual(bob_memory["expires_at"], "")

        connection = sqlite3.connect(self.bridge_store.database_path)
        try:
            connection.execute(
                "UPDATE contact_memories SET expires_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                (alice_memory["id"],),
            )
            connection.commit()
        finally:
            connection.close()
        result = self.runtime_store.consolidate_expired_memories(self.alice.id)
        self.assertEqual(result, {"examined": 1, "kept_as_diary": 1, "archived": 1})
        alice_memories = self.runtime_store.list_memories(self.alice.id)
        self.assertEqual({item["layer"] for item in alice_memories}, {"daily", "diary"})
        self.assertEqual(next(item for item in alice_memories if item["layer"] == "daily")["status"], "archived")
        diary = next(item for item in alice_memories if item["layer"] == "diary")
        self.assertEqual(diary["consolidated_from"], [alice_memory["id"]])
        self.assertEqual(self.runtime_store.list_memories(self.bob.id)[0]["status"], "published")

    def test_direct_pwa_reply_reuses_contact_progress_without_creating_wechat_action(self) -> None:
        planner = AsyncMock(return_value={
            "should_reply": True,
            "intent": "直接回应",
            "memory_candidates": [],
        })
        renderer = AsyncMock(return_value="我记得")

        async def run() -> None:
            with (
                patch("core.contact_runtime.get_active_snapshot", return_value=_snapshot()),
                patch("core.contact_runtime.get_legacy_inner_state", new=AsyncMock(return_value=InnerState())),
                patch("core.contact_runtime.llm_client.structured_chat", new=planner),
                patch("core.contact_runtime.llm_client.text_chat", new=renderer),
                patch("core.contact_runtime.analyze_conversation", new=AsyncMock(return_value=ConversationSignal())),
                patch("core.contact_runtime.postprocess.humanize_reply", return_value=["我记得"]),
                patch("core.contact_runtime.calculate_reply_delay", return_value=0),
                patch("core.contact_runtime.calculate_typing_sequence", return_value=[]),
            ):
                result = await self.service.process_direct(
                    contact_id=self.alice.id,
                    message="接着微信聊",
                    source_event_id="pwa-event-1",
                    conversation_id="pwa-conversation-1",
                )
                self.assertIsNotNone(result)
                self.assertEqual(result.chunks, ["我记得"])
                retried = await self.service.process_direct(
                    contact_id=self.alice.id,
                    message="接着微信聊",
                    source_event_id="pwa-event-1",
                    conversation_id="pwa-conversation-1",
                )
                self.assertEqual(retried.reply_text, "我记得")

        asyncio.run(run())
        history = self.runtime_store.get_recent_messages(self.alice.id)
        self.assertEqual([item["content"] for item in history], ["接着微信聊", "我记得"])
        planner.assert_awaited_once()
        renderer.assert_awaited_once()
        with self.assertRaises(OutboundActionNotFound):
            self.bridge_store.get_action_by_dedupe_key("reply:pwa-conversation-1")

    def test_timeline_page_uses_stable_cursor_for_older_messages(self) -> None:
        for index in range(3):
            self.runtime_store.save_message(
                contact_id=self.alice.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"message-{index}",
                message_id=f"timeline-{index}",
            )

        first, has_more = self.runtime_store.get_timeline_page(self.alice.id, limit=2)
        self.assertTrue(has_more)
        self.assertEqual(len(first), 2)
        cursor = first[-1]
        second, second_has_more = self.runtime_store.get_timeline_page(
            self.alice.id,
            limit=2,
            before_created_at=cursor["created_at"],
            before_id=cursor["id"],
        )
        self.assertFalse(second_has_more)
        self.assertEqual([item["id"] for item in second], ["timeline-0"])

    def test_unknown_contact_is_rejected_before_model_calls(self) -> None:
        planner = AsyncMock()

        async def run() -> None:
            with patch("core.contact_runtime.llm_client.structured_chat", new=planner):
                with self.assertRaises(UnknownContact):
                    await self.service.process(
                        contact_id="missing",
                        message="你好",
                        source_event_ids=["missing-event"],
                        batch_id="missing-batch",
                    )

        asyncio.run(run())
        planner.assert_not_awaited()

    def test_media_action_only_queues_current_contacts_approved_asset(self) -> None:
        asset = self.bridge_store.register_attachment(
            external_contact_id="wxid-alice",
            kind="image",
            storage_path="attachments/alice-approved.png",
            sha256="9" * 64,
            mime_type="image/png",
            size_bytes=12,
        )
        self.bridge_store.review_media_asset(asset.id, review_status="approved")

        async def run() -> None:
            with (
                patch("core.contact_runtime.get_active_snapshot", return_value=_snapshot()),
                patch("core.contact_runtime.get_legacy_inner_state", new=AsyncMock(return_value=InnerState())),
                patch("core.contact_runtime.llm_client.structured_chat", new=AsyncMock(return_value={
                    "should_reply": True,
                    "intent": "给对方看图",
                    "memory_candidates": [],
                    "media_action": {"type": "image", "asset_query": ""},
                })),
                patch("core.contact_runtime.llm_client.text_chat", new=AsyncMock(return_value="给你看这个")),
                patch("core.contact_runtime.analyze_conversation", new=AsyncMock(return_value=ConversationSignal())),
                patch("core.contact_runtime.postprocess.humanize_reply", return_value=["给你看这个"]),
                patch("core.contact_runtime.calculate_reply_delay", return_value=0),
                patch("core.contact_runtime.calculate_typing_sequence", return_value=[]),
            ):
                await self.service.process(
                    contact_id=self.alice.id,
                    message="发张图看看",
                    source_event_ids=["alice-media-event"],
                    batch_id="alice-media-batch",
                )

        asyncio.run(run())
        media = self.bridge_store.get_action_by_dedupe_key("reply:alice-media-batch:media")
        self.assertEqual(media.action_type, "image")
        self.assertEqual(media.payload["attachment_refs"], [asset.id])
        self.assertEqual(media.contact_id, self.alice.id)
        self.assertIsNone(
            self.bridge_store.find_approved_media_asset(
                contact_id=self.bob.id,
                kind="image",
            )
        )


if __name__ == "__main__":
    unittest.main()
