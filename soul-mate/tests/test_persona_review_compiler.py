from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from bridge.v1_store import BridgeStore
from core.character_context import CharacterContext, character_scope
from persona.dossier import ensure_dossier_schema
from persona.review_compiler import BaselineCompileError, PersonaReviewCompiler
from scripts.init_db import init


class PersonaReviewCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.workspace = Path(self.temporary_dir.name) / "data"
        init(self.workspace)
        self.context = CharacterContext(
            character_id="default",
            display_name="测试角色",
            workspace_dir=self.workspace,
        )
        with character_scope(self.context):
            ensure_dossier_schema()
        self.store = BridgeStore(self.workspace / "soul_mate.db")
        self.store.initialize_schema()
        self.contact = self.store.create_contact(external_id="wx-test", display_name="测试联系人")
        self._seed_review_data()
        self.compiler_patch = patch(
            "persona.review_compiler.get_single_character",
            return_value={"id": "default"},
        )
        self.compiler_patch.start()
        self.addCleanup(self.compiler_patch.stop)
        self.compiler = PersonaReviewCompiler(self.store)

    def _seed_review_data(self) -> None:
        now = "2026-08-07T00:00:00+00:00"
        materials = [
            ("m-name", "canonical_setup", "name: 角色甲", "keep"),
            ("m-pet", "canonical_setup", "pet: 猫，叫旧名字", "exclude"),
            (
                "m-personality",
                "canonical_setup",
                "personality: 旧性格",
                "corrected",
            ),
            ("m-chat", "chat", "真实聊天", "keep"),
            ("m-moment-keep", "moments", "保留的朋友圈", "keep"),
            ("m-moment-exclude", "moments", "真实但不单独入人设的朋友圈", "exclude"),
            ("m-relationship", "canonical_setup", "relationship_with_user: 旧关系", "keep"),
        ]
        claims = [
            ("c-name", "identity", "name", "角色甲", {}, "m-name"),
            ("c-pet", "preference", "pet", "猫，叫旧名字", {}, "m-pet"),
            ("c-personality", "value", "personality", "旧性格", {}, "m-personality"),
            (
                "c-chat-style",
                "communication",
                "chat_style_profile",
                json.dumps(
                    {
                        "common_words": ["老师", "哈哈哈"],
                        "emoji_habits": ["^ ^"],
                        "sentence_style": "旧标点规则",
                        "special_expressions": ["好羡慕"],
                        "tone": "轻松",
                    },
                    ensure_ascii=False,
                ),
                {"channel": "wechat_chat"},
                "m-chat",
            ),
            (
                "c-moments-style",
                "communication",
                "moments_style_profile",
                json.dumps(
                    {
                        "common_words": ["最近", "绝望"],
                        "emoji_habits": ["……"],
                        "sentence_style": "极简留白",
                        "special_expressions": ["好绝望"],
                        "tone": "短句",
                    },
                    ensure_ascii=False,
                ),
                {"channel": "moments"},
                "m-moment-keep",
            ),
            (
                "c-relationship",
                "identity",
                "relationship_with_user",
                "旧关系",
                {},
                "m-relationship",
            ),
        ]
        with closing(sqlite3.connect(self.workspace / "soul_mate.db")) as connection:
            connection.execute(
                """
                UPDATE material_reviews
                SET decision='exclude', reviewed_by='owner', reviewed_at=?, updated_at=?
                WHERE decision='pending'
                """,
                (now, now),
            )
            for material_id, source_type, content, decision in materials:
                connection.execute(
                    """
                    INSERT INTO source_material(
                        id, character_id, source_type, content, source_time,
                        import_meta_json, reliability, scope, content_hash,
                        is_deleted, created_at
                    ) VALUES (?, 'default', ?, ?, ?, '{}', 1, 'persona', ?, 0, ?)
                    """,
                    (material_id, source_type, content, now, material_id, now),
                )
                correction = "personality: 新性格" if decision == "corrected" else None
                channel = "chat" if source_type == "chat" else "moments" if source_type == "moments" else "identity"
                connection.execute(
                    """
                    INSERT INTO material_reviews(
                        id, source_material_id, source_channel, decision,
                        corrected_content, evidence_json, reviewed_by, reviewed_at,
                        created_at, updated_at, review_note, split_contents_json
                    ) VALUES (?, ?, ?, ?, ?, '[]', 'owner', ?, ?, ?, '', '[]')
                    """,
                    (f"review:{material_id}", material_id, channel, decision, correction, now, now, now),
                )
            for claim_id, facet, key, value, context, source_id in claims:
                connection.execute(
                    """
                    INSERT INTO persona_claim(
                        id, character_id, facet, claim_key, claim_value,
                        context_json, polarity, confidence, status, origin,
                        reviewed_at, reviewed_by
                    ) VALUES (?, 'default', ?, ?, ?, ?, 'supports', 1, 'approved', 'test', ?, 'owner')
                    """,
                    (claim_id, facet, key, value, json.dumps(context), now),
                )
                connection.execute(
                    """
                    INSERT INTO claim_evidence(
                        id, claim_id, source_material_id, quote_span_json,
                        support_kind, evidence_weight, created_at
                    ) VALUES (?, ?, ?, '{}', 'supports', 1, ?)
                    """,
                    (f"e-{claim_id}", claim_id, source_id, now),
                )
            connection.execute(
                "INSERT INTO claim_evidence VALUES (?, ?, ?, '{}', 'supports', 1, ?)",
                ("e-moment-excluded", "c-moments-style", "m-moment-exclude", now),
            )
            connection.execute(
                "INSERT INTO contact_profiles VALUES (?, 'legacy_private_blob', '角色不应知道的内容', 1, ?)",
                (self.contact.id, now),
            )
            connection.commit()

    def _resolution(self) -> dict:
        return {
            "contact_id": self.contact.id,
            "addressing_style": "不固定称呼，按语境自然决定",
            "punctuation_style": "通常不用标点，只有无奈或停顿时用省略号",
            "relationship_baseline": "普通同学，不预设暧昧，关系随真实聊天发展",
            "known_partner_status": "当前认为对方有女朋友，后续以较新的可靠信息为准",
            "education_goal": "内心确定的保研目标是北京大学",
            "education_disclosure": "不会主动告诉普通同学",
            "moments_excluded_are_style_evidence": True,
            "pet_status": "目前没有宠物，以后可能会养",
            "last_shared_context": "2026年7月30日聊过学习和保研",
        }

    def test_draft_respects_review_decisions_without_changing_active_snapshot(self) -> None:
        before = self.compiler.get_state()["active_snapshot"]
        state = self.compiler.create_draft(self._resolution(), actor="owner", note="预览")
        draft = state["draft"]
        self.assertEqual(state["active_snapshot"], before)
        self.assertEqual(draft["status"], "draft")
        self.assertGreater(len(draft["profile_diff"]), 0)

        with closing(sqlite3.connect(self.workspace / "soul_mate.db")) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT stable_profile_json, channel_styles_json FROM persona_baseline_drafts WHERE id=?",
                (draft["id"],),
            ).fetchone()
            profile = json.loads(row["stable_profile_json"])
            styles = json.loads(row["channel_styles_json"])
            active_count = connection.execute(
                "SELECT COUNT(*) FROM persona_snapshot WHERE is_active=1"
            ).fetchone()[0]

        self.assertEqual(active_count, 1)
        self.assertEqual(profile["name"], "角色甲")
        self.assertEqual(profile["personality"], "新性格")
        self.assertEqual(profile["pet"], "目前没有宠物，以后可能会养")
        self.assertNotIn("relationship_with_user", profile)
        chat_style = styles["wechat_chat"]["style"]
        self.assertNotIn("老师", chat_style["common_words"])
        self.assertIn("通常不用标点", chat_style["punctuation_style"])
        self.assertEqual(styles["moments"]["style"]["common_words"], ["最近", "绝望"])

    def test_activation_is_transactional_and_sanitizes_contact_profile(self) -> None:
        created = self.compiler.create_draft(self._resolution(), actor="owner")
        draft = created["draft"]
        original_review_count = sum(created["review_summary"].values())
        result = self.compiler.activate_draft(draft["id"], actor="owner")
        self.assertEqual(result["active_snapshot"]["version"], 2)
        self.assertEqual(result["draft"]["status"], "activated")
        self.assertGreater(len(result["draft"]["profile_diff"]), 0)
        self.assertGreater(len(result["draft"]["style_diff"]), 0)
        self.assertGreater(len(result["draft"]["contact_profile_diff"]), 0)
        self.assertEqual(sum(result["review_summary"].values()), original_review_count)
        self.assertEqual(result["owner_confirmation_count"], 6)

        with closing(sqlite3.connect(self.workspace / "soul_mate.db")) as connection:
            statuses = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT id, status FROM persona_claim WHERE id IN ('c-pet','c-personality','c-chat-style','c-moments-style','c-relationship')"
                ).fetchall()
            }
            profile = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT key, value FROM contact_profiles WHERE contact_id=?",
                    (self.contact.id,),
                ).fetchall()
            }
            pending = connection.execute(
                "SELECT COUNT(*) FROM material_reviews WHERE decision='pending'"
            ).fetchone()[0]

        self.assertTrue(all(status == "superseded" for status in statuses.values()))
        self.assertNotIn("legacy_private_blob", profile)
        self.assertIn("当前关系基线", profile)
        self.assertEqual(pending, 0)

    def test_changed_reviews_block_activation(self) -> None:
        draft = self.compiler.create_draft(self._resolution(), actor="owner")["draft"]
        with closing(sqlite3.connect(self.workspace / "soul_mate.db")) as connection:
            connection.execute(
                "UPDATE material_reviews SET review_note='changed', updated_at='2026-08-08' WHERE id='review:m-name'"
            )
            connection.commit()
        with self.assertRaises(BaselineCompileError):
            self.compiler.activate_draft(draft["id"], actor="owner")
        self.assertEqual(self.compiler.get_state()["active_snapshot"]["version"], 1)


if __name__ == "__main__":
    unittest.main()
