from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.character_context import character_scope
from persona.dossier import list_snapshots
from persona.review_cases import PersonaReviewStore
from scripts import character_registry
from scripts.seed_ji_yu import seed_ji_yu
from scripts.seed_ji_yu_v6_candidate import seed_candidate


class JiYuV6CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {"SOUL_MATE_ROOT": str(self.root), "DATA_DIR": str(self.root / "data")},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        registry_file = self.root / "data" / "registry" / "characters.json"
        self.patches = [
            patch.object(character_registry, "_REGISTRY_DIR", registry_file.parent),
            patch.object(character_registry, "_REGISTRY_FILE", registry_file),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def test_candidate_is_idempotent_reviewable_and_not_activated(self) -> None:
        seed_ji_yu(activate=True)
        first = seed_candidate()
        second = seed_candidate()
        self.assertEqual(first["candidate_snapshot_id"], second["candidate_snapshot_id"])
        self.assertFalse(first["candidate_is_active"])
        self.assertEqual(first["review_case_count"], 40)
        self.assertGreaterEqual(first["profile_diff_count"], 10)

        context = character_registry.get_character_context("ji-yu")
        assert context is not None
        with character_scope(context):
            snapshots = list_snapshots()
        active = next(item for item in snapshots if item["is_active"])
        candidate = next(item for item in snapshots if item["id"] == first["candidate_snapshot_id"])
        self.assertEqual(active["version"], 5)
        self.assertEqual(candidate["version"], 6)
        self.assertIn("美术相关双学位", candidate["stable_profile"]["education"])
        self.assertIn("高标准又逞强", candidate["stable_profile"]["flaws"])

        review = PersonaReviewStore(context.workspace_dir / "soul_mate.db").list_cases(
            str(candidate["id"])
        )
        self.assertEqual(review["summary"]["total"], 40)
        self.assertEqual(review["summary"]["rated"], 0)
        self.assertFalse(review["summary"]["passes_85_percent"])


if __name__ == "__main__":
    unittest.main()
