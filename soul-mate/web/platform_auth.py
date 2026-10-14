"""Owner 会话、登录限速、CSRF 与危险操作确认令牌基础模块。

本模块与现有 `web/app.py` 完全隔离，供后续 FastAPI 集成显式接入；不修改、
不依赖任何现有路由。所有状态均为进程内存态（会话、限速计数、确认令牌），
仅密码校验与自举兼容 ADMIN_PASSWORD 环境变量。

安全要点：
- 密码只以 `hashlib.scrypt` 编码后的字符串形式存在于 `OWNER_PASSWORD_HASH`；
  本模块不会把明文密码写入磁盘或审计。
- 会话令牌只保存其哈希，原始令牌不落盘、不入日志。
- 登录失败按来源计数并限速，失败时只返回剩余等待秒数，不透露密码线索。
- CSRF 采用双重提交校验：写操作必须携带与会话绑定的 `csrf_secret` 一致的令牌。
- 确认令牌绑定 owner/action/scope/resource_id，短时效、一次性消费。
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Mapping

__all__ = [
    "hash_owner_password",
    "verify_scrypt_hash",
    "verify_owner_password",
    "OwnerCredentialError",
    "SessionRecord",
    "OwnerSessionService",
    "RateLimitExceeded",
    "LoginRateLimiter",
    "SAFE_HTTP_METHODS",
    "is_safe_method",
    "verify_csrf_token",
    "ConfirmationToken",
    "ConfirmationTokenError",
    "ConfirmationTokenService",
]


# ── 密码哈希（scrypt）与 Owner 凭证解析 ─────────────────────────────

_SCRYPT_SCHEME = "scrypt"
_DEFAULT_SCRYPT_N = 2**14
_DEFAULT_SCRYPT_R = 8
_DEFAULT_SCRYPT_P = 1
_DEFAULT_SCRYPT_DKLEN = 64


class OwnerCredentialError(ValueError):
    """Owner 凭证配置缺失或格式非法。"""


def hash_owner_password(
    password: str,
    *,
    n: int = _DEFAULT_SCRYPT_N,
    r: int = _DEFAULT_SCRYPT_R,
    p: int = _DEFAULT_SCRYPT_P,
    dklen: int = _DEFAULT_SCRYPT_DKLEN,
    salt: bytes | None = None,
) -> str:
    """使用标准库 `hashlib.scrypt` 生成可持久化的密码哈希编码串。

    编码格式：`scrypt$n$r$p$salt_hex$hash_hex`，可直接写入 `OWNER_PASSWORD_HASH`。
    """
    if not password:
        raise OwnerCredentialError("密码不能为空")
    resolved_salt = salt if salt is not None else os.urandom(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=resolved_salt, n=n, r=r, p=p, dklen=dklen
    )
    return "$".join(
        (
            _SCRYPT_SCHEME,
            str(n),
            str(r),
            str(p),
            resolved_salt.hex(),
            derived.hex(),
        )
    )


def verify_scrypt_hash(password: str, encoded: str) -> bool:
    """校验密码与 `hash_owner_password` 编码串是否匹配，使用安全比较。"""
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != _SCRYPT_SCHEME:
        return False
    _, n_text, r_text, p_text, salt_hex, hash_hex = parts
    try:
        n, r, p = int(n_text), int(r_text), int(p_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    if not password:
        return False
    try:
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except ValueError:
        return False
    return secrets.compare_digest(candidate, expected)


def verify_owner_password(
    password: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """校验密码并返回匹配的 owner_id，不匹配返回 None。

    优先使用 `OWNER_PASSWORD_HASH`（scrypt 编码）；仅当其缺失时，回退到
    `ADMIN_PASSWORD` 明文环境变量作为迁移期兼容自举，两者都不会被本函数
    写入磁盘或日志。
    """
    env = environ if environ is not None else os.environ
    owner_id = env.get("OWNER_ID", "").strip() or "owner"
    password_hash = env.get("OWNER_PASSWORD_HASH", "").strip()
    if password_hash:
        return owner_id if verify_scrypt_hash(password, password_hash) else None

    admin_password = env.get("ADMIN_PASSWORD", "")
    if admin_password and secrets.compare_digest(password, admin_password):
        return owner_id
    return None


def owner_credential_configured(environ: Mapping[str, str] | None = None) -> bool:
    """判断是否已配置任一可用的 Owner 凭证来源（哈希或兼容自举）。"""
    env = environ if environ is not None else os.environ
    return bool(env.get("OWNER_PASSWORD_HASH", "").strip() or env.get("ADMIN_PASSWORD", ""))


def owner_credential_mode(
    environ: Mapping[str, str] | None = None,
) -> Literal["scrypt_hash", "admin_password_bootstrap", "unconfigured"]:
    """报告当前生效的凭证模式，供上层日志/健康检查使用，不暴露密码内容。"""
    env = environ if environ is not None else os.environ
    if env.get("OWNER_PASSWORD_HASH", "").strip():
        return "scrypt_hash"
    if env.get("ADMIN_PASSWORD", ""):
        return "admin_password_bootstrap"
    return "unconfigured"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    """对随机会话/确认令牌做单向哈希，仅哈希值会被保存在内存记录中。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Owner 会话服务 ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """内存态 Owner 会话记录快照（不含原始令牌）。"""

    session_id: str
    owner_id: str
    token_hash: str
    csrf_secret: str
    created_at: datetime
    expires_at: datetime
    bound_source_ip: str | None
    revoked: bool = False

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return (now or _utcnow()) >= self.expires_at

    def is_active(self, *, now: datetime | None = None) -> bool:
        return not self.revoked and not self.is_expired(now=now)


