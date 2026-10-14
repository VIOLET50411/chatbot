from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.character_context import CharacterContext, character_scope
from memory.models import InnerState
from scripts.init_db import init


class RuntimeSnapshotPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_dir.cleanup)
        workspace = Path(self._temporary_dir.name) / "characters" / "role-a"
        init(data_dir=workspace)
        self.context = CharacterContext(
            character_id="role-a",
            display_name="角色甲",
            workspace_dir=workspace,
        )

    def test_prompt_uses_only_active_snapshot_and_wechat_channel_style(self) -> None:
        from persona.prompt_builder import build_prompt

        snapshot = {
            "is_active": True,
            "stable_profile": {
                "name": "快照角色",
                "age": "20",
                "occupation": "学生",
                "personality": "独立且有边界",
            },
            "channel_styles": {
                "wechat_chat": {
                    "style": {
                        "common_words": ["聊天词"],
                        "sentence_style": "短句为主",
                        "emoji_habits": ["^ ^"],
                        "special_expressions": ["聊天表达"],
                    }
                },
                "reference": {
                    "style": {
                        "common_words": ["短视频文案词"],
                        "special_expressions": ["短视频表达"],
                    }
                },
            },
        }

        with character_scope(self.context):
            messages = asyncio.run(build_prompt(
                message="你好",
                state=InnerState(),
                memory_context={},
                persona_snapshot=snapshot,
                history=[],
            ))

        system = messages[0]["content"]
        self.assertIn("快照角色", system)
        self.assertIn("聊天词", system)
        self.assertIn("聊天表达", system)
        self.assertNotIn("短视频文案词", system)
        self.assertNotIn("短视频表达", system)

    def test_prompt_fails_closed_without_active_snapshot(self) -> None:
        from persona.prompt_builder import build_prompt

        with character_scope(self.context):
            with self.assertRaises(RuntimeError):
                asyncio.run(build_prompt(
                    message="你好",
                    state=InnerState(),
                    memory_context={},
                    persona_snapshot=None,
                    history=[],
                ))

    def test_legacy_persona_manager_reads_snapshot_and_rejects_writes(self) -> None:
        from memory import manager
        from persona import persona_manager

        snapshot = {
            "is_active": True,
            "stable_profile": {"name": "快照角色"},
        }
        with (
            character_scope(self.context),
            patch("persona.persona_manager.get_active_snapshot", return_value=snapshot),
        ):
            persona = asyncio.run(persona_manager.get_persona())
            with self.assertRaises(RuntimeError):
                asyncio.run(persona_manager.update_persona({"name": "旧 KV"}))
            with self.assertRaises(RuntimeError):
                asyncio.run(manager.set_persona_key("name", "旧 KV"))
            with self.assertRaises(RuntimeError):
                asyncio.run(manager.add_corpus_manual("绕过审核"))
            with self.assertRaises(RuntimeError):
                asyncio.run(manager.save_corpus("绕过审核", "manual", {}))
            with self.assertRaises(RuntimeError):
                asyncio.run(manager.save_aggregated_style({"tone": "旧风格"}))
            with self.assertRaises(RuntimeError):
                asyncio.run(manager.delete_corpus_entry(1))

        self.assertEqual(persona, {"name": "快照角色"})


class SchedulerSnapshotBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = CharacterContext(
            character_id="scheduler-role",
            display_name="调度角色",
            workspace_dir=Path(tempfile.gettempdir()) / "scheduler-role",
        )

    def test_background_generation_stops_without_active_snapshot(self) -> None:
        from memory import consolidator
        from scheduler import agenda

        with (
            patch("scheduler.agenda.get_active_snapshot", return_value=None),
            patch("memory.consolidator.get_active_snapshot", return_value=None),
            patch.object(agenda.manager, "get_today_agenda", new=AsyncMock()) as get_agenda,
            patch.object(consolidator.manager, "get_today_conversations", new=AsyncMock()) as get_conversations,
        ):
            asyncio.run(agenda.generate_daily_agenda(self.context))
            asyncio.run(consolidator.run(self.context))

        get_agenda.assert_not_awaited()
        get_conversations.assert_not_awaited()

    def test_agenda_uses_active_snapshot_not_legacy_persona(self) -> None:
        from scheduler import agenda

        snapshot = {"is_active": True, "stable_profile": {"name": "快照角色"}}
        state = InnerState()
        with (
            patch("scheduler.agenda.get_active_snapshot", return_value=snapshot),
            patch.object(agenda.manager, "get_today_agenda", new=AsyncMock(return_value=None)),
            patch.object(agenda, "get_legacy_inner_state", new=AsyncMock(return_value=state)),
            patch.object(agenda.manager, "get_recent_summaries", new=AsyncMock(return_value=[])),
            patch.object(agenda.manager, "save_daily_agenda", new=AsyncMock()),
            patch.object(agenda.llm_client, "lightweight_chat", new=AsyncMock(return_value="{}")) as chat,
        ):
            asyncio.run(agenda.generate_daily_agenda(self.context))

        prompt = chat.await_args.args[0]
        self.assertIn("快照角色", prompt)
        self.assertNotIn("未设置", prompt)


if __name__ == "__main__":
    unittest.main()
