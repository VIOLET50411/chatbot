from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core.owner_profile import OwnerProfileStore
from scripts.migrate_owner_profile_encryption import migrate as migrate_owner_profile_encryption


class OwnerProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = OwnerProfileStore(Path(self.temporary.name) / "owner-profile.db")

    def test_sensitive_source_is_encrypted_and_redacted_until_revealed(self) -> None:
        exact = "这是需要逐字保留的敏感原文"
        entry = self.store.upsert_entry(
            profile_key="private.sample",
            category="sensitive_self_report",
            title="敏感资料示例",
            content="提炼结论与原文分开",
            raw_quote=exact,
            source="owner_questionnaire",
            sensitivity="high",
            topic_tags=["敏感话题"],
        )
        self.assertTrue(entry["redacted"])
        self.assertEqual(entry["content"], "")
        self.assertNotIn(exact.encode("utf-8"), self.store.database_path.read_bytes())

        revealed = self.store.get_entry(entry["id"], reveal_sensitive=True)
        self.assertIsNotNone(revealed)
        assert revealed is not None
        self.assertEqual(revealed["raw_quote"], exact)
        self.assertEqual(revealed["content"], "提炼结论与原文分开")

    def test_legacy_local_encryption_can_be_migrated_to_stable_master_key(self) -> None:
        legacy_environment = {
            "RUNTIME_SETTINGS_MASTER_KEY": "",
            "WECHAT_BRIDGE_TOKEN": "",
            "OWNER_PASSWORD_HASH": "",
        }
        with patch.dict(os.environ, legacy_environment, clear=False):
            entry = self.store.upsert_entry(
                profile_key="private.legacy",
                category="sensitive_self_report",
                title="旧资料",
                content="需要迁移的内容",
                raw_quote="需要迁移的原文",
                source="legacy_script",
                sensitivity="high",
            )

        with patch.dict(os.environ, {"RUNTIME_SETTINGS_MASTER_KEY": "stable-test-key-material-123456789"}, clear=False):
            result = migrate_owner_profile_encryption(self.store.database_path)
            revealed = self.store.get_entry(entry["id"], reveal_sensitive=True)

        self.assertEqual(result["migrated_versions"], 1)
        self.assertIsNotNone(revealed)
        assert revealed is not None
        self.assertEqual(revealed["raw_quote"], "需要迁移的原文")

    def test_versions_restore_without_overwriting_history(self) -> None:
        first = self.store.upsert_entry(
            profile_key="fact.birth_date",
            category="explicit_fact",
            title="出生日期",
            content="2005-10-12",
            source="owner_confirmation",
        )
        second = self.store.upsert_entry(
            profile_key="fact.birth_date",
            category="explicit_fact",
            title="出生日期",
            content="临时错误值",
            source="manual_edit",
        )
        self.assertEqual(second["version"], 2)
        restored = self.store.restore_version(first["id"], 1, actor="owner")
        self.assertEqual(restored["version"], 3)
        self.assertEqual(restored["content"], "2005-10-12")
        self.assertEqual(len(self.store.list_versions(first["id"])), 3)

    def test_response_rule_is_not_misclassified_as_character_knowledge(self) -> None:
        entry = self.store.upsert_entry(
            profile_key="preference.support_style",
            category="conditional_rule",
            title="难受时的陪伴顺序",
            content="先安慰情绪，明确询问后再分析",
            source="owner_questionnaire",
            sensitivity="private",
            topic_tags=["难受", "安慰"],
        )
        unknown = self.store.prompt_context(
            character_id="ji-yu",
            current_message="今天有点难受",
            include_unknown_topics=True,
        )
        self.assertIn("Owner 已确认的回应偏好", unknown)
        self.assertIn("先安慰情绪", unknown)
        self.assertNotIn("这个人物已经知道", unknown)
        self.assertNotIn(entry["id"], {item["entry_id"] for item in self.store.list_disclosures(character_id="ji-yu")})

    def test_character_fact_disclosure_is_isolated(self) -> None:
        entry = self.store.upsert_entry(
            profile_key="fact.sample",
            category="explicit_fact",
            title="Owner 明确说过的事实",
            content="只应让确认过的人物知道",
            source="owner_confirmation",
            topic_tags=["事实"],
        )
        self.store.set_disclosure(
            character_id="ji-yu",
            entry_id=entry["id"],
            state="confirmed_known",
            evidence="真实聊天确认",
        )
        known = self.store.prompt_context(character_id="ji-yu", current_message="事实")
        other = self.store.prompt_context(character_id="default", current_message="事实")
        self.assertIn("只应让确认过的人物知道", known)
        self.assertNotIn("只应让确认过的人物知道", other)

    def test_goal_has_explicit_next_step_and_followup_policy(self) -> None:
        goal = self.store.upsert_goal(
            title="准备考研",
            detail="2026-12-19 考试",
            due_at="2026-12-19T00:00:00+08:00",
            next_step="完成今天的复习计划",
        )
        self.assertEqual(goal["status"], "active")
        self.assertIn("只听我说", goal["followup_policy"])
        self.assertEqual(len(self.store.list_goals()), 1)

    def test_private_followup_goal_is_encrypted_scoped_and_only_due_once(self) -> None:
        now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
        goal = self.store.upsert_goal(
            title="关心之前谈过的焦虑",
            detail="这段细节只允许纪雨在合适语境中看到",
            due_at=(now - timedelta(minutes=1)).isoformat(),
            next_step="自然问一句最近是否好一点",
            followup_policy="先关心，不复述成咨询报告",
            character_id="ji-yu",
            contact_id="contact-violet",
            sensitivity="high",
        )
        self.assertEqual(goal["detail"], "这段细节只允许纪雨在合适语境中看到")
        with closing(sqlite3.connect(self.store.database_path)) as connection:
            stored = connection.execute(
                "SELECT detail, encrypted FROM owner_goals WHERE id=?",
                (goal["id"],),
            ).fetchone()
        self.assertEqual(stored[1], 1)
        self.assertNotIn("这段细节", stored[0])

        due_for_ji = self.store.due_followup_goals(
            character_id="ji-yu",
            contact_id="contact-violet",
            now=now,
        )
        self.assertEqual([item["id"] for item in due_for_ji], [goal["id"]])
        self.assertEqual(
            self.store.due_followup_goals(
                character_id="default",
                contact_id="contact-violet",
                now=now,
            ),
            [],
        )
        self.store.mark_goal_followed(goal["id"], followed_at=now)
        self.assertEqual(
            self.store.due_followup_goals(
                character_id="ji-yu",
                contact_id="contact-violet",
                now=now + timedelta(minutes=1),
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