@dataclass(frozen=True, slots=True)
class _IssuedSession:
    """`create_session` 的返回值：明文令牌只在创建时返回一次。"""

    session_id: str
    token: str
    csrf_token: str
    record: SessionRecord


class OwnerSessionService:
    """进程内存的 Owner 短期会话管理。

    - 会话 ID 与令牌均为 `secrets.token_urlsafe` 生成的高熵随机值。
    - 令牌只保存哈希（sha256），校验时使用安全比较。
    - 可选按来源 IP 绑定：创建时记录来源，校验时若来源不符则拒绝。
    """

    def __init__(self, *, ttl_seconds: int = 60 * 60 * 4) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须为正数")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = threading.Lock()

    def create_session(
        self,
        owner_id: str,
        *,
        source_ip: str | None = None,
        now: datetime | None = None,
    ) -> _IssuedSession:
        """创建新会话，返回明文令牌（仅此一次）与会话记录快照。"""
        current = now or _utcnow()
        session_id = secrets.token_urlsafe(24)
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        record = SessionRecord(
            session_id=session_id,
            owner_id=owner_id,
            token_hash=_hash_token(token),
            csrf_secret=_hash_token(csrf_token),
            created_at=current,
            expires_at=current + self._ttl,
            bound_source_ip=source_ip,
            revoked=False,
        )
        with self._lock:
            self._sessions[session_id] = record
        return _IssuedSession(
            session_id=session_id, token=token, csrf_token=csrf_token, record=record
        )

    def verify_session(
        self,
        session_id: str,
        token: str,
        *,
        source_ip: str | None = None,
        now: datetime | None = None,
    ) -> SessionRecord | None:
        """校验会话 ID + 令牌是否有效；来源绑定不符或过期/撤销均返回 None。"""
        with self._lock:
            record = self._sessions.get(session_id)
        if record is None:
            return None
        if not secrets.compare_digest(record.token_hash, _hash_token(token)):
            return None
        if not record.is_active(now=now):
            return None
        if record.bound_source_ip is not None and record.bound_source_ip != source_ip:
            return None
        return record

    def revoke_session(self, session_id: str) -> bool:
        """撤销指定会话；返回是否存在过该会话。"""
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return False
            self._sessions[session_id] = _replace_revoked(record)
        return True

    def revoke_all_for_owner(self, owner_id: str) -> int:
        """撤销某 owner 的所有会话，返回撤销数量。"""
        count = 0
        with self._lock:
            for session_id, record in list(self._sessions.items()):
                if record.owner_id == owner_id and not record.revoked:
                    self._sessions[session_id] = _replace_revoked(record)
                    count += 1
        return count

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """清理已过期的会话记录，返回清理数量（供长期运行进程周期调用）。"""
        current = now or _utcnow()
        with self._lock:
            expired_ids = [
                session_id
                for session_id, record in self._sessions.items()
                if record.is_expired(now=current)
            ]
            for session_id in expired_ids:
                del self._sessions[session_id]
        return len(expired_ids)


