from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bridge.v1_store import BridgeStore
from persona.dossier_schema import DOSSIER_SCHEMA
from persona.evolution import (
    EvolutionEvidence,
    EvolutionPolicyError,
    PersonaEvolutionService,
)


class PersonaEvolutionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.database_path = Path(self.temporary_dir.name) / "soul_mate.db"
        self.store = BridgeStore(self.database_path)
        self.store.initialize_schema()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(DOSSIER_SCHEMA)
            connection.execute(
                """
                INSERT INTO persona_snapshot(
                    id, character_id, version, snapshot_kind, approved_claims_hash,
                    stable_profile_json, channel_styles_json, claim_ids_json, note,
                    is_active, is_read_only, is_published, created_at, published_at, activated_at
                )
                VALUES (
                    'baseline', 'default', 1, 'reviewed', 'hash-1', ?, '{}', '[]',
                    '测试基线', 1, 1, 1, ?, ?, ?
                )
                """,
                (
                    json.dumps(
                        {
                            "identity": {"name": "杜佳颖"},
                            "preferences": {"drink": "乌龙茶"},
                        },
                        ensure_ascii=False,
                    ),
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO persona_baseline_drafts(
                    id, character_id, baseline_snapshot_id, result_snapshot_id,
                    review_fingerprint, resolution_json, stable_profile_json,
                    channel_styles_json, selected_claim_ids_json,
                    derived_claims_json, supersede_claim_ids_json, contact_id,
                    contact_delete_keys_json, contact_profile_json, status,
                    note, created_by, created_at, updated_at, activated_at
                ) VALUES (
                    'baseline-draft', 'default', 'baseline', 'baseline', 'fingerprint',
                    '{}', '{}', '{}', '[]', '[]', '[]', NULL, '[]', '{}',
                    'activated', '测试已激活基线', 'owner', ?, ?, ?
                )
                """,
                (
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.commit()
        self.current_time = datetime(2026, 3, 1, tzinfo=timezone.utc)
        self.service = PersonaEvolutionService(self.store, clock=lambda: self.current_time)

    def _evidence(self, *, all_commands: bool = False) -> list[EvolutionEvidence]:
        kinds = ["user_command", "user_command", "user_command"] if all_commands else [
            "conversation_observation",
            "conversation_observation",
            "life_schedule_observation",
        ]
        return [
            EvolutionEvidence("evidence-1", self.current_time - timedelta(days=20), kinds[0]),
            EvolutionEvidence("evidence-2", self.current_time - timedelta(days=10), kinds[1]),
            EvolutionEvidence("evidence-3", self.current_time - timedelta(days=5), kinds[2]),
        ]

    def _resume(self) -> None:
        self.service.set_paused(False, reason="素材审核已经完成", actor="owner")

    def _candidate(self, *, after: object | None = None) -> dict[str, object]:
        return self.service.create_candidate(
            dimension="preferences",
            after=after if after is not None else {"drink": "茉莉花茶"},
            evidence=self._evidence(),
            confidence=0.91,
            reason="跨多次自然互动形成了稳定偏好",
        )

    def test_default_pause_blocks_candidate_creation(self) -> None:
        self.assertTrue(self.service.get_control()["paused"])
        with self.assertRaisesRegex(EvolutionPolicyError, "已暂停"):
            self._candidate()

    def test_identity_and_weak_evidence_are_rejected(self) -> None:
        self._resume()
        with self.assertRaisesRegex(EvolutionPolicyError, "身份事实"):
            self.service.create_candidate(
                dimension="identity",
                after={"name": "另一个名字"},
                evidence=self._evidence(),
                confidence=0.99,
                reason="身份变更测试",
            )
        with self.assertRaisesRegex(EvolutionPolicyError, "至少需要 3"):
            self.service.create_candidate(
                dimension="habits",
                after={"sleep": "早睡"},
                evidence=self._evidence()[:2],
                confidence=0.9,
                reason="证据数量测试",
            )
        short_span = [
            EvolutionEvidence(f"short-{index}", self.current_time - timedelta(days=index), "observation")
            for index in range(3)
        ]
        with self.assertRaisesRegex(EvolutionPolicyError, "14 天"):
            self.service.create_candidate(
                dimension="habits",
                after={"sleep": "早睡"},
                evidence=short_span,
                confidence=0.9,
                reason="证据跨度测试",
            )
        with self.assertRaisesRegex(EvolutionPolicyError, "0.85"):
            self.service.create_candidate(
                dimension="habits",
                after={"sleep": "早睡"},
                evidence=self._evidence(),
                confidence=0.84,
                reason="置信度测试",
            )

    def test_user_commands_cannot_be_the_only_trigger(self) -> None:
        self._resume()
        with self.assertRaisesRegex(EvolutionPolicyError, "用户命令"):
            self.service.create_candidate(
                dimension="personality_weights",
                after={"warmth": 0.8},
                evidence=self._evidence(all_commands=True),
                confidence=0.95,
                reason="用户直接命令测试",
            )

    def test_publish_clones_active_snapshot_and_rollback_restores_baseline(self) -> None:
        self._resume()
        candidate = self._candidate()
        published = self.service.publish_candidate(str(candidate["id"]), actor="system")
        self.assertEqual(published["status"], "published")
        self.assertTrue(published["result_snapshot_id"])
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            active = connection.execute(
                "SELECT * FROM persona_snapshot WHERE character_id='default' AND is_active=1"
            ).fetchone()
            profile = json.loads(active["stable_profile_json"])
            self.assertEqual(active["version"], 2)
            self.assertEqual(profile["identity"], {"name": "杜佳颖"})
            self.assertEqual(profile["preferences"], {"drink": "茉莉花茶"})

        rolled_back = self.service.rollback(str(candidate["id"]), actor="owner")
        self.assertEqual(rolled_back["status"], "rolled_back")
        with closing(sqlite3.connect(self.database_path)) as connection:
            active_id = connection.execute(
                "SELECT id FROM persona_snapshot WHERE character_id='default' AND is_active=1"
            ).fetchone()[0]
        self.assertEqual(active_id, "baseline")

    def test_publish_rate_limit_is_seven_days(self) -> None:
        self._resume()
        first = self._candidate()
        self.service.publish_candidate(str(first["id"]), actor="system")
        self.current_time += timedelta(days=1)
        second = self._candidate(after={"drink": "白茶"})
        with self.assertRaisesRegex(EvolutionPolicyError, "每 7 天"):
            self.service.publish_candidate(str(second["id"]), actor="system")


if __name__ == "__main__":
    unittest.main()
