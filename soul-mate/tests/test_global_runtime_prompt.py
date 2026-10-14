import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.global_runtime_prompt import (
    get_active_prompt,
    list_prompt_versions,
    restore_prompt_version,
    save_active_prompt,
)


class GlobalRuntimePromptTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.root = Path(self.temporary_dir.name)
        self.environment = patch.dict(
            os.environ,
            {"SOUL_MATE_ROOT": str(self.root), "DATA_DIR": str(self.root / "data")},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_save_history_and_restore_create_auditable_versions(self):
        self.assertEqual(get_active_prompt(), "")
        first = save_active_prompt("不要使用客服式套话", created_by="owner")
        second = save_active_prompt("可以自然混用 token 和 city walk", created_by="owner")
        restored = restore_prompt_version(first.id, created_by="owner")

        self.assertEqual(get_active_prompt(), "不要使用客服式套话")
        self.assertEqual(restored.restored_from_id, first.id)
        self.assertEqual(len(list_prompt_versions()), 3)
        self.assertFalse(first.id == second.id == restored.id)

    def test_same_prompt_is_idempotent_and_length_is_bounded(self):
        first = save_active_prompt("保持自然", created_by="owner")
        same = save_active_prompt("保持自然", created_by="owner")
        self.assertEqual(first.id, same.id)
        with self.assertRaisesRegex(ValueError, "12000"):
            save_active_prompt("x" * 12_001, created_by="owner")


if __name__ == "__main__":
    unittest.main()