def _replace_revoked(record: SessionRecord) -> SessionRecord:
    return SessionRecord(
        session_id=record.session_id,
        owner_id=record.owner_id,
        token_hash=record.token_hash,
        csrf_secret=record.csrf_secret,
        created_at=record.created_at,
        expires_at=record.expires_at,
        bound_source_ip=record.bound_source_ip,
        revoked=True,
    )


# ── 登录限速 ──────────────────────────────────────────────────────


class RateLimitExceeded(RuntimeError):
    """登录来源已被限速，携带明确的剩余等待秒数。"""

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"登录尝试过于频繁，请在 {retry_after_seconds:.0f} 秒后重试")


@dataclass(slots=True)
class _AttemptState:
    failure_count: int = 0
    locked_until: datetime | None = None


class LoginRateLimiter:
    """按来源标识（通常为 source_ip）限速登录尝试。

    连续失败达到阈值后，来源进入锁定状态，锁定时长随连续失败次数指数递增
    （封顶 `max_lockout_seconds`）。成功登录会清空该来源的失败计数。
    """

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        base_lockout_seconds: float = 5.0,
        max_lockout_seconds: float = 300.0,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须为正数")
        self._max_attempts = max_attempts
        self._base_lockout = base_lockout_seconds
        self._max_lockout = max_lockout_seconds
        self._states: dict[str, _AttemptState] = {}
        self._lock = threading.Lock()

    def check(self, source_key: str, *, now: datetime | None = None) -> None:
        """在尝试登录前调用；若来源仍处锁定期，抛出 `RateLimitExceeded`。"""
        current = now or _utcnow()
        with self._lock:
            state = self._states.get(source_key)
            if state is None or state.locked_until is None:
                return
            remaining = (state.locked_until - current).total_seconds()
            if remaining > 0:
                raise RateLimitExceeded(remaining)

    def record_failure(self, source_key: str, *, now: datetime | None = None) -> float:
        """记录一次失败尝试，返回本次触发的锁定剩余秒数（未达阈值则为 0）。"""
        current = now or _utcnow()
        with self._lock:
            state = self._states.setdefault(source_key, _AttemptState())
            state.failure_count += 1
            if state.failure_count < self._max_attempts:
                return 0.0
            overflow = state.failure_count - self._max_attempts
            lockout = min(self._base_lockout * (2**overflow), self._max_lockout)
            state.locked_until = current + timedelta(seconds=lockout)
            return lockout

    def record_success(self, source_key: str) -> None:
        """成功登录后清空该来源的失败计数与锁定状态。"""
        with self._lock:
            self._states.pop(source_key, None)


# ── CSRF ──────────────────────────────────────────────────────────

SAFE_HTTP_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def is_safe_method(method: str) -> bool:
    """安全方法（不改变服务端状态）豁免 CSRF 校验。"""
    return method.upper() in SAFE_HTTP_METHODS


def verify_csrf_token(session: SessionRecord, submitted_token: str) -> bool:
    """校验写操作提交的 CSRF 令牌是否与会话绑定的 `csrf_secret` 一致。"""
    if not submitted_token:
        return False
    return secrets.compare_digest(session.csrf_secret, _hash_token(submitted_token))


