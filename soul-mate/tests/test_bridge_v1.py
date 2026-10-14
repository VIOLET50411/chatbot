from __future__ import annotations

import os
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from bridge.v1_api import router
from bridge.v1_models import InboundEnvelope
from bridge.v1_store import (
    BridgeStateConflict,
    BridgeStore,
    UnknownContact,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BridgeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.database = Path(self.temporary_dir.name) / "soul_mate.db"
        self.store = BridgeStore(self.database)
        self.store.initialize_schema()
        self.alice = self.store.create_contact(
            external_id="wxid-alice",
            display_name="Alice",
        )
        self.bob = self.store.create_contact(
            external_id="wxid-bob",
            display_name="Bob",
        )

    def _inbound(
        self,
        *,
        event_id: str = "event-1",
        external_contact_id: str = "wxid-alice",
        message_hash: str = "a" * 64,
        text: str = "在吗",
    ) -> InboundEnvelope:
        return InboundEnvelope(
            event_id=event_id,
            external_contact_id=external_contact_id,
            occurred_at=_now(),
            type="text",
            text=text,
            attachment_refs=[],
            message_hash=message_hash,
            agent_id="agent-test",
        )

    def test_schema_contains_contact_scoped_runtime_tables(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            migration = connection.execute(
                "SELECT id FROM schema_migrations WHERE id=?",
                ("bridge_contacts_outbox_v1",),
            ).fetchone()

        self.assertTrue(
            {
                "contacts",
                "contact_profiles",
                "contact_conversations",
                "contact_daily_summaries",
                "contact_memories",
                "contact_relationships",
                "inbound_events",
                "outbound_actions",
                "attachments",
                "sticker_assets",
                "web_media_provenance",
                "material_reviews",
                "persona_evolution_events",
                "bridge_heartbeats",
                "legacy_contact_migrations",
            }.issubset(tables)
        )
        self.assertEqual(migration, ("bridge_contacts_outbox_v1",))

    def test_web_media_keeps_provenance_and_stays_pending_for_review(self) -> None:
        content = b"\x89PNG\r\n\x1a\nreviewed-web-image"
        digest = hashlib.sha256(content).hexdigest()
        path = self.database.parent / "attachments" / "web" / f"{digest}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        attachment = self.store.register_attachment(
            external_contact_id=self.alice.external_id,
            kind="image",
            storage_path=f"attachments/web/{digest}.png",
            sha256=digest,
            mime_type="image/png",
            size_bytes=len(content),
        )
        item = self.store.register_web_media_provenance(
            attachment_id=attachment.id,
            source_page_url="https://example.com/post/1",
            image_url="https://cdn.example.com/image.png",
            source_platform="other",
            attribution="Example Author",
            usage_scope="chat",
            presentation_mode="private_roleplay",
            vision_summary="公开网页上的城市夜景",
        )
        self.assertEqual(item["review_status"], "pending")
        self.assertEqual(item["claim_mode"], "found_online")
        self.assertEqual(item["presentation_mode"], "private_roleplay")
        self.assertEqual(item["source_page_url"], "https://example.com/post/1")
        self.assertEqual(item["vision_summary"], "公开网页上的城市夜景")

        self.store.review_media_asset(attachment.id, review_status="approved")
        available = self.store.list_approved_media_for_contact(contact_id=self.alice.id)
        self.assertEqual(available, [{
            "kind": "image",
            "summary": "公开网页上的城市夜景",
            "presentation_mode": "private_roleplay",
        }])

        with self.assertRaisesRegex(ValueError, "只能用于"):
            self.store.register_web_media_provenance(
                attachment_id=attachment.id,
                source_page_url="https://example.com/post/1",
                image_url="https://cdn.example.com/image.png",
                source_platform="other",
                attribution="Example Author",
                usage_scope="moments",
                presentation_mode="private_roleplay",
                vision_summary="不能进入朋友圈",
            )

    def test_legacy_single_contact_data_is_copied_once_without_deleting_sources(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE user_profile (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL,
                    confirmed INTEGER DEFAULT 0, updated_at TEXT
                );
                CREATE TABLE conversations (
                    id INTEGER PRIMARY KEY, role TEXT NOT NULL,
                    content TEXT NOT NULL, created_at TEXT
                );
                CREATE TABLE daily_summaries (
                    id INTEGER PRIMARY KEY, date TEXT NOT NULL, summary TEXT NOT NULL,
                    mood_arc TEXT, key_events TEXT, relation_change TEXT, created_at TEXT
                );
                CREATE TABLE inner_state (
                    id INTEGER PRIMARY KEY, affection REAL, trust REAL, intimacy REAL,
                    resentment REAL, stage TEXT, updated_at TEXT
                );
                CREATE TABLE memory_fact (
                    id TEXT PRIMARY KEY, content TEXT, memory_type TEXT, importance INTEGER,
                    status TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE memory_fact_evidence (
                    id TEXT PRIMARY KEY, fact_id TEXT, source_message_id TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO user_profile VALUES ('name', '旧用户', 1, '2026-01-01T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO conversations VALUES (1, 'user', '旧消息', '2026-01-02T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO daily_summaries VALUES (1, '2026-01-02', '聊了旧事', NULL, '[]', NULL, '2026-01-02T23:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO inner_state VALUES (1, 12, 34, 56, 7, '熟悉朋友', '2026-01-03T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO memory_fact VALUES ('fact-1', '喜欢咖啡', 'preference', 4, 'published', '2026-01-02T00:00:00+00:00', '2026-01-03T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO memory_fact_evidence VALUES ('evidence-1', 'fact-1', 'source-1')"
            )
            connection.commit()

        first = self.store.migrate_legacy_contact(
            external_id="wxid-alice",
            display_name="Alice",
        )
        second = self.store.migrate_legacy_contact(
            external_id="wxid-alice",
            display_name="Alice",
        )

        self.assertFalse(first.already_applied)
        self.assertTrue(second.already_applied)
        self.assertEqual(first.profile_rows, 1)
        self.assertEqual(first.conversation_rows, 1)
        self.assertEqual(first.summary_rows, 1)
        self.assertEqual(first.memory_rows, 1)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM user_profile").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM contact_profiles").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM contact_conversations").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM contact_daily_summaries").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM contact_memories").fetchone()[0], 1)
            relationship = connection.execute(
                "SELECT stage, trust, intimacy, affection, resentment FROM contact_relationships WHERE contact_id=?",
                (self.alice.id,),
            ).fetchone()
        self.assertEqual(relationship, ("熟悉朋友", 34.0, 56.0, 12.0, 7.0))

    def test_contacts_can_be_listed_and_disabled_without_deleting_history(self) -> None:
        self.store.update_contact(self.alice.id, display_name="Alice 新备注", enabled=False)
        contacts = self.store.list_contacts(include_disabled=True)
        alice = next(item for item in contacts if item.id == self.alice.id)
        self.assertEqual(alice.display_name, "Alice 新备注")
        self.assertFalse(alice.enabled)
        self.assertIsNone(self.store.get_contact_by_external("wxid-alice"))
        self.assertIsNotNone(
            self.store.get_contact_by_external("wxid-alice", enabled_only=False)
        )

    def test_contact_candidate_requires_owner_confirmation_before_whitelist(self) -> None:
        self.assertEqual(self.store.record_contact_candidates(["Owner", "Alice"]), 1)
        candidates = self.store.list_contact_candidates()
        self.assertEqual([item["display_name"] for item in candidates], ["Owner"])
        self.assertIsNone(self.store.get_contact_by_external(candidates[0]["suggested_external_id"]))

        contact = self.store.review_contact_candidate(candidates[0]["id"], decision="confirm")
        self.assertIsNotNone(contact)
        assert contact is not None
        self.assertEqual(contact.display_name, "Owner")
        self.assertTrue(contact.external_id.startswith("wx:auto:"))
        self.assertEqual(self.store.list_contact_candidates(), [])

    def test_inbound_is_whitelist_scoped_and_idempotent_by_event_and_hash(self) -> None:
        first = self.store.record_inbound(self._inbound())
        duplicate_event = self.store.record_inbound(self._inbound())
        duplicate_hash = self.store.record_inbound(
            self._inbound(event_id="event-2")
        )

        self.assertEqual(first.action, "accepted")
        self.assertEqual(duplicate_event.action, "duplicate")
        self.assertEqual(duplicate_hash.action, "duplicate")
        self.assertEqual(first.contact_id, self.alice.id)
        self.assertEqual(self.store.count_inbound(contact_id=self.alice.id), 1)
        self.assertEqual(self.store.count_inbound(contact_id=self.bob.id), 0)

        with self.assertRaises(UnknownContact):
            self.store.record_inbound(
                self._inbound(external_contact_id="wxid-not-whitelisted")
            )

        with self.assertRaises(BridgeStateConflict):
            self.store.record_inbound(
                self._inbound(text="冲突内容", message_hash="b" * 64)
            )

    def test_outbox_lease_ack_and_contact_isolation(self) -> None:
        future = _now() + timedelta(minutes=5)
        alice_action = self.store.enqueue_action(
            contact_id=self.alice.id,
            action_type="text",
            payload={"chunks": ["晚点说"]},
            scheduled_at=_now(),
            dedupe_key="reply:event-1",
        )
        self.store.enqueue_action(
            contact_id=self.bob.id,
            action_type="text",
            payload={"chunks": ["未来消息"]},
            scheduled_at=future,
            dedupe_key="reply:event-bob-future",
        )

        leased = self.store.lease_actions(agent_id="agent-a", limit=10, lease_seconds=30)
        self.assertEqual([item.action_id for item in leased], [alice_action.action_id])
        self.assertEqual(leased[0].external_contact_id, "wxid-alice")
        self.assertNotIn("contact_id", leased[0].payload)

        ack = self.store.ack_action(
            action_id=alice_action.action_id,
            agent_id="agent-a",
            lease_token=leased[0].lease_token,
            provider_message_id="provider-1",
        )
        repeated_ack = self.store.ack_action(
            action_id=alice_action.action_id,
            agent_id="agent-a",
            lease_token=leased[0].lease_token,
            provider_message_id="provider-1",
        )
        self.assertEqual(ack.status, "sent")
        self.assertEqual(repeated_ack.status, "sent")
        self.assertEqual(self.store.lease_actions(agent_id="agent-a"), [])

    def test_nack_retries_only_explicit_pre_send_failures(self) -> None:
        retryable = self.store.enqueue_action(
            contact_id=self.alice.id,
            action_type="text",
            payload={"chunks": ["重试"]},
            scheduled_at=_now(),
            dedupe_key="retryable",
        )
        leased = self.store.lease_actions(agent_id="agent-a", lease_seconds=30)[0]
        failed = self.store.fail_action(
            action_id=retryable.action_id,
            agent_id="agent-a",
            lease_token=leased.lease_token,
            reason="发送前窗口不可用",
            retryable=True,
            delivery_unknown=False,
            retry_delay_seconds=0,
        )
        self.assertEqual(failed.status, "retry_wait")
        self.assertEqual(len(self.store.lease_actions(agent_id="agent-a")), 1)

        unknown_action = self.store.enqueue_action(
            contact_id=self.alice.id,
            action_type="text",
            payload={"chunks": ["不可盲重发"]},
            scheduled_at=_now(),
            dedupe_key="unknown",
        )
        leased_unknown = next(
            item
            for item in self.store.lease_actions(agent_id="agent-b", limit=10)
            if item.action_id == unknown_action.action_id
        )
        unknown = self.store.fail_action(
            action_id=unknown_action.action_id,
            agent_id="agent-b",
            lease_token=leased_unknown.lease_token,
            reason="点击发送后连接中断",
            retryable=True,
            delivery_unknown=True,
        )
        self.assertEqual(unknown.status, "delivery_unknown")
        self.assertNotIn(
            unknown_action.action_id,
            [item.action_id for item in self.store.lease_actions(agent_id="agent-b")],
        )

    def test_expired_lease_becomes_delivery_unknown_instead_of_requeued(self) -> None:
        action = self.store.enqueue_action(
            contact_id=self.alice.id,
            action_type="text",
            payload={"chunks": ["只发一次"]},
            scheduled_at=_now() - timedelta(minutes=3),
            dedupe_key="expired-lease",
        )
        leased = self.store.lease_actions(
            agent_id="agent-a",
            lease_seconds=1,
            now=_now() - timedelta(minutes=2),
        )
        self.assertEqual(leased[0].action_id, action.action_id)

        self.assertEqual(self.store.lease_actions(agent_id="agent-b", now=_now()), [])
        self.assertEqual(
            self.store.get_action(action.action_id).status,
            "delivery_unknown",
        )

    def test_moment_state_machine_is_rate_limited_and_never_blindly_replays(self) -> None:
        prepared = self.store.prepare_moment(
            text="只对测试标签可见",
            attachment_refs=[],
            privacy_tags=["SoulMate测试"],
            source="manual_test",
            now=_now(),
        )
        self.assertEqual(prepared.status, "prepared")
        with self.assertRaises(BridgeStateConflict):
            self.store.prepare_moment(
                text="间隔不足",
                attachment_refs=[],
                privacy_tags=["SoulMate测试"],
                source="manual_test",
                now=_now() + timedelta(hours=47),
            )

        dispatching = self.store.dispatch_moment(prepared.id, scheduled_at=_now())
        self.assertEqual(dispatching.status, "dispatching")
        leased = self.store.lease_actions(agent_id="agent-a", now=_now())[0]
        self.assertEqual(leased.action_type, "moment")
        self.assertEqual(leased.payload["privacy_tags"], ["SoulMate测试"])
        failed = self.store.fail_action(
            action_id=leased.action_id,
            agent_id="agent-a",
            lease_token=leased.lease_token,
            reason="点击发布后未收到可靠结果",
            retryable=False,
            delivery_unknown=True,
        )
        self.assertEqual(failed.status, "delivery_unknown")
        self.assertEqual(self.store.get_moment(prepared.id).status, "delivery_unknown")
        self.assertEqual(self.store.lease_actions(agent_id="agent-a"), [])
        resolved = self.store.resolve_moment_unknown(
            prepared.id,
            published=False,
            note="已在微信端确认没有发布",
        )
        self.assertEqual(resolved.status, "failed")

    def test_moment_ack_marks_published_and_prevents_replay(self) -> None:
        prepared = self.store.prepare_moment(
            text="发布确认测试",
            attachment_refs=[],
            privacy_tags=["SoulMate测试"],
            source="manual_test",
            now=_now(),
        )
        dispatching = self.store.dispatch_moment(prepared.id, scheduled_at=_now())
        leased = self.store.lease_actions(agent_id="agent-a", now=_now())[0]
        self.store.ack_action(
            action_id=leased.action_id,
            agent_id="agent-a",
            lease_token=leased.lease_token,
            provider_message_id="moment-provider-1",
        )
        published = self.store.get_moment(prepared.id)
        self.assertEqual(published.status, "published")
        self.assertEqual(published.provider_post_id, "moment-provider-1")
        self.assertEqual(self.store.dispatch_moment(prepared.id).status, "published")


class BridgeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.root = Path(self.temporary_dir.name)
        self.environment = patch.dict(
            os.environ,
            {
                "DATA_DIR": str(self.root / "data"),
                "WECHAT_BRIDGE_TOKEN": "bridge-test-token",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = BridgeStore(self.root / "data" / "soul_mate.db")
        self.store.initialize_schema()
        self.contact = self.store.create_contact(
            external_id="wxid-alice",
            display_name="Alice",
        )
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer bridge-test-token"}

    def test_bridge_requires_dedicated_bearer_token(self) -> None:
        missing = self.client.post(
            "/api/bridge/v1/heartbeat",
            json={"agent_id": "agent-a"},
        )
        wrong = self.client.post(
            "/api/bridge/v1/heartbeat",
            headers={"Authorization": "Bearer wrong"},
            json={"agent_id": "agent-a"},
        )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)

    def test_heartbeat_inbound_lease_ack_flow(self) -> None:
        heartbeat = self.client.post(
            "/api/bridge/v1/heartbeat",
            headers=self.headers,
            json={
                "agent_id": "agent-a",
                "bridge_version": "0.1.0",
                "wechat_version": "4.1.8.107",
                "plus_active": False,
                "window_ready": True,
                "capabilities": ["text"],
            },
        )
        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(heartbeat.json()["status"], "online")

        inbound = self.client.post(
            "/api/bridge/v1/inbound",
            headers=self.headers,
            json={
                "event_id": "event-api-1",
                "external_contact_id": "wxid-alice",
                "occurred_at": _now().isoformat(),
                "type": "text",
                "text": "你好",
                "attachment_refs": [],
                "message_hash": "c" * 64,
                "agent_id": "agent-a",
            },
        )
        self.assertEqual(inbound.status_code, 202)
        self.assertEqual(inbound.json()["action"], "accepted")

        action = self.store.enqueue_action(
            contact_id=self.contact.id,
            action_type="text",
            payload={"chunks": ["你好呀"]},
            scheduled_at=_now(),
            dedupe_key="api-action",
        )
        lease = self.client.post(
            "/api/bridge/v1/outbox/lease",
            headers=self.headers,
            json={"agent_id": "agent-a", "limit": 5, "lease_seconds": 30},
        )
        self.assertEqual(lease.status_code, 200)
        leased = lease.json()["actions"][0]
        self.assertEqual(leased["action_id"], action.action_id)
        self.assertEqual(leased["external_contact_id"], "wxid-alice")

        ack = self.client.post(
            f"/api/bridge/v1/outbox/{action.action_id}/ack",
            headers=self.headers,
            json={
                "agent_id": "agent-a",
                "lease_token": leased["lease_token"],
                "provider_message_id": "wx-msg-1",
            },
        )
        self.assertEqual(ack.status_code, 200)
        self.assertEqual(ack.json()["status"], "sent")

    def test_agent_can_sync_enabled_contacts_and_upload_contact_scoped_attachments(self) -> None:
        bob = self.store.create_contact(external_id="wxid-bob", display_name="Bob")
        contacts = self.client.get("/api/bridge/v1/contacts", headers=self.headers)
        self.assertEqual(contacts.status_code, 200)
        self.assertEqual(
            contacts.json()["contacts"],
            [
                {"external_contact_id": "wxid-alice", "display_name": "Alice"},
                {"external_contact_id": "wxid-bob", "display_name": "Bob"},
            ],
        )

        content = b"not-a-real-png-but-content-addressed"
        digest = hashlib.sha256(content).hexdigest()
        upload_headers = {
            **self.headers,
            "X-External-Contact-ID": "wxid-alice",
            "X-Attachment-Kind": "image",
            "X-Attachment-SHA256": digest,
            "X-Attachment-Mime": "image/png",
            "Content-Type": "application/octet-stream",
        }
        first = self.client.post(
            "/api/bridge/v1/attachments",
            headers=upload_headers,
            content=content,
        )
        self.assertEqual(first.status_code, 201)
        self.assertFalse(first.json()["duplicate"])
        attachment_ref = first.json()["attachment_ref"]
        stored_files = list((self.root / "data" / "attachments").rglob("*.png"))
        self.assertEqual(len(stored_files), 1)
        self.assertEqual(stored_files[0].read_bytes(), content)
        download = self.client.get(
            f"/api/bridge/v1/attachments/{attachment_ref}",
            headers={**self.headers, "X-External-Contact-ID": "wxid-alice"},
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, content)
        denied_download = self.client.get(
            f"/api/bridge/v1/attachments/{attachment_ref}",
            headers={**self.headers, "X-External-Contact-ID": "wxid-bob"},
        )
        self.assertEqual(denied_download.status_code, 404)

        cross_contact = self.client.post(
            "/api/bridge/v1/inbound",
            headers=self.headers,
            json={
                "event_id": "event-bob-image-before-upload",
                "external_contact_id": "wxid-bob",
                "occurred_at": _now().isoformat(),
                "type": "image",
                "attachment_refs": [attachment_ref],
                "message_hash": "e" * 64,
                "agent_id": "agent-a",
            },
        )
        self.assertEqual(cross_contact.status_code, 409)

        bob_upload = self.client.post(
            "/api/bridge/v1/attachments",
            headers={**upload_headers, "X-External-Contact-ID": "wxid-bob"},
            content=content,
        )
        self.assertEqual(bob_upload.status_code, 201)
        self.assertTrue(bob_upload.json()["duplicate"])
        self.assertEqual(bob_upload.json()["attachment_ref"], attachment_ref)
        bob_inbound = self.client.post(
            "/api/bridge/v1/inbound",
            headers=self.headers,
            json={
                "event_id": "event-bob-image-after-upload",
                "external_contact_id": "wxid-bob",
                "occurred_at": _now().isoformat(),
                "type": "image",
                "attachment_refs": [attachment_ref],
                "message_hash": "f" * 64,
                "agent_id": "agent-a",
            },
        )
        self.assertEqual(bob_inbound.status_code, 202)
        self.assertEqual(self.store.count_inbound(contact_id=bob.id), 1)

    def test_attachment_upload_rejects_unknown_contact_and_bad_digest(self) -> None:
        content = b"image"
        digest = hashlib.sha256(content).hexdigest()
        base_headers = {
            **self.headers,
            "X-Attachment-Kind": "image",
            "X-Attachment-Mime": "image/png",
            "Content-Type": "application/octet-stream",
        }
        unknown = self.client.post(
            "/api/bridge/v1/attachments",
            headers={
                **base_headers,
                "X-External-Contact-ID": "wxid-unknown",
                "X-Attachment-SHA256": digest,
            },
            content=content,
        )
        bad_digest = self.client.post(
            "/api/bridge/v1/attachments",
            headers={
                **base_headers,
                "X-External-Contact-ID": "wxid-alice",
                "X-Attachment-SHA256": "0" * 64,
            },
            content=content,
        )
        self.assertEqual(unknown.status_code, 403)
        self.assertEqual(bad_digest.status_code, 409)
        self.assertFalse((self.root / "data" / "attachments").exists())

    def test_unknown_contact_is_rejected_without_persisting_content(self) -> None:
        response = self.client.post(
            "/api/bridge/v1/inbound",
            headers=self.headers,
            json={
                "event_id": "event-unknown",
                "external_contact_id": "not-allowed",
                "occurred_at": _now().isoformat(),
                "type": "text",
                "text": "不应入库",
                "attachment_refs": [],
                "message_hash": "d" * 64,
                "agent_id": "agent-a",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.count_inbound(), 0)


if __name__ == "__main__":
    unittest.main()
