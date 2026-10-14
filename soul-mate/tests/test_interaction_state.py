from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.character_context import CharacterContext
from core.interaction_reducer import ConversationSignal, apply_conversation, tick_state
from core.interaction_state import (
    ActivityState,
    EmotionState,
    InteractionState,
    RelationshipState,
)
from memory.models import RelationshipStage
from scripts.init_db import init


class InteractionReducerTests(unittest.TestCase):
    def test_conversation_evolution_is_repeatable_and_has_explicit_inputs(self) -> None:
        state = InteractionState(
            emotion=EmotionState(happiness=4.0, loneliness=6.0, irritability=4.0),
            relationship=RelationshipState(
                stage=RelationshipStage.PROBING,
                trust=49.5,
                intimacy=39.5,
            ),
        )
        signal = ConversationSignal(
            sentiment="positive",
            depth="normal",
            intimacy_delta=0.5,
            intimacy_context="deep_talk",
            reason="共同完成一件事",
        )
        occurred_at = datetime(2026, 7, 27, 20, 0, 0)

        first = apply_conversation(state, signal, occurred_at=occurred_at)
        second = apply_conversation(state, signal, occurred_at=occurred_at)

        self.assertEqual(first, second)
        self.assertEqual(first.reason, "conversation_evaluated")
        self.assertEqual(first.inputs["analysis_reason"], "共同完成一件事")
        self.assertEqual(first.state.relationship.stage, RelationshipStage.FAMILIAR)
        self.assertEqual(first.state.activity.last_chat_at, occurred_at)
        self.assertEqual(state.relationship.stage, RelationshipStage.PROBING)

    def test_tick_applies_late_night_context_once_without_randomness(self) -> None:
        occurred_at = datetime(2026, 7, 28, 8, 0, 0)
        state = InteractionState(
            relationship=RelationshipState(
                stage=RelationshipStage.CLOSE,
                trust=75.0,
                intimacy=80.0,
                resentment=0.5,
                intimacy_context="late_night",
            ),
            activity=ActivityState(last_chat_at=occurred_at - timedelta(hours=7)),
        )

        changed = tick_state(state, occurred_at=occurred_at)
        unchanged = tick_state(changed.state, occurred_at=occurred_at + timedelta(hours=1))

        self.assertEqual(changed.state.relationship.intimacy, 77.5)
        self.assertEqual(changed.state.relationship.intimacy_context, "normal")
        self.assertEqual(changed.state.emotion.loneliness, 3.15)
        self.assertEqual(unchanged.state.relationship.intimacy, 77.5)
        self.assertEqual(changed.inputs["stage_rule"], None)

    def test_downgrade_rule_records_the_specific_boundary(self) -> None:
        state = InteractionState(
            relationship=RelationshipState(
                stage=RelationshipStage.LOVER,
                trust=69.0,
                intimacy=90.0,
                resentment=0.0,
            )
        )

        changed = tick_state(state, occurred_at=datetime(2026, 7, 27, 12, 0, 0))

        self.assertEqual(changed.state.relationship.stage, RelationshipStage.AMBIGUOUS)
        self.assertEqual(changed.inputs["stage_rule"], "downgrade_lover")


class InteractionStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_dir.cleanup)
        self.root = Path(self._temporary_dir.name)
        self.first = CharacterContext(
            character_id="role-a",
            display_name="角色甲",
            workspace_dir=self.root / "characters" / "role-a",
        )
        self.second = CharacterContext(
            character_id="role-b",
            display_name="角色乙",
            workspace_dir=self.root / "characters" / "role-b",
        )
        init(self.first.workspace_dir)
        init(self.second.workspace_dir)

    def test_first_read_migrates_legacy_state_without_rewriting_it(self) -> None:
        from memory.interaction_state_store import (
            list_interaction_transitions,
            load_interaction_state,
        )

        with closing(sqlite3.connect(self.first.workspace_dir / "soul_mate.db")) as connection:
            connection.execute(
                "UPDATE inner_state SET happiness=?, trust=?, current_activity=? WHERE id=1",
                (8.5, 42.0, "正在看电影"),
            )
            connection.commit()

        state = load_interaction_state(self.first)
        transitions = list_interaction_transitions(self.first)

        self.assertEqual(state.version, 0)
        self.assertEqual(state.emotion.happiness, 8.5)
        self.assertEqual(state.relationship.trust, 42.0)
        self.assertEqual(state.activity.current_activity, "正在看电影")
        self.assertEqual(transitions[0].reason, "legacy_state_migration")
        with closing(sqlite3.connect(self.first.workspace_dir / "soul_mate.db")) as connection:
            legacy = connection.execute(
                "SELECT happiness, trust, current_activity FROM inner_state WHERE id=1"
            ).fetchone()
        self.assertEqual(legacy, (8.5, 42.0, "正在看电影"))

    def test_concurrent_first_read_reuses_winning_initialization_without_duplicate_audit(self) -> None:
        from memory import interaction_state_store as state_store
        from memory.interaction_state_store import (
            list_interaction_transitions,
            load_interaction_state,
        )

        barrier = Barrier(2)
        original_legacy_state = state_store._legacy_state

        def synchronize_before_initial_insert(connection: sqlite3.Connection) -> InteractionState:
            barrier.wait(timeout=5)
            return original_legacy_state(connection)

        with patch.object(
            state_store,
            "_legacy_state",
            side_effect=synchronize_before_initial_insert,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                states = list(executor.map(lambda _: load_interaction_state(self.first), range(2)))

        self.assertEqual([state.version for state in states], [0, 0])
        transitions = list_interaction_transitions(self.first)
        migrations = [
            transition
            for transition in transitions
            if transition.reason == "legacy_state_migration"
        ]
        self.assertEqual(len(migrations), 1)

    def test_commit_is_audited_versioned_and_rejects_stale_writer(self) -> None:
        from memory.interaction_state_store import (
            StateVersionConflict,
            commit_interaction_transition,
            list_interaction_transitions,
            load_interaction_state,
        )

        original = load_interaction_state(self.first)
        next_state = replace(
            original,
            emotion=replace(original.emotion, happiness=7.5),
        )
        committed = commit_interaction_transition(
            self.first,
            expected=original,
            next_state=next_state,
            reason="test_state_change",
            inputs={"source": "test"},
        )

        self.assertEqual(committed.version, 1)
        self.assertEqual(load_interaction_state(self.first).emotion.happiness, 7.5)
        with self.assertRaises(StateVersionConflict):
            commit_interaction_transition(
                self.first,
                expected=original,
                next_state=next_state,
                reason="stale_writer",
                inputs={},
            )

        transitions = list_interaction_transitions(self.first)
        self.assertEqual([item.reason for item in transitions[:2]], ["test_state_change", "legacy_state_migration"])
        self.assertEqual(transitions[0].inputs, {"source": "test"})

    def test_state_storage_isolated_by_explicit_character_context(self) -> None:
        from memory.interaction_state_store import (
            commit_interaction_transition,
            load_interaction_state,
        )

        first_state = load_interaction_state(self.first)
        first_next = replace(
            first_state,
            relationship=replace(first_state.relationship, trust=91.0),
        )
        commit_interaction_transition(
            self.first,
            expected=first_state,
            next_state=first_next,
            reason="role_a_only",
            inputs={},
        )

        second_state = load_interaction_state(self.second)

        self.assertEqual(load_interaction_state(self.first).relationship.trust, 91.0)
        self.assertEqual(second_state.relationship.trust, 20.0)
        self.assertNotEqual(self.first.workspace_dir, self.second.workspace_dir)


if __name__ == "__main__":
    unittest.main()