# ── 危险操作确认令牌 ─────────────────────────────────────────────────


class ConfirmationTokenError(ValueError):
    """确认令牌不存在、已过期、已被消费或与请求不匹配。"""


@dataclass(frozen=True, slots=True)
class ConfirmationToken:
    """内存态一次性确认令牌记录（不含原始令牌）。"""

    token_id: str
    token_hash: str
    owner_id: str
    action: str
    scope: str
    resource_id: str | None
    reason: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return (now or _utcnow()) >= self.expires_at

    def is_consumed(self) -> bool:
        return self.consumed_at is not None


@dataclass(frozen=True, slots=True)
class _IssuedConfirmationToken:
    """`issue` 的返回值：明文令牌只在创建时返回一次。"""

    token_id: str
    token: str
    record: ConfirmationToken


class ConfirmationTokenService:
    """危险操作的短时效、一次性确认令牌管理。

    令牌与 `owner_id` / `action` / `scope` / `resource_id` 四元组绑定；
    `consume` 必须提供完全一致的四元组才能消费，且每个令牌只能消费一次。
    """

    def __init__(self, *, ttl_seconds: int = 120) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须为正数")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._tokens: dict[str, ConfirmationToken] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        *,
        owner_id: str,
        action: str,
        scope: str,
        resource_id: str | None = None,
        reason: str = "",
        now: datetime | None = None,
    ) -> _IssuedConfirmationToken:
        """签发一枚绑定四元组的确认令牌，返回明文令牌（仅此一次）。"""
        current = now or _utcnow()
        token_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        record = ConfirmationToken(
            token_id=token_id,
            token_hash=_hash_token(token),
            owner_id=owner_id,
            action=action,
            scope=scope,
            resource_id=resource_id,
            reason=reason,
            created_at=current,
            expires_at=current + self._ttl,
            consumed_at=None,
        )
        with self._lock:
            self._tokens[token_id] = record
        return _IssuedConfirmationToken(token_id=token_id, token=token, record=record)

    def consume(
        self,
        token_id: str,
        token: str,
        *,
        owner_id: str,
        action: str,
        scope: str,
        resource_id: str | None = None,
        now: datetime | None = None,
    ) -> ConfirmationToken:
        """校验并消费确认令牌；成功后立即标记为已消费，不可复用。

        任何不匹配（令牌值、四元组、过期、已消费）都会抛出
        `ConfirmationTokenError`，不区分具体原因以避免探测。
        """
        current = now or _utcnow()
        with self._lock:
            record = self._tokens.get(token_id)
            if record is None:
                raise ConfirmationTokenError("确认令牌不存在或已失效")
            if not secrets.compare_digest(record.token_hash, _hash_token(token)):
                raise ConfirmationTokenError("确认令牌不存在或已失效")
            if record.is_consumed():
                raise ConfirmationTokenError("确认令牌已被使用")
            if record.is_expired(now=current):
                raise ConfirmationTokenError("确认令牌已过期")
            if (
                record.owner_id != owner_id
                or record.action != action
                or record.scope != scope
                or record.resource_id != resource_id
            ):
                raise ConfirmationTokenError("确认令牌与本次操作不匹配")

            consumed_record = ConfirmationToken(
                token_id=record.token_id,
                token_hash=record.token_hash,
                owner_id=record.owner_id,
                action=record.action,
                scope=record.scope,
                resource_id=record.resource_id,
                reason=record.reason,
                created_at=record.created_at,
                expires_at=record.expires_at,
                consumed_at=current,
            )
            self._tokens[token_id] = consumed_record
        return consumed_record

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """清理已过期的确认令牌记录，返回清理数量。"""
        current = now or _utcnow()
        with self._lock:
            expired_ids = [
                token_id
                for token_id, record in self._tokens.items()
                if record.is_expired(now=current)
            ]
            for token_id in expired_ids:
                del self._tokens[token_id]
        return len(expired_ids)
