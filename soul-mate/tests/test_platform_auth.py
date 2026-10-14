"""平台基础模块（web/platform_auth.py, web/platform_audit.py）的单元测试。

覆盖范围：scrypt 哈希、ADMIN_PASSWORD 兼容自举、会话失效/撤销、登录限速、
CSRF、确认令牌一次性/绑定、审计分页与敏感字段防泄露。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from web.platform_audit import (  # noqa: E402
    AuditPayloadSecurityError,
    PlatformAuditStore,
)
from web.platform_auth import (  # noqa: E402
    ConfirmationTokenError,
    ConfirmationTokenService,
    LoginRateLimiter,
    OwnerSessionService,
    RateLimitExceeded,
    hash_owner_password,
    is_safe_method,
    owner_credential_mode,
    verify_csrf_token,
    verify_owner_password,
    verify_scrypt_hash,
)


class ScryptPasswordHashTests(unittest.TestCase):
    def test_hash_and_verify_round_trip(self) -> None:
        encoded = hash_owner_password("correct-horse-battery-staple")
        self.assertTrue(encoded.startswith("scrypt$"))
        self.assertTrue(verify_scrypt_hash("correct-horse-battery-staple", encoded))

    def test_verify_rejects_wrong_password(self) -> None:
        encoded = hash_owner_password("right-password")
        self.assertFalse(verify_scrypt_hash("wrong-password", encoded))

    def test_verify_rejects_malformed_encoding(self) -> None:
        self.assertFalse(verify_scrypt_hash("anything", "not-a-valid-hash"))
        self.assertFalse(verify_scrypt_hash("anything", "scrypt$bad$format"))

    def test_hash_rejects_empty_password(self) -> None:
        with self.assertRaises(ValueError):
            hash_owner_password("")

    def test_distinct_salts_produce_distinct_hashes(self) -> None:
        first = hash_owner_password("same-password")
        second = hash_owner_password("same-password")
        self.assertNotEqual(first, second)


class OwnerCredentialBootstrapTests(unittest.TestCase):
    def test_owner_password_hash_takes_priority(self) -> None:
        encoded = hash_owner_password("hash-mode-password")
        environ = {
            "OWNER_PASSWORD_HASH": encoded,
            "ADMIN_PASSWORD": "should-be-ignored",
            "OWNER_ID": "owner-1",
        }
        self.assertEqual(
            verify_owner_password("hash-mode-password", environ=environ), "owner-1"
        )
        self.assertIsNone(verify_owner_password("should-be-ignored", environ=environ))
        self.assertEqual(owner_credential_mode(environ), "scrypt_hash")

    def test_admin_password_bootstrap_when_hash_missing(self) -> None:
        environ = {"ADMIN_PASSWORD": "legacy-secret"}
        self.assertEqual(verify_owner_password("legacy-secret", environ=environ), "owner")
        self.assertIsNone(verify_owner_password("wrong", environ=environ))
        self.assertEqual(owner_credential_mode(environ), "admin_password_bootstrap")

    def test_default_owner_id_is_owner(self) -> None:
        environ = {"ADMIN_PASSWORD": "legacy-secret"}
        self.assertEqual(verify_owner_password("legacy-secret", environ=environ), "owner")

    def test_unconfigured_credentials_never_authenticate(self) -> None:
        environ: dict[str, str] = {}
        self.assertIsNone(verify_owner_password("anything", environ=environ))
        self.assertEqual(owner_credential_mode(environ), "unconfigured")


class OwnerSessionServiceTests(unittest.TestCase):
    def test_create_and_verify_session(self) -> None:
        service = OwnerSessionService(ttl_seconds=3600)
        issued = service.create_session("owner")
        record = service.verify_session(issued.session_id, issued.token)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.owner_id, "owner")
        self.assertTrue(record.is_active())

    def test_verify_rejects_wrong_token(self) -> None:
        service = OwnerSessionService(ttl_seconds=3600)
        issued = service.create_session("owner")
        self.assertIsNone(service.verify_session(issued.session_id, "wrong-token"))

    def test_verify_rejects_unknown_session(self) -> None:
        service = OwnerSessionService(ttl_seconds=3600)
        self.assertIsNone(service.verify_session("unknown-id", "any-token"))

    def test_session_expires_after_ttl(self) -> None:
        service = OwnerSessionService(ttl_seconds=60)
        issued = service.create_session("owner")
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        self.assertIsNone(
            service.verify_session(issued.session_id, issued.token, now=future)
        )

    def test_revoke_session_invalidates_it(self) -> None:
        service = OwnerSessionService(ttl_seconds=3600)
        issued = service.create_session("owner")
        self.assertTrue(service.revoke_session(issued.session_id))
        self.assertIsNone(service.verify_session(issued.session_id, issued.token))

    def test_revoke_unknown_session_returns_false(self) -> None:
        service = OwnerSessionService(ttl_seconds=3600)
        self.assertFalse(service.revoke_session("does-not-exist"))

    def test_revoke_all_for_owner(self) -> None:
        service = OwnerSessionService(ttl_seconds=3600)
        first = service.create_session("owner")
        second = service.create_session("owner")
        other = service.create_session("someone-else")
        revoked_count = service.revoke_all_for_owner("owner")
        self.assertEqual(revoked_count, 2)
        self.assertIsNone(service.verify_session(first.session_id, first.token))
        self.assertIsNone(service.verify_session(second.session_id, second.token))
        self.assertIsNotNone(service.verify_session(other.session_id, other.token))

    def test_source_ip_binding_rejects_mismatch(self) -> None:
        service = OwnerSessionService(ttl_seconds=3600)
        issued = service.create_session("owner", source_ip="10.0.0.1")
        self.assertIsNone(
            service.verify_session(
                issued.session_id, issued.token, source_ip="10.0.0.2"
            )
        )
        self.assertIsNotNone(
            service.verify_session(
                issued.session_id, issued.token, source_ip="10.0.0.1"
            )
        )

    def test_purge_expired_removes_only_expired(self) -> None:
        service = OwnerSessionService(ttl_seconds=60)
        expiring = service.create_session("owner")
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        removed = service.purge_expired(now=future)
        self.assertEqual(removed, 1)
        self.assertIsNone(
            service.verify_session(expiring.session_id, expiring.token, now=future)
        )

    def test_token_is_not_stored_in_plaintext(self) -> None:
        service = OwnerSessionService(ttl_seconds=3600)
        issued = service.create_session("owner")
        self.assertNotEqual(issued.record.token_hash, issued.token)
        self.assertNotIn(issued.token, issued.record.token_hash)


class LoginRateLimiterTests(unittest.TestCase):
    def test_success_clears_failure_count(self) -> None:
        limiter = LoginRateLimiter(max_attempts=3, base_lockout_seconds=1.0)
        limiter.record_failure("1.2.3.4")
        limiter.record_failure("1.2.3.4")
        limiter.record_success("1.2.3.4")
        limiter.check("1.2.3.4")  # 不应抛出

    def test_lockout_after_threshold_reports_retry_after(self) -> None:
        limiter = LoginRateLimiter(max_attempts=3, base_lockout_seconds=5.0)
        limiter.record_failure("1.2.3.4")
        limiter.record_failure("1.2.3.4")
        retry_after = limiter.record_failure("1.2.3.4")
        self.assertGreater(retry_after, 0)
        with self.assertRaises(RateLimitExceeded) as ctx:
            limiter.check("1.2.3.4")
        self.assertGreater(ctx.exception.retry_after_seconds, 0)

    def test_lockout_expires_after_wait(self) -> None:
        limiter = LoginRateLimiter(max_attempts=2, base_lockout_seconds=10.0)
        limiter.record_failure("5.5.5.5")
        limiter.record_failure("5.5.5.5")
        future = datetime.now(timezone.utc) + timedelta(seconds=30)
        limiter.check("5.5.5.5", now=future)  # 不应抛出

    def test_independent_sources_are_isolated(self) -> None:
        limiter = LoginRateLimiter(max_attempts=1, base_lockout_seconds=10.0)
        limiter.record_failure("source-a")
        with self.assertRaises(RateLimitExceeded):
            limiter.check("source-a")
        limiter.check("source-b")  # 不应抛出

    def test_lockout_backs_off_exponentially(self) -> None:
        limiter = LoginRateLimiter(
            max_attempts=1, base_lockout_seconds=2.0, max_lockout_seconds=1000.0
        )
        first_retry = limiter.record_failure("host")
        limiter.record_success("host")
        limiter.record_failure("host")
        second_retry = limiter.record_failure("host")
        self.assertGreaterEqual(second_retry, first_retry)


class CsrfTests(unittest.TestCase):
    def test_safe_methods_are_exempt(self) -> None:
        for method in ("GET", "HEAD", "OPTIONS", "get", "head"):
            self.assertTrue(is_safe_method(method))
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertFalse(is_safe_method(method))

    def test_verify_csrf_token_matches_issued_token(self) -> None:
        service = OwnerSessionService(ttl_seconds=3600)
        issued = service.create_session("owner")
        self.assertTrue(verify_csrf_token(issued.record, issued.csrf_token))

    def test_verify_csrf_token_rejects_wrong_token(self) -> None:
        service = OwnerSessionService(ttl_seconds=3600)
        issued = service.create_session("owner")
        self.assertFalse(verify_csrf_token(issued.record, "wrong-csrf-token"))

    def test_verify_csrf_token_rejects_empty_token(self) -> None:
        service = OwnerSessionService(ttl_seconds=3600)
        issued = service.create_session("owner")
        self.assertFalse(verify_csrf_token(issued.record, ""))


class ConfirmationTokenServiceTests(unittest.TestCase):
    def _issue(self, service: ConfirmationTokenService):
        return service.issue(
            owner_id="owner",
            action="delete_character",
            scope="character",
            resource_id="char-1",
            reason="用户请求删除测试角色",
        )

    def test_issue_and_consume_succeeds_once(self) -> None:
        service = ConfirmationTokenService(ttl_seconds=120)
        issued = self._issue(service)
        consumed = service.consume(
            issued.token_id,
            issued.token,
            owner_id="owner",
            action="delete_character",
            scope="character",
            resource_id="char-1",
        )
        self.assertTrue(consumed.is_consumed())

    def test_token_cannot_be_reused(self) -> None:
        service = ConfirmationTokenService(ttl_seconds=120)
        issued = self._issue(service)
        service.consume(
            issued.token_id,
            issued.token,
            owner_id="owner",
            action="delete_character",
            scope="character",
            resource_id="char-1",
        )
        with self.assertRaises(ConfirmationTokenError):
            service.consume(
                issued.token_id,
                issued.token,
                owner_id="owner",
                action="delete_character",
                scope="character",
                resource_id="char-1",
            )

    def test_token_rejects_mismatched_action(self) -> None:
        service = ConfirmationTokenService(ttl_seconds=120)
        issued = self._issue(service)
        with self.assertRaises(ConfirmationTokenError):
            service.consume(
                issued.token_id,
                issued.token,
                owner_id="owner",
                action="different_action",
                scope="character",
                resource_id="char-1",
            )

    def test_token_rejects_mismatched_resource_id(self) -> None:
        service = ConfirmationTokenService(ttl_seconds=120)
        issued = self._issue(service)
        with self.assertRaises(ConfirmationTokenError):
            service.consume(
                issued.token_id,
                issued.token,
                owner_id="owner",
                action="delete_character",
                scope="character",
                resource_id="char-2",
            )

    def test_token_expires(self) -> None:
        service = ConfirmationTokenService(ttl_seconds=60)
        issued = self._issue(service)
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        with self.assertRaises(ConfirmationTokenError):
            service.consume(
                issued.token_id,
                issued.token,
                owner_id="owner",
                action="delete_character",
                scope="character",
                resource_id="char-1",
                now=future,
            )

    def test_wrong_token_value_rejected(self) -> None:
        service = ConfirmationTokenService(ttl_seconds=120)
        issued = self._issue(service)
        with self.assertRaises(ConfirmationTokenError):
            service.consume(
                issued.token_id,
                "wrong-token-value",
                owner_id="owner",
                action="delete_character",
                scope="character",
                resource_id="char-1",
            )

    def test_purge_expired_removes_only_expired_tokens(self) -> None:
        service = ConfirmationTokenService(ttl_seconds=60)
        issued = self._issue(service)
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        removed = service.purge_expired(now=future)
        self.assertEqual(removed, 1)
        with self.assertRaises(ConfirmationTokenError):
            service.consume(
                issued.token_id,
                issued.token,
                owner_id="owner",
                action="delete_character",
                scope="character",
                resource_id="char-1",
                now=future,
            )


class PlatformAuditStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.db_path = Path(self._temp_dir.name) / "platform_audit.db"
        self.store = PlatformAuditStore(db_path=self.db_path)

    def test_record_persists_and_is_listed(self) -> None:
        event = self.store.record(
            actor_id="owner",
            action="update_character",
            scope_type="character",
            outcome="success",
            character_id="char-1",
            resource_id="char-1",
            reason="修正显示名",
            before={"display_name": "旧名字"},
            after={"display_name": "新名字"},
            request_id="req-1",
            source_ip="127.0.0.1",
        )
        events = self.store.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, event.id)
        self.assertEqual(events[0].after_json, {"display_name": "新名字"})

    def test_record_rejects_password_field_in_before(self) -> None:
        with self.assertRaises(AuditPayloadSecurityError):
            self.store.record(
                actor_id="owner",
                action="rotate_credential",
                scope_type="owner",
                outcome="success",
                before={"password": "leaked-secret"},
            )

    def test_record_rejects_token_field_in_after(self) -> None:
        with self.assertRaises(AuditPayloadSecurityError):
            self.store.record(
                actor_id="owner",
                action="create_session",
                scope_type="owner",
                outcome="success",
                after={"session": {"token": "leaked-token"}},
            )

    def test_record_rejects_nested_sensitive_field(self) -> None:
        with self.assertRaises(AuditPayloadSecurityError):
            self.store.record(
                actor_id="owner",
                action="update_settings",
                scope_type="owner",
                outcome="success",
                after={"nested": {"list": [{"csrf_token": "abc"}]}},
            )

    def test_record_rejects_credential_marker_in_reason(self) -> None:
        with self.assertRaises(AuditPayloadSecurityError):
            self.store.record(
                actor_id="owner",
                action="login",
                scope_type="owner",
                outcome="failure",
                reason="attempted with password=abc123",
            )

    def test_list_events_pagination(self) -> None:
        for index in range(5):
            self.store.record(
                actor_id="owner",
                action="noop",
                scope_type="character",
                outcome="success",
                resource_id=f"item-{index}",
            )
        first_page = self.store.list_events(limit=2, offset=0)
        second_page = self.store.list_events(limit=2, offset=2)
        self.assertEqual(len(first_page), 2)
        self.assertEqual(len(second_page), 2)
        first_ids = {event.id for event in first_page}
        second_ids = {event.id for event in second_page}
        self.assertEqual(first_ids & second_ids, set())

    def test_list_events_filters_by_scope_and_character(self) -> None:
        self.store.record(
            actor_id="owner",
            action="a",
            scope_type="character",
            character_id="char-1",
            outcome="success",
        )
        self.store.record(
            actor_id="owner",
            action="b",
            scope_type="character",
            character_id="char-2",
            outcome="success",
        )
        self.store.record(
            actor_id="owner",
            action="c",
            scope_type="owner",
            outcome="success",
        )
        scoped = self.store.list_events(scope_type="character", character_id="char-1")
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0].character_id, "char-1")

    def test_count_matches_filtered_results(self) -> None:
        for index in range(3):
            self.store.record(
                actor_id="owner",
                action="noop",
                scope_type="character",
                outcome="success" if index else "failure",
            )
        self.assertEqual(self.store.count(), 3)
        self.assertEqual(self.store.count(outcome="failure"), 1)
        self.assertEqual(self.store.count(outcome="success"), 2)

    def test_events_are_ordered_most_recent_first(self) -> None:
        base = datetime.now(timezone.utc)
        for index in range(3):
            with_time = base + timedelta(seconds=index)
            self.store.record(
                actor_id="owner",
                action="noop",
                scope_type="character",
                outcome="success",
                resource_id=str(index),
            )
        events = self.store.list_events(limit=10)
        created_at_values = [event.created_at for event in events]
        self.assertEqual(created_at_values, sorted(created_at_values, reverse=True))

    def test_database_is_confined_to_project_root(self) -> None:
        from web.platform_audit import _resolve_audit_db_path

        import os

        original = os.environ.get("SOUL_MATE_ROOT")
        try:
            os.environ["SOUL_MATE_ROOT"] = str(Path(self._temp_dir.name))
            resolved = _resolve_audit_db_path()
            resolved.relative_to(Path(self._temp_dir.name).resolve())
        finally:
            if original is None:
                os.environ.pop("SOUL_MATE_ROOT", None)
            else:
                os.environ["SOUL_MATE_ROOT"] = original


if __name__ == "__main__":
    unittest.main()
