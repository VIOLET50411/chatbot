import gc
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.character_context import character_scope
from persona.dossier import get_active_snapshot, list_snapshots
from scripts import character_registry
from scripts.seed_du_v7 import seed_du_v7


class DuV7SeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(self.temporary_dir.name)
        self.environment = patch.dict(
            os.environ,
            {"SOUL_MATE_ROOT": str(self.root), "DATA_DIR": str(self.root / "data")},
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

    def test_seed_is_idempotent_and_compiles_confirmed_rules(self):
        first = seed_du_v7()
        second = seed_du_v7()
        context = character_registry.get_character_context("default")
        with character_scope(context):
            active = get_active_snapshot()
            matching = [item for item in list_snapshots() if str(item.get("note", "")).startswith("杜佳颖 v7")]

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(len(matching), 1)
        self.assertIn("软件与微电子学院", active["stable_profile"]["academic"])
        self.assertIn("明确告诉她已经分手", active["stable_profile"]["relationship_update_rule"])
        self.assertIn("宁可少说", active["stable_profile"]["uncertain_reply_policy"])


if __name__ == "__main__":
    unittest.main()
