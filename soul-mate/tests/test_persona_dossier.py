from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.character_context import CharacterContext, character_scope
from scripts.init_db import init


class PersonaDossierReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_dir.cleanup)
        self.root = Path(self._temporary_dir.name)
        self.workspace = self.root / "characters" / "role-a"
        init(data_dir=self.workspace)
        self.context = CharacterContext(
            character_id="role-a",
            display_name="角色甲",
            workspace_dir=self.workspace,
        )

    def test_one_material_can_support_and_contradict_separate_claims(self) -> None:
        from persona.dossier import (
            add_persona_claim,
            add_source_material,
            link_claim_evidence,
            list_claims,
        )

        with character_scope(self.context):
            material = add_source_material(
                content="她说不喜欢被催消息，也说偶尔会晚睡。",
                source_type="chat",
                reliability=0.8,
            )
            boundary = add_persona_claim(
                facet="boundary",
                claim_key="message_pressure",
                claim_value="不喜欢被催消息",
            )
            habit = add_persona_claim(
                facet="habit",
                claim_key="sleep_habit",
                claim_value="作息规律",
                polarity="contradicts",
            )
            link_claim_evidence(
                claim_id=boundary["id"],
                source_material_id=material["id"],
                support_kind="supports",
                evidence_weight=0.9,
            )
            link_claim_evidence(
                claim_id=habit["id"],
                source_material_id=material["id"],
                support_kind="contradicts",
                evidence_weight=0.7,
            )
            claims = {claim["id"]: claim for claim in list_claims(include_evidence=True)}

        self.assertEqual(claims[boundary["id"]]["evidence"][0]["source_material_id"], material["id"])
        self.assertEqual(claims[boundary["id"]]["evidence"][0]["support_kind"], "supports")
        self.assertEqual(claims[habit["id"]]["evidence"][0]["support_kind"], "contradicts")

    def test_reviewed_snapshot_contains_only_approved_claims_and_can_roll_back(self) -> None:
        from persona.dossier import (
            activate_snapshot,
            add_persona_claim,
            add_source_material,
            create_reviewed_snapshot,
            get_active_snapshot,
            link_claim_evidence,
            list_snapshots,
            review_persona_claim,
        )

        with character_scope(self.context):
            legacy_snapshot = get_active_snapshot()
            self.assertIsNotNone(legacy_snapshot)

            material = add_source_material(
                content="角色名和微信聊天风格的人工设定。",
                source_type="canonical_setup",
                reliability=1.0,
            )
            name = add_persona_claim(
                facet="identity",
                claim_key="name",
                claim_value="档案中的角色名",
                origin="manual",
            )
            style = add_persona_claim(
                facet="communication",
                claim_key="style_profile",
                claim_value='{"sentence_style":"短句为主","emoji_habits":["^ ^"]}',
                context={"channel": "wechat_chat"},
                origin="manual",
            )
            discarded = add_persona_claim(
                facet="preference",
                claim_key="food",
                claim_value="甜食",
                origin="manual",
            )
            link_claim_evidence(
                claim_id=name["id"],
                source_material_id=material["id"],
                support_kind="supports",
            )
            link_claim_evidence(
                claim_id=style["id"],
                source_material_id=material["id"],
                support_kind="supports",
            )
            review_persona_claim(name["id"], "approved", actor="reviewer")
            review_persona_claim(style["id"], "approved", actor="reviewer")
            review_persona_claim(discarded["id"], "rejected", actor="reviewer")

            snapshot = create_reviewed_snapshot(note="首次人工审核", actor="reviewer")
            snapshots = list_snapshots()
            active_before_activation = get_active_snapshot()
            activated = activate_snapshot(snapshot["id"], actor="reviewer")
            rolled_back = activate_snapshot(legacy_snapshot["id"], actor="reviewer")
            active_snapshot_count = sum(item["is_active"] for item in list_snapshots())

        self.assertEqual(snapshot["kind"], "reviewed")
        self.assertTrue(snapshot["is_read_only"])
        self.assertTrue(snapshot["is_published"])
        self.assertFalse(snapshot["is_active"])
        self.assertEqual(active_before_activation["id"], legacy_snapshot["id"])
        self.assertTrue(activated["is_active"])
        self.assertEqual(activated["id"], snapshot["id"])
        self.assertEqual(snapshot["stable_profile"], {"name": "档案中的角色名"})
        self.assertEqual(
            snapshot["channel_styles"]["wechat_chat"]["style"]["sentence_style"],
            "短句为主",
        )
        self.assertNotIn(discarded["id"], snapshot["claim_ids"])
        self.assertEqual(snapshots[0]["id"], snapshot["id"])
        self.assertTrue(rolled_back["is_active"])
        self.assertTrue(rolled_back["is_read_only"])
        self.assertTrue(rolled_back["is_published"])
        self.assertEqual(rolled_back["id"], legacy_snapshot["id"])
        self.assertEqual(active_snapshot_count, 1)

        with closing(sqlite3.connect(self.workspace / "soul_mate.db")) as connection:
            actions = [row[0] for row in connection.execute(
                "SELECT action FROM persona_audit_log ORDER BY created_at, id"
            ).fetchall()]
        self.assertIn("publish", actions)
        self.assertIn("activate", actions)

    def test_active_snapshot_must_be_published_and_read_only(self) -> None:
        from persona.dossier import get_active_snapshot

        with character_scope(self.context):
            snapshot = get_active_snapshot()
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            with closing(sqlite3.connect(self.workspace / "soul_mate.db")) as connection:
                connection.execute(
                    "UPDATE persona_snapshot SET is_published=0 WHERE id=?",
                    (snapshot["id"],),
                )
                connection.commit()
            self.assertIsNone(get_active_snapshot())

    def test_editing_a_past_snapshot_creates_a_new_version_without_rewriting_history(self) -> None:
        from persona.dossier import activate_snapshot, create_edited_snapshot, get_active_snapshot, list_snapshots

        with character_scope(self.context):
            original = get_active_snapshot()
            assert original is not None
            edited = create_edited_snapshot(
                original["id"],
                stable_profile={"name": "角色甲", "boundary": "会温和拒绝不合理要求"},
                note="Owner 补充独立人格边界",
                actor="owner",
            )
            self.assertFalse(edited["is_active"])
            self.assertEqual(edited["version"], original["version"] + 1)
            self.assertEqual(get_active_snapshot()["id"], original["id"])
            activate_snapshot(edited["id"], actor="owner")
            versions = list_snapshots()

        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["stable_profile"]["name"], "角色甲")
        self.assertEqual(versions[1]["stable_profile"], original["stable_profile"])
        self.assertTrue(versions[0]["is_active"])

    def test_claim_review_state_machine_prevents_rewriting_approved_history(self) -> None:
        from persona.dossier import (
            add_persona_claim,
            add_source_material,
            link_claim_evidence,
            review_persona_claim,
        )

        with character_scope(self.context):
            material = add_source_material(
                content="候选角色名的原始依据。",
                source_type="canonical_setup",
                reliability=1.0,
            )
            claim = add_persona_claim(
                facet="identity",
                claim_key="name",
                claim_value="待审核名称",
            )
            link_claim_evidence(
                claim_id=claim["id"],
                source_material_id=material["id"],
                support_kind="supports",
            )
            approved = review_persona_claim(claim["id"], "approved")
            with self.assertRaises(ValueError):
                review_persona_claim(claim["id"], "rejected")

        self.assertEqual(approved["status"], "approved")


if __name__ == "__main__":
    unittest.main()
