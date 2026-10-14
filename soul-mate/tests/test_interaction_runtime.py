from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.character_context import CharacterContext
from memory.interaction_state_store import load_interaction_state
from scripts.init_db import init


class InteractionRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_dir.cleanup)
        root = Path(self._temporary_dir.name) / "characters"
        self.first = CharacterContext("role-a", "角色甲", root / "role-a")
        self.second = CharacterContext("role-b", "角色乙", root / "role-b")
        init(self.first.workspace_dir)
        init(self.second.workspace_dir)

    def test_engine_rejects_missing_snapshot_before_state_or_message_writes(self) -> None:
        from core.engine import ConversationEngine

        engine = ConversationEngine()
        async def run() -> None:
            with (
                patch("persona.dossier.get_active_snapshot", return_value=None),
                patch("core.engine.manager.save_message", new=AsyncMock()) as save_message,
            ):
                result = await engine.process(self.first, "user-a", "你好")
            self.assertIsNone(result)
            save_message.assert_not_awaited()

        asyncio.run(run())
        self.assertFalse(_has_interaction_state(self.first))

    def test_engine_commits_isolated_interaction_transitions(self) -> None:
        from core.engine import ConversationEngine

        engine = ConversationEngine()
        snapshot = {"is_active": True, "stable_profile": {"name": "快照角色"}}

        def active_snapshot() -> dict:
            return snapshot

        async def run() -> None:
            with (
                patch("persona.dossier.get_active_snapshot", side_effect=active_snapshot),
                patch("core.engine.manager.save_message", new=AsyncMock()),
                patch("core.engine.manager.get_today_agenda", new=AsyncMock(return_value=None)),
                patch("core.engine.manager.get_recent_messages", new=AsyncMock(return_value=[])),
                patch("core.engine.retriever.retrieve_context", new=AsyncMock(return_value={})),
                patch("core.engine.llm_client.structured_chat", new=AsyncMock(return_value={"should_reply": True, "memory_candidates": []})),
                patch("core.engine.llm_client.text_chat", new=AsyncMock(return_value="收到")),
                patch("core.engine.llm_client.lightweight_chat", new=AsyncMock(return_value=(
                    '{"sentiment":"positive","depth":"normal","intimacy_delta":0.5,'
                    '"intimacy_context":"normal","reason":"正常回应"}'
                ))),
                patch("core.engine.postprocess.humanize_reply", return_value=["收到"]),
                patch("core.typing_rhythm.calculate_typing_sequence", return_value=[]),
                patch("core.reply_delay.calculate_reply_delay", return_value=0),
            ):
                first_result = await engine.process(self.first, "user-a", "你好")
                second_result = await engine.process(self.second, "user-b", "你好")

                self.assertEqual(first_result["chunks"], ["收到"])
                self.assertEqual(second_result["chunks"], ["收到"])
                await engine.wait_for_background_tasks()

        asyncio.run(run())

        first_state = load_interaction_state(self.first)
        second_state = load_interaction_state(self.second)
        self.assertEqual(first_state.version, 1)
        self.assertEqual(second_state.version, 1)
        self.assertEqual(first_state.relationship.intimacy, 10.9)
        self.assertEqual(second_state.relationship.intimacy, 10.9)
        self.assertIsNotNone(first_state.activity.last_chat_at)
        self.assertIsNotNone(second_state.activity.last_chat_at)

    def test_memory_retrieval_failure_uses_empty_context_and_still_replies(self) -> None:
        from core.engine import ConversationEngine

        engine = ConversationEngine()
        observed_contexts: list[dict] = []

        async def capture_prompt(**kwargs):
            observed_contexts.append(kwargs["memory_context"])
            return []

        async def run() -> None:
            with (
                patch("persona.dossier.get_active_snapshot", return_value={"is_active": True, "stable_profile": {"name": "快照角色"}}),
                patch("core.engine.manager.save_message", new=AsyncMock(return_value=11)),
                patch("core.engine.manager.get_today_agenda", new=AsyncMock(return_value=None)),
                patch("core.engine.manager.get_recent_messages", new=AsyncMock(return_value=[])),
                patch("core.engine.retriever.retrieve_context", new=AsyncMock(side_effect=RuntimeError("vector unavailable"))),
                patch("core.engine.prompt_builder.build_decision_prompt", new=AsyncMock(side_effect=capture_prompt)),
                patch("core.engine.prompt_builder.build_render_prompt", new=AsyncMock(return_value=[])),
                patch("core.engine.llm_client.structured_chat", new=AsyncMock(return_value={"should_reply": True, "memory_candidates": []})),
                patch("core.engine.llm_client.text_chat", new=AsyncMock(return_value="收到")),
                patch("core.engine.postprocess.humanize_reply", return_value=["收到"]),
                patch("core.typing_rhythm.calculate_typing_sequence", return_value=[]),
                patch("core.reply_delay.calculate_reply_delay", return_value=0),
            ):
                result = await engine.process(self.first, "user-a", "你好")
                self.assertEqual(result["chunks"], ["收到"])
                await engine.wait_for_background_tasks()

        asyncio.run(run())
        self.assertEqual(observed_contexts, [{"relevant_memories": []}])

    def test_empty_humanized_reply_skips_assistant_history_and_post_process(self) -> None:
        from core.engine import ConversationEngine

        engine = ConversationEngine()

        async def run() -> None:
            with (
                patch("persona.dossier.get_active_snapshot", return_value={"is_active": True, "stable_profile": {"name": "快照角色"}}),
                patch("core.engine.manager.save_message", new=AsyncMock(return_value=11)) as save_message,
                patch("core.engine.manager.get_today_agenda", new=AsyncMock(return_value=None)),
                patch("core.engine.manager.get_recent_messages", new=AsyncMock(return_value=[])),
                patch("core.engine.retriever.retrieve_context", new=AsyncMock(return_value={})),
                patch("core.engine.prompt_builder.build_decision_prompt", new=AsyncMock(return_value=[])),
                patch("core.engine.prompt_builder.build_render_prompt", new=AsyncMock(return_value=[])),
                patch("core.engine.llm_client.structured_chat", new=AsyncMock(return_value={"should_reply": True, "memory_candidates": []})),
                patch("core.engine.llm_client.text_chat", new=AsyncMock(return_value="好的，")),
                patch("core.engine.postprocess.humanize_reply", return_value=[]),
                patch.object(engine, "_async_post_process", new=AsyncMock()) as post_process,
            ):
                result = await engine.process(self.first, "user-a", "你好")
                self.assertIsNone(result)
                self.assertEqual(save_message.await_count, 1)
                post_process.assert_not_called()

        asyncio.run(run())


def _has_interaction_state(context: CharacterContext) -> bool:
    with closing(sqlite3.connect(context.workspace_dir / "soul_mate.db")) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='interaction_state'"
        ).fetchone()
    return row is not None


if __name__ == "__main__":
    unittest.main()
