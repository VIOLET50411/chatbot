import base64
import gc
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class _Supervisor:
    def snapshot(self):
        return {"running": [], "recent": [], "counts": {"running": 0}}


class _Engine:
    _background_tasks = _Supervisor()
    process = None


class AdminApiContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(self.temporary_dir.name)
        self.environment = patch.dict(
            os.environ,
            {
                "SOUL_MATE_ROOT": str(self.root),
                "ADMIN_PASSWORD": "test-admin-password",
                "DATA_DIR": str(self.root / "data"),
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

        from scripts import character_registry

        self.registry_patches = [
            patch.object(character_registry, "_REGISTRY_DIR", self.root / "registry"),
            patch.object(character_registry, "_REGISTRY_FILE", self.root / "registry" / "characters.json"),
        ]
        for registry_patch in self.registry_patches:
            registry_patch.start()
            self.addCleanup(registry_patch.stop)
        self.character = character_registry.get_single_character()

        from web.app import create_app

        self.engine = _Engine()
        self.client = TestClient(create_app(self.engine))
        self.client.headers["Authorization"] = "Basic " + base64.b64encode(
            b"owner:test-admin-password"
        ).decode("ascii")
        self.addCleanup(self.client.close)

    def _scoped(self, path):
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}character_id={self.character['id']}"

    def _bind_pwa_contact(self):
        created = self.client.post(
            "/api/contacts",
            json={"external_id": "wxid-owner", "display_name": "我"},
        )
        self.assertEqual(created.status_code, 201)
        contact = created.json()
        bound = self.client.put(f"/api/contacts/{contact['id']}/pwa-binding")
        self.assertEqual(bound.status_code, 200)
        self.assertTrue(bound.json()["pwa_bound"])
        return contact

    def test_admin_contract_endpoints_return_safe_empty_states(self):
        expected = {
            "/api/overview": {"character_count", "enabled_contact_count", "pending_material_reviews", "inbound_pending", "generated_at"},
            "/api/settings": {"admin_username", "session_timeout_minutes"},
            "/api/settings/runtime": {"dialogue", "vision", "audio", "external_information", "proactive_defaults", "deployment"},
            "/api/wechat/status": {"bridge", "enabled_contacts", "inbound_pending", "outbox", "attachments", "stickers"},
            "/api/materials": {"items", "total", "summary"},
            "/api/contacts": {"items"},
            "/api/persona/snapshots": {"items"},
            "/api/external-information": {"configuration", "source_states", "cached_item_count", "recent_items"},
        }
        for path, keys in expected.items():
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertTrue(keys.issubset(response.json()), path)

        self.assertEqual(self.client.get("/api/runtime").status_code, 404)
        characters = self.client.get("/api/characters")
        self.assertEqual(characters.status_code, 200)
        self.assertEqual(characters.json()["items"][0]["display_name"], "杜佳颖")

    def test_persona_history_edit_creates_new_inactive_version_then_requires_confirmation_to_switch(self):
        snapshots = self.client.get("/api/persona/snapshots").json()["items"]
        original = next(item for item in snapshots if item["is_active"])
        created_response = self.client.post(
            f"/api/persona/snapshots/{original['id']}/edits",
            json={
                "stable_profile": {"name": "测试人物", "boundary": "会拒绝不合理要求"},
                "channel_styles": original["channel_styles"],
                "note": "测试从历史版本创建可回滚的新版本",
            },
        )
        self.assertEqual(created_response.status_code, 201)
        created = created_response.json()
        self.assertFalse(created["is_active"])
        self.assertEqual(created["version"], original["version"] + 1)
        self.assertEqual(
            next(item for item in self.client.get("/api/persona/snapshots").json()["items"] if item["is_active"])["id"],
            original["id"],
        )

        missing_confirmation = self.client.post(f"/api/persona/snapshots/{created['id']}/activate")
        self.assertEqual(missing_confirmation.status_code, 428)
        confirmation = self.client.post(
            "/api/confirmations",
            json={
                "action": "persona.snapshot.activate",
                "scope": "persona_snapshot",
                "resource_id": created["id"],
                "reason": "测试切换人格历史版本",
            },
        ).json()
        activated = self.client.post(
            f"/api/persona/snapshots/{created['id']}/activate",
            headers={
                "X-Confirmation-ID": confirmation["id"],
                "X-Confirmation-Token": confirmation["token"],
            },
        )
        self.assertEqual(activated.status_code, 200)
        self.assertTrue(activated.json()["is_active"])

    def test_persona_review_api_supports_any_character_candidate_and_saves_rating(self):
        from core.character_context import CharacterContext, character_scope
        from persona.dossier import create_edited_snapshot, list_snapshots
        from persona.review_cases import PersonaReviewStore
        from scripts.init_db import init

        context = CharacterContext(
            character_id=str(self.character["id"]),
            display_name=str(self.character["display_name"]),
            workspace_dir=self.root / "data",
        )
        init(context.workspace_dir)
        with character_scope(context):
            base = next(item for item in list_snapshots() if item["is_active"])
            candidate = create_edited_snapshot(
                base["id"],
                stable_profile={**base["stable_profile"], "review_rule": "只按完整语义断句"},
                channel_styles=base["channel_styles"],
                note="真人感第二轮候选 API 测试，未激活",
                actor="test",
            )
        store = PersonaReviewStore(context.workspace_dir / "soul_mate.db")
        store.seed_cases(
            candidate_snapshot_id=candidate["id"],
            character_id=context.character_id,
            cases=[
                {
                    "category": "日常",
                    "user_message": f"测试消息 {index}",
                    "context_note": "接口测试",
                    "candidate_reply": f"自然回复 {index}",
                }
                for index in range(40)
            ],
        )
        state = self.client.get(self._scoped(f"/api/persona/reviews/{candidate['id']}"))
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.json()["summary"]["total"], 40)
        case_id = state.json()["items"][0]["id"]
        rated = self.client.put(
            self._scoped(f"/api/persona/reviews/cases/{case_id}/rating"),
            json={"rating": "artificial", "reasons": ["断句太碎"], "note": "测试保存"},
        )
        self.assertEqual(rated.status_code, 200)
        refreshed = self.client.get(self._scoped(f"/api/persona/reviews/{candidate['id']}"))
        self.assertEqual(refreshed.json()["summary"]["rated"], 1)
        self.assertEqual(refreshed.json()["items"][0]["reasons"], ["断句太碎"])

    def test_api_boundary_auth_and_health_probe(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})
        missing = self.client.get("/api/not-a-route")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.headers["content-type"].split(";", 1)[0], "application/json")

        from web.app import create_app
        with TestClient(create_app(None)) as unauthenticated:
            self.assertEqual(unauthenticated.get("/api/overview").status_code, 401)

    def test_spa_uses_route_specific_manifest_for_admin_and_chat(self):
        admin = self.client.get("/")
        chat = self.client.get("/chat")
        self.assertEqual(admin.status_code, 200)
        self.assertEqual(chat.status_code, 200)
        self.assertIn('/static/admin-manifest.webmanifest', admin.text)
        self.assertIn('/static/chat-manifest.webmanifest', chat.text)
        self.assertNotIn('/static/chat-manifest.webmanifest', admin.text)
        self.assertNotIn('/static/admin-manifest.webmanifest', chat.text)

    def test_cookie_write_requires_csrf_token(self):
        from web.app import create_app
        with TestClient(create_app(None)) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "owner", "password": "test-admin-password"},
            )
            self.assertEqual(login.status_code, 200)
            response = client.post(
                "/api/contacts",
                json={"external_id": "wxid-no-csrf", "display_name": "不应写入"},
            )
            self.assertEqual(response.status_code, 403)

    def test_pwa_chat_send_uses_bound_contact_progress_without_wechat_outbox(self):
        contact = self._bind_pwa_contact()
        from core.contact_runtime import DirectConversationReply

        direct = AsyncMock(return_value=DirectConversationReply(["收到", "晚点聊"], "收到\n晚点聊"))
        with patch("web.app.ContactConversationService.process_direct", new=direct):
            response = self.client.post(
                "/api/pwa/send",
                json={"message": "在吗", "character_id": "default", "message_id": "pwa-test-1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"reply": "收到\n晚点聊", "chunks": ["收到", "晚点聊"], "status": "ok"},
        )
        direct.assert_awaited_once()
        self.assertEqual(direct.await_args.kwargs["contact_id"], contact["id"])
        self.assertEqual(direct.await_args.kwargs["message"], "在吗")

    def test_pwa_profile_requires_dedicated_token_when_configured(self):
        self._bind_pwa_contact()
        from web.app import create_app
        with patch.dict(os.environ, {"PWA_CHAT_TOKEN": "pwa-secret"}, clear=False):
            with TestClient(create_app(_Engine())) as client:
                missing = client.get("/api/pwa/profile")
                self.assertEqual(missing.status_code, 401)
                wrong = client.get(
                    "/api/pwa/profile",
                    headers={"X-PWA-Token": "wrong"},
                )
                self.assertEqual(wrong.status_code, 401)
                ok = client.get(
                    "/api/pwa/profile",
                    headers={"X-PWA-Token": "pwa-secret"},
                )
                self.assertEqual(ok.status_code, 200)
                self.assertEqual(ok.json()["display_name"], "杜佳颖")

    def test_global_runtime_prompt_is_saved_outside_persona_snapshot(self):
        empty = self.client.get("/api/runtime-prompt")
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json()["prompt"], "")

        saved = self.client.put(
            "/api/runtime-prompt",
            json={"prompt": "不要使用客服式套话"},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["prompt"], "不要使用客服式套话")
        history = self.client.get("/api/runtime-prompt/history")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json()["items"]), 1)

    def test_contact_whitelist_management_and_confirmed_disable(self):
        empty = self.client.get("/api/contacts")
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json(), {"items": []})

        created = self.client.post(
            "/api/contacts",
            json={"external_id": "wxid-contact-a", "display_name": "联系人甲"},
        )
        self.assertEqual(created.status_code, 201)
        contact = created.json()
        self.assertTrue(contact["enabled"])
        self.assertFalse(contact["proactive_enabled"])

        delivery = self.client.patch(
            f"/api/contacts/{contact['id']}",
            json={
                "proactive_enabled": True,
                "proactive_max_daily": 6,
                "proactive_cooldown_hours": 3,
                "proactive_active_start_hour": 9,
                "proactive_active_end_hour": 23,
                "proactive_check_in_after_hours": 10,
            },
        )
        self.assertEqual(delivery.status_code, 200)
        self.assertTrue(delivery.json()["proactive_enabled"])
        self.assertEqual(delivery.json()["proactive_max_daily"], 6)

        duplicate = self.client.post(
            "/api/contacts",
            json={"external_id": "wxid-contact-a", "display_name": "重复联系人"},
        )
        self.assertEqual(duplicate.status_code, 409)

        duplicate_name = self.client.post(
            "/api/contacts",
            json={"external_id": "wxid-contact-b", "display_name": "联系人甲"},
        )
        self.assertEqual(duplicate_name.status_code, 409)

        missing_confirmation = self.client.patch(
            f"/api/contacts/{contact['id']}",
            json={"enabled": False},
        )
        self.assertEqual(missing_confirmation.status_code, 428)

        confirmation = self.client.post(
            "/api/confirmations",
            json={
                "action": "contact.disable",
                "scope": "contact",
                "resource_id": contact["id"],
                "reason": "测试停用白名单联系人",
            },
        )
        self.assertEqual(confirmation.status_code, 200)
        disabled = self.client.patch(
            f"/api/contacts/{contact['id']}",
            headers={
                "X-Confirmation-ID": confirmation.json()["id"],
                "X-Confirmation-Token": confirmation.json()["token"],
            },
            json={"enabled": False},
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["enabled"])
        self.assertEqual(len(self.client.get("/api/contacts").json()["items"]), 1)

    def test_runtime_settings_encrypt_secrets_and_never_return_them(self):
        saved = self.client.put(
            "/api/settings/runtime",
            json={
                "deepseek_api_key": "deepseek-test-secret",
                "deepseek_model": "deepseek-v4-pro",
                "vision_model": "qwen3.7-flash",
                "proactive_max_daily": 5,
                "vps_target_url": "https://soul-mate.example.com",
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        body = saved.json()
        self.assertTrue(body["dialogue"]["api_key_configured"])
        self.assertNotIn("deepseek-test-secret", saved.text)
        self.assertEqual(body["deployment"]["vps_target_url"], "https://soul-mate.example.com")

        database = self.root / "data" / "platform" / "runtime_settings.db"
        import sqlite3
        with sqlite3.connect(database) as connection:
            stored, encrypted = connection.execute(
                "SELECT value, encrypted FROM owner_runtime_settings WHERE key='DEEPSEEK_API_KEY'"
            ).fetchone()
        self.assertEqual(encrypted, 1)
        self.assertNotEqual(stored, "deepseek-test-secret")

        cleared = self.client.put(
            "/api/settings/runtime",
            json={"clear_secrets": ["DEEPSEEK_API_KEY"]},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.json()["dialogue"]["api_key_configured"])

    def test_runtime_connection_test_is_character_scoped_and_never_returns_secrets(self):
        result = {
            "target": "dialogue",
            "ok": True,
            "latency_ms": 42,
            "message": "文字模型已返回有效响应",
        }
        with patch(
            "web.routes.platform_settings._run_connection_test",
            new=AsyncMock(return_value=result),
        ) as connection_test:
            response = self.client.post(
                self._scoped("/api/settings/runtime/test"),
                json={"target": "dialogue"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), result)
        self.assertNotIn("api_key", response.text.lower())
        connection_test.assert_awaited_once_with(str(self.character["id"]), "dialogue")

    def test_moment_requires_explicit_dispatch_confirmation(self):
        contact = self.client.post(
            "/api/contacts",
            json={"external_id": "wxid-moment-bridge", "display_name": "朋友圈桥接"},
        ).json()
        prepared = self.client.post(
            "/api/wechat/moments",
            json={
                "text": "测试朋友圈",
                "attachment_refs": [],
                "privacy_tags": ["SoulMate测试"],
                "source": "manual_test",
            },
        )
        self.assertEqual(prepared.status_code, 201)
        post = prepared.json()
        self.assertEqual(post["status"], "prepared")
        missing = self.client.post(f"/api/wechat/moments/{post['id']}/dispatch")
        self.assertEqual(missing.status_code, 428)
        confirmation = self.client.post(
            "/api/confirmations",
            json={
                "action": "moment.dispatch",
                "scope": "moment",
                "resource_id": post["id"],
                "reason": "确认只向测试标签发布一次",
            },
        ).json()
        dispatched = self.client.post(
            f"/api/wechat/moments/{post['id']}/dispatch",
            headers={
                "X-Confirmation-ID": confirmation["id"],
                "X-Confirmation-Token": confirmation["token"],
            },
        )
        self.assertEqual(dispatched.status_code, 200)
        self.assertEqual(dispatched.json()["status"], "dispatching")
        self.assertEqual(
            self.client.get("/api/wechat/moments").json()["items"][0]["id"],
            post["id"],
        )
        self.assertTrue(contact["enabled"])

    def test_contact_runtime_exposes_only_its_timeline_and_reviews_memory(self):
        alice = self.client.post(
            "/api/contacts",
            json={"external_id": "wxid-runtime-a", "display_name": "联系人甲"},
        ).json()
        bob = self.client.post(
            "/api/contacts",
            json={"external_id": "wxid-runtime-b", "display_name": "联系人乙"},
        ).json()
        from bridge.v1_store import BridgeStore
        from core.contact_runtime_store import ContactRuntimeStore

        store = BridgeStore.from_environment()
        runtime = ContactRuntimeStore(store.database_path)
        runtime.save_message(contact_id=alice["id"], role="user", content="甲的私密消息")
        runtime.save_message(contact_id=bob["id"], role="user", content="乙的私密消息")
        runtime.save_memory_candidates(
            contact_id=alice["id"],
            user_message="我喜欢乌龙茶",
            candidates=[{
                "type": "preference",
                "content": "喜欢乌龙茶",
                "evidence_quote": "喜欢乌龙茶",
                "importance": 4,
            }],
            source_event_ids=["alice-memory-source"],
        )
        detail = self.client.get(f"/api/contacts/{alice['id']}/runtime")
        self.assertEqual(detail.status_code, 200)
        serialized = str(detail.json())
        self.assertIn("甲的私密消息", serialized)
        self.assertNotIn("乙的私密消息", serialized)
        self.assertIn("conversation_checkpoint", detail.json())
        self.assertIn("memory_associations", detail.json())
        self.assertIn("pending_topic", detail.json())
        memory = detail.json()["memories"][0]
        published = self.client.patch(
            f"/api/contacts/{alice['id']}/memories/{memory['id']}",
            json={"status": "published"},
        )
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()["status"], "published")
        missing = self.client.patch(
            f"/api/contacts/{alice['id']}/memories/{memory['id']}",
            json={"status": "archived"},
        )
        self.assertEqual(missing.status_code, 428)
        confirmation = self.client.post(
            "/api/confirmations",
            json={
                "action": "contact.memory.archive",
                "scope": "contact_memory",
                "resource_id": memory["id"],
                "reason": "测试归档错误记忆",
            },
        ).json()
        archived = self.client.patch(
            f"/api/contacts/{alice['id']}/memories/{memory['id']}",
            headers={
                "X-Confirmation-ID": confirmation["id"],
                "X-Confirmation-Token": confirmation["token"],
            },
            json={"status": "archived"},
        )
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(archived.json()["status"], "archived")

    def test_media_asset_requires_owner_review_before_becoming_available(self):
        contact = self.client.post(
            "/api/contacts",
            json={"external_id": "wxid-media-review", "display_name": "媒体来源"},
        ).json()
        from bridge.v1_store import BridgeStore

        store = BridgeStore.from_environment()
        path = store.database_path.parent / "attachments" / "review-test.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"review-test-image")
        asset = store.register_attachment(
            external_contact_id="wxid-media-review",
            kind="image",
            storage_path="attachments/review-test.png",
            sha256="8" * 64,
            mime_type="image/png",
            size_bytes=17,
        )
        listed = self.client.get("/api/wechat/media")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"][0]["review_status"], "pending")
        content = self.client.get(f"/api/wechat/media/{asset.id}/content")
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.content, b"review-test-image")
        approved = self.client.patch(
            f"/api/wechat/media/{asset.id}",
            json={"review_status": "approved"},
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["review_status"], "approved")
        self.assertEqual(
            store.find_approved_media_asset(contact_id=contact["id"], kind="image")["attachment_ref"],
            asset.id,
        )


if __name__ == "__main__":
    unittest.main()
