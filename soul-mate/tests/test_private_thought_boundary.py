from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.character_context import CharacterContext, character_scope
from memory.models import DailyAgenda, InnerState
from scripts.init_db import init


class PrivateThoughtModelBoundaryTests(unittest.TestCase):
    def test_compatibility_and_agenda_models_cannot_carry_private_thought(self) -> None:
        state = InnerState()
        agenda = DailyAgenda(
            date="2026-07-28",
            morning="整理房间",
            afternoon="看电影",
            evening="散步",
            mood_note="平静",
            possible_share=["新电影"],
        )

        self.assertNotIn("private_thought", InnerState.__dataclass_fields__)
        self.assertNotIn("agenda", InnerState.__dataclass_fields__)
        self.assertNotIn("private_thought", DailyAgenda.__dataclass_fields__)
        with self.assertRaises(AttributeError):
            state.private_thought = "不应进入运行态"
        with self.assertRaises(AttributeError):
            agenda.private_thought = "不应进入日程"
        with self.assertRaises(TypeError):
            DailyAgenda(
                date="2026-07-28",
                morning="",
                afternoon="",
                evening="",
                mood_note="",
                possible_share=[],
                private_thought="不应接受隐藏字段",
            )

    def test_compatibility_projection_does_not_accept_an_agenda_payload(self) -> None:
        from memory.interaction_service import get_legacy_inner_state

        context = CharacterContext(
            character_id="role-a",
            display_name="角色甲",
            workspace_dir=Path(tempfile.mkdtemp()) / "role-a",
        )
        self.addCleanup(lambda: _remove_tree(context.workspace_dir.parent))
        init(context.workspace_dir)

        with self.assertRaises(TypeError):
            asyncio.run(
                get_legacy_inner_state(
                    context,
                    agenda={"private_thought": "不应进入兼容投影"},
                )
            )


class AgendaStorageMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_dir.cleanup)
        self.context = CharacterContext(
            character_id="role-a",
            display_name="角色甲",
            workspace_dir=Path(self._temporary_dir.name) / "characters" / "role-a",
        )
        init(self.context.workspace_dir)

    def test_migration_removes_legacy_private_column_and_preserves_public_agenda(self) -> None:
        database = self.context.workspace_dir / "soul_mate.db"
        today = datetime.now().strftime("%Y-%m-%d")
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("DROP TABLE daily_agenda")
            connection.execute("""
                CREATE TABLE daily_agenda (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL UNIQUE,
                    morning TEXT,
                    afternoon TEXT,
                    evening TEXT,
                    mood_note TEXT,
                    possible_share TEXT,
                    private_thought TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            connection.execute("""
                INSERT INTO daily_agenda
                (date, morning, afternoon, evening, mood_note, possible_share, private_thought)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                today,
                "整理房间",
                "看电影",
                "散步",
                "平静",
                '["新电影"]',
                "仅旧表中存在的隐藏内容",
            ))
            connection.commit()

        init(self.context.workspace_dir)

        with closing(sqlite3.connect(database)) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(daily_agenda)").fetchall()
            }
            migration = connection.execute(
                "SELECT id FROM schema_migrations WHERE id=?",
                ("remove_daily_agenda_private_thought_v1",),
            ).fetchone()
        self.assertNotIn("private_thought", columns)
        self.assertIsNotNone(migration)

        async def load_agenda() -> DailyAgenda | None:
            from memory import manager

            with character_scope(self.context):
                return await manager.get_today_agenda()

        agenda = asyncio.run(load_agenda())
        self.assertIsNotNone(agenda)
        assert agenda is not None
        self.assertEqual(agenda.morning, "整理房间")
        self.assertEqual(agenda.possible_share, ["新电影"])
        self.assertNotIn("private_thought", agenda.__dataclass_fields__)


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
