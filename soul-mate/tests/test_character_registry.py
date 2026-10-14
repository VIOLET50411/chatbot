import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import character_registry


class CharacterRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        root = Path(self.temporary_dir.name)
        self.registry_file = root / "registry" / "characters.json"
        self.patches = [
            patch.object(character_registry, "_REGISTRY_DIR", self.registry_file.parent),
            patch.object(character_registry, "_REGISTRY_FILE", self.registry_file),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def test_legacy_default_is_upgraded_without_changing_workspace(self):
        self.registry_file.parent.mkdir(parents=True)
        self.registry_file.write_text(
            json.dumps([{"id": "default", "display_name": "杜佳颖", "data_dir": "data"}], ensure_ascii=False),
            encoding="utf-8",
        )

        record = character_registry.get_single_character()

        self.assertEqual(record["id"], "default")
        self.assertEqual(record["data_dir"], "data")
        self.assertEqual(record["account_id"], "du-jiaying")
        saved = json.loads(self.registry_file.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["persona_status"], "active")

    def test_two_characters_have_unique_accounts_and_workspaces(self):
        character_registry.get_single_character()
        created = character_registry.upsert_character(
            {
                "id": "ji-yu",
                "display_name": "纪雨",
                "data_dir": "data/characters/ji-yu",
                "account_id": "ji-yu",
                "enabled": True,
                "persona_status": "draft",
            }
        )

        self.assertEqual(created["display_name"], "纪雨")
        self.assertEqual(len(character_registry.list_characters()), 2)
        self.assertEqual(character_registry.get_character_by_account("ji-yu")["id"], "ji-yu")
        self.assertEqual(character_registry.get_single_character()["id"], "default")

    def test_duplicate_account_or_workspace_is_rejected(self):
        default = character_registry.get_single_character()
        with self.assertRaisesRegex(ValueError, "微信账号 ID 不能重复绑定"):
            character_registry.upsert_character(
                {
                    "id": "ji-yu",
                    "display_name": "纪雨",
                    "data_dir": "data/characters/ji-yu",
                    "account_id": default["account_id"],
                }
            )
        with self.assertRaisesRegex(ValueError, "不能共用同一工作空间"):
            character_registry.upsert_character(
                {
                    "id": "ji-yu",
                    "display_name": "纪雨",
                    "data_dir": default["data_dir"],
                    "account_id": "ji-yu",
                }
            )


if __name__ == "__main__":
    unittest.main()
