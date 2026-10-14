import gc
import os
import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from core.character_context import character_scope
from persona.dossier import get_active_snapshot, list_snapshots
from scripts import character_registry
from scripts.seed_ji_yu import seed_ji_yu


class JiYuSeedTests(unittest.TestCase):
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

    def test_seed_creates_one_active_snapshot_and_is_idempotent(self):
        first = seed_ji_yu()
        second = seed_ji_yu()
        context = character_registry.get_character_context("ji-yu")
        self.assertIsNotNone(context)
        with character_scope(context):
            active = get_active_snapshot()
            snapshots = list_snapshots()

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        seeded = [item for item in snapshots if str(item.get("note", "")).startswith("纪雨 v1")]
        self.assertEqual(len(seeded), 1)
        self.assertEqual(active["stable_profile"]["name"], "纪雨")
        self.assertIn("美术相关双学位", active["stable_profile"]["education"])
        self.assertEqual(active["stable_profile"]["birth_date"], "2005-07-17")
        self.assertIn("与 Owner 同年", active["stable_profile"]["age"])
        self.assertEqual(character_registry.get_character("ji-yu")["persona_status"], "active")
        with sqlite3.connect(first["database_path"]) as connection:
            pending = connection.execute(
                "SELECT COUNT(*) FROM material_reviews WHERE decision='pending'"
            ).fetchone()[0]
        self.assertEqual(pending, 0)


if __name__ == "__main__":
    unittest.main()
