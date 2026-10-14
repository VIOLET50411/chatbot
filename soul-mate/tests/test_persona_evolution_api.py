from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from bridge.v1_store import BridgeStore
from persona.dossier_schema import DOSSIER_SCHEMA
from web.app import create_app


class PersonaEvolutionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.data_dir = Path(self.temporary_dir.name) / "data"
        self.environment = patch.dict(
            os.environ,
            {
                "DATA_DIR": str(self.data_dir),
                "ADMIN_PASSWORD": "test-admin-password",
                "ADMIN_USERNAME": "owner",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = TestClient(create_app(engine=None))
        self.addCleanup(self.client.close)
        response = self.client.post(
            "/api/auth/login",
            json={"username": "owner", "password": "test-admin-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.csrf = {"X-CSRF-Token": response.json()["csrf_token"]}

    @staticmethod
    def _seed_activated_baseline(store: BridgeStore) -> None:
        connection = sqlite3.connect(store.database_path)
        try:
            connection.executescript(DOSSIER_SCHEMA)
            connection.execute(
                """
                INSERT INTO persona_snapshot(
                    id, character_id, version, snapshot_kind, approved_claims_hash,
                    stable_profile_json, channel_styles_json, claim_ids_json, note,
                    is_active, is_read_only, is_published, created_at, published_at, activated_at
                ) VALUES ('baseline', 'default', 1, 'reviewed', 'hash', '{}', '{}', '[]',
                          '测试基线', 1, 1, 1, datetime('now'), datetime('now'), datetime('now'))
                """
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
                ) VALUES ('draft', 'default', 'baseline', 'baseline', 'fingerprint', '{}', '{}',
                          '{}', '[]', '[]', '[]', NULL, '[]', '{}', 'activated',
                          '测试已激活基线', 'owner', datetime('now'), datetime('now'), datetime('now'))
                """
            )
            connection.commit()
        finally:
            connection.close()

    def test_state_is_owner_only_and_paused_by_default(self) -> None:
        response = self.client.get("/api/persona/evolution")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["control"]["paused"])
        self.assertEqual(response.json()["events"], [])

    def test_resume_requires_csrf_and_all_materials_reviewed(self) -> None:
        blocked = self.client.patch(
            "/api/persona/evolution/control",
            json={"paused": False, "reason": "素材已经审核完成"},
        )
        self.assertEqual(blocked.status_code, 403)

        store = BridgeStore.from_environment()
        store.initialize_schema()
        with store._connection(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO material_reviews(
                    id, source_material_id, source_channel, decision, corrected_content,
                    evidence_json, reviewed_by, reviewed_at, created_at, updated_at,
                    review_note, split_contents_json
                )
                VALUES ('review:test', 'source:test', 'chat', 'pending', NULL, '[]',
                        NULL, NULL, datetime('now'), datetime('now'), '', '[]')
                """
            )
        pending = self.client.patch(
            "/api/persona/evolution/control",
            json={"paused": False, "reason": "素材已经审核完成"},
            headers=self.csrf,
        )
        self.assertEqual(pending.status_code, 422)
        self.assertIn("待审核", pending.json()["detail"])

        with store._connection(immediate=True) as connection:
            connection.execute("UPDATE material_reviews SET decision='exclude' WHERE id='review:test'")
        baseline_pending = self.client.patch(
            "/api/persona/evolution/control",
            json={"paused": False, "reason": "素材已经审核完成"},
            headers=self.csrf,
        )
        self.assertEqual(baseline_pending.status_code, 422)
        self.assertIn("新人格", baseline_pending.json()["detail"])
        self._seed_activated_baseline(store)
        resumed = self.client.patch(
            "/api/persona/evolution/control",
            json={"paused": False, "reason": "素材已经审核完成"},
            headers=self.csrf,
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(resumed.json()["paused"])

    def test_publish_requires_explicit_confirmation(self) -> None:
        response = self.client.post(
            "/api/persona/evolution/missing/publish",
            headers=self.csrf,
        )
        self.assertEqual(response.status_code, 428)


if __name__ == "__main__":
    unittest.main()
