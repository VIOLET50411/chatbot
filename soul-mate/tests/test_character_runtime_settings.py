from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.global_runtime_prompt import (
    get_active_character_prompt,
    list_character_prompt_versions,
    restore_character_prompt_version,
    save_active_character_prompt,
)
from core.runtime_settings import RuntimeSettingsStore


class CharacterRuntimeSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_model_and_key_are_independent_per_character(self) -> None:
        store = RuntimeSettingsStore(self.root / "runtime-settings.db")
        store.set_character_many(
            "default",
            {"DEEPSEEK_MODEL": "du-model", "DEEPSEEK_API_KEY": "du-secret-key"},
            actor="owner",
        )
        store.set_character_many(
            "ji-yu",
            {"DEEPSEEK_MODEL": "ji-model", "DEEPSEEK_API_KEY": "ji-secret-key"},
            actor="owner",
        )
        self.assertEqual(store.get_character("default", "DEEPSEEK_MODEL"), "du-model")
        self.assertEqual(store.get_character("ji-yu", "DEEPSEEK_MODEL"), "ji-model")
        self.assertEqual(store.get_character("default", "DEEPSEEK_API_KEY"), "du-secret-key")
        self.assertEqual(store.get_character("ji-yu", "DEEPSEEK_API_KEY"), "ji-secret-key")
        raw = store.database_path.read_bytes()
        self.assertNotIn(b"du-secret-key", raw)
        self.assertNotIn(b"ji-secret-key", raw)

    def test_missing_character_override_uses_global_migration_source(self) -> None:
        store = RuntimeSettingsStore(self.root / "runtime-settings.db")
        store.set_many({"VISION_MODEL": "global-vision"}, actor="owner")
        self.assertEqual(store.get_character("ji-yu", "VISION_MODEL"), "global-vision")
        store.set_character_many("ji-yu", {"VISION_MODEL": "ji-vision"}, actor="owner")
        self.assertEqual(store.get_character("ji-yu", "VISION_MODEL"), "ji-vision")
        self.assertEqual(store.get_character("default", "VISION_MODEL"), "global-vision")

    def test_character_prompt_versions_are_isolated_and_restorable(self) -> None:
        prompt_database = self.root / "runtime-prompt.db"
        with patch("core.global_runtime_prompt._database_path", return_value=prompt_database):
            du_v1 = save_active_character_prompt("default", "杜佳颖运行补充 v1", created_by="owner")
            save_active_character_prompt("default", "杜佳颖运行补充 v2", created_by="owner")
            save_active_character_prompt("ji-yu", "纪雨运行补充", created_by="owner")
            self.assertEqual(get_active_character_prompt("default"), "杜佳颖运行补充 v2")
            self.assertEqual(get_active_character_prompt("ji-yu"), "纪雨运行补充")
            restored = restore_character_prompt_version("default", du_v1.id, created_by="owner")
            self.assertEqual(restored.prompt, "杜佳颖运行补充 v1")
            self.assertEqual(len(list_character_prompt_versions("default")), 3)
            self.assertEqual(len(list_character_prompt_versions("ji-yu")), 1)


if __name__ == "__main__":
    unittest.main()
