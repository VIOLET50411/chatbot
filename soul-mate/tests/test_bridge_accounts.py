from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bridge.account_router import bridge_store_for_account
from bridge.v1_api import router
from core.runtime_catalog import list_character_runtimes
from scripts import character_registry


class BridgeAccountIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.root = Path(self.temporary_dir.name)
        self.environment = patch.dict(
            os.environ,
            {
                "SOUL_MATE_ROOT": str(self.root),
                "DATA_DIR": str(self.root / "data"),
                "WECHAT_BRIDGE_TOKEN": "bridge-account-test-token",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        registry_file = self.root / "data" / "registry" / "characters.json"
        self.registry_patches = [
            patch.object(character_registry, "_REGISTRY_DIR", registry_file.parent),
            patch.object(character_registry, "_REGISTRY_FILE", registry_file),
        ]
        for active_patch in self.registry_patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)
        character_registry.get_single_character()
        character_registry.upsert_character(
            {
                "id": "ji-yu",
                "display_name": "纪雨",
                "data_dir": "data/characters/ji-yu",
                "account_id": "ji-yu",
                "enabled": True,
                "persona_status": "draft",
            }
        )
        self.du_store = bridge_store_for_account("du-jiaying")
        self.ji_store = bridge_store_for_account("ji-yu")
        self.du_contact = self.du_store.create_contact(external_id="owner", display_name="Owner")
        self.ji_contact = self.ji_store.create_contact(external_id="owner", display_name="Owner")
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer bridge-account-test-token"}

    def test_same_contact_and_event_are_isolated_by_account(self) -> None:
        body = {
            "event_id": "same-event",
            "external_contact_id": "owner",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "type": "text",
            "text": "同一条测试消息",
            "attachment_refs": [],
            "message_hash": "f" * 64,
            "agent_id": "windows-primary",
        }
        du = self.client.post(
            "/api/bridge/v1/inbound",
            headers=self.headers,
            json={**body, "account_id": "du-jiaying"},
        )
        ji = self.client.post(
            "/api/bridge/v1/inbound",
            headers=self.headers,
            json={**body, "account_id": "ji-yu"},
        )

        self.assertEqual(du.status_code, 202)
        self.assertEqual(ji.status_code, 202)
        self.assertEqual(self.du_store.count_inbound(), 1)
        self.assertEqual(self.ji_store.count_inbound(), 1)

    def test_each_account_leases_only_its_own_outbox(self) -> None:
        now = datetime.now(timezone.utc)
        du_action = self.du_store.enqueue_action(
            contact_id=self.du_contact.id,
            action_type="text",
            payload={"chunks": ["杜佳颖回复"]},
            scheduled_at=now,
            dedupe_key="du-only",
        )
        ji_action = self.ji_store.enqueue_action(
            contact_id=self.ji_contact.id,
            action_type="text",
            payload={"chunks": ["纪雨回复"]},
            scheduled_at=now,
            dedupe_key="ji-only",
        )

        du = self.client.post(
            "/api/bridge/v1/outbox/lease",
            headers=self.headers,
            json={"agent_id": "windows-primary", "account_id": "du-jiaying"},
        ).json()["actions"]
        ji = self.client.post(
            "/api/bridge/v1/outbox/lease",
            headers=self.headers,
            json={"agent_id": "windows-primary", "account_id": "ji-yu"},
        ).json()["actions"]

        self.assertEqual([item["action_id"] for item in du], [du_action.action_id])
        self.assertEqual([item["action_id"] for item in ji], [ji_action.action_id])
        self.assertEqual(du[0]["account_id"], "du-jiaying")
        self.assertEqual(ji[0]["account_id"], "ji-yu")

    def test_unregistered_account_is_rejected(self) -> None:
        response = self.client.get(
            "/api/bridge/v1/contacts",
            headers={**self.headers, "X-WeChat-Account-ID": "unknown-account"},
        )
        self.assertEqual(response.status_code, 404)

    def test_runtime_catalog_excludes_draft_persona_from_message_workers(self) -> None:
        all_runtimes = list_character_runtimes()
        active_runtimes = list_character_runtimes(active_only=True)

        self.assertEqual([item.context.display_name for item in all_runtimes], ["杜佳颖", "纪雨"])
        self.assertEqual([item.context.display_name for item in active_runtimes], ["杜佳颖"])
        self.assertNotEqual(all_runtimes[0].bridge_store.database_path, all_runtimes[1].bridge_store.database_path)


if __name__ == "__main__":
    unittest.main()
