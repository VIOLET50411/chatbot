from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bridge.v1_api import router
from scripts import character_registry
from scripts.wxautox_bridge_agent import _load_account_configs_from_payload


class WechatBindingCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        root = Path(self.temporary_dir.name)
        registry_file = root / "data" / "registry" / "characters.json"
        self.patches = [
            patch.dict(
                os.environ,
                {
                    "DATA_DIR": str(root / "data"),
                    "WECHAT_BRIDGE_TOKEN": "wechat-binding-test-token",
                },
                clear=False,
            ),
            patch.object(character_registry, "_REGISTRY_DIR", registry_file.parent),
            patch.object(character_registry, "_REGISTRY_FILE", registry_file),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)
        current = character_registry.get_single_character()
        character_registry.upsert_character(
            {
                **current,
                "persona_status": "active",
                "wechat_window_nickname": "Sakino",
                "wechat_id": "Sakino411",
                "owner_remark": "VIOLET",
                "binding_enabled": True,
                "identity_confirmed": True,
            }
        )
        character_registry.upsert_character(
            {
                "id": "ji-yu",
                "display_name": "纪雨",
                "data_dir": str(root / "data" / "characters" / "ji-yu"),
                "account_id": "ji-yu",
                "account_label": "Locin（微信号 Theonlywan1）",
                "persona_status": "active",
                "enabled": True,
                "wechat_window_nickname": "Locin",
                "wechat_id": "Theonlywan1",
                "owner_remark": "VIOLET",
                "binding_enabled": True,
                "identity_confirmed": True,
            }
        )
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_bridge_catalog_returns_only_approved_versioned_bindings(self) -> None:
        response = self.client.get(
            "/api/bridge/v1/accounts",
            headers={"Authorization": "Bearer wechat-binding-test-token"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["configuration_version"]), 24)
        self.assertEqual(
            [
                (item["account_id"], item["window_nickname"], item["wechat_id"])
                for item in payload["accounts"]
            ],
            [
                ("du-jiaying", "Sakino", "Sakino411"),
                ("ji-yu", "Locin", "Theonlywan1"),
            ],
        )

    def test_host_rejects_duplicate_window_binding(self) -> None:
        payload = {
            "configuration_version": "v1",
            "accounts": [
                {
                    "account_id": "du-jiaying",
                    "character_id": "default",
                    "window_nickname": "同一窗口",
                    "wechat_id": "Sakino411",
                    "owner_remark": "VIOLET",
                    "binding_version": 1,
                },
                {
                    "account_id": "ji-yu",
                    "character_id": "ji-yu",
                    "window_nickname": "同一窗口",
                    "wechat_id": "Theonlywan1",
                    "owner_remark": "VIOLET",
                    "binding_version": 1,
                },
            ],
        }
        with self.assertRaisesRegex(ValueError, "窗口昵称不能重复"):
            _load_account_configs_from_payload(payload)

    def test_binding_update_uses_version_and_keeps_account_id(self) -> None:
        before = character_registry.get_wechat_binding("ji-yu")
        self.assertIsNotNone(before)
        assert before is not None
        after = character_registry.upsert_wechat_binding(
            "ji-yu",
            {
                "window_nickname": "Locin 新窗口",
                "wechat_id": "Theonlywan1",
                "owner_remark": "VIOLET",
                "enabled": True,
                "identity_confirmed": True,
            },
            expected_version=int(before["binding_version"]),
        )
        self.assertEqual(after["account_id"], "ji-yu")
        self.assertEqual(after["window_nickname"], "Locin 新窗口")
        self.assertEqual(after["binding_version"], int(before["binding_version"]) + 1)
        with self.assertRaisesRegex(RuntimeError, "刷新后重试"):
            character_registry.upsert_wechat_binding(
                "ji-yu",
                after,
                expected_version=int(before["binding_version"]),
            )


if __name__ == "__main__":
    unittest.main()
