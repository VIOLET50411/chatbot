"""多人物 Owner 管理台与微信桥接的 FastAPI 组合入口。"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bridge.v1_api import router as bridge_v1_router
from bridge.v1_store import BridgeStore
from core.contact_runtime import ContactConversationService
from core.contact_runtime_store import ContactRuntimeStore
from core.character_context import CharacterContext, bind_character_context, reset_character_context
from core.pwa_bindings import get_pwa_contact_binding
from web.platform_audit import PlatformAuditStore
from web.platform_auth import (
    ConfirmationTokenError,
    ConfirmationTokenService,
    LoginRateLimiter,
    OwnerSessionService,
    RateLimitExceeded,
    owner_credential_mode,
    verify_csrf_token,
    verify_owner_password,
)
from web.routes.contacts import create_contacts_router
from web.routes.baseline import create_baseline_router
from web.routes.evolution import create_evolution_router
from web.routes.materials import create_materials_router
from web.routes.media import create_media_router
from web.routes.moments import create_moments_router
from web.routes.wechat_status import create_wechat_status_router
from web.routes.wechat_bindings import create_wechat_bindings_router
from web.routes.runtime_prompt import create_runtime_prompt_router
from web.routes.snapshots import create_snapshots_router
from web.routes.external_information import create_external_information_router
from web.routes.devices import create_devices_router
from web.routes.platform_settings import create_platform_settings_router
from web.routes.owner_profile import create_owner_profile_router
from web.routes.persona_reviews import create_persona_reviews_router
from web.character_access import store_for_context


_STATIC_DIR = Path(__file__).resolve().parent / "static"
_SESSION_ID_COOKIE = "soul_mate_session_id"
_SESSION_TOKEN_COOKIE = "soul_mate_session_token"
_CSRF_COOKIE = "soul_mate_csrf"


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _source_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _cookie_secure() -> bool:
    return os.getenv("OWNER_COOKIE_SECURE", "false").strip().lower() == "true"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1_024)


class ConfirmationRequest(BaseModel):
    action: str = Field(min_length=1, max_length=128)
    scope: str = Field(min_length=1, max_length=128)
    resource_id: str | None = Field(default=None, max_length=256)
    reason: str = Field(min_length=3, max_length=500)


class PwaChatSendRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    character_id: str = Field(default="default", min_length=1, max_length=64)
    message_id: str = Field(min_length=1, max_length=128)


def create_app(engine: Any = None) -> FastAPI:
    app = FastAPI(title="Soul Mate Owner", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def bind_owner_character(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path.startswith(("/api/bridge/", "/api/pwa/")):
            return await call_next(request)
        from scripts.character_registry import get_character_context

        character_id = str(request.query_params.get("character_id") or "default").strip().lower()
        context = get_character_context(character_id)
        if context is None:
            return JSONResponse(status_code=404, content={"detail": "所选人物不存在"})
        request.state.character_context = context
        token = bind_character_context(context)
        try:
            return await call_next(request)
        finally:
            reset_character_context(token)

    app.include_router(bridge_v1_router)
    security = HTTPBasic(auto_error=False)
    sessions = OwnerSessionService(ttl_seconds=_positive_int_env("OWNER_SESSION_TTL_SECONDS", 14_400))
    confirmations = ConfirmationTokenService(ttl_seconds=_positive_int_env("OWNER_CONFIRMATION_TTL_SECONDS", 120))
    login_limiter = LoginRateLimiter(
        max_attempts=_positive_int_env("OWNER_LOGIN_MAX_ATTEMPTS", 5),
        base_lockout_seconds=float(_positive_int_env("OWNER_LOGIN_LOCKOUT_SECONDS", 5)),
    )
    audit_store = PlatformAuditStore()

    def _audit(
        request: Request,
        *,
        actor: str,
        action: str,
        scope_type: str,
        outcome: str,
        resource_id: str | None = None,
        reason: str = "",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        character_id: str | None = None,
    ) -> None:
        audit_store.record(
            actor_id=actor,
            action=action,
            scope_type=scope_type,
            outcome=outcome,
            character_id=character_id,
            resource_id=resource_id,
            reason=reason,
            before=before,
            after=after,
            request_id=str(request.headers.get("X-Request-ID") or "")[:128],
            source_ip=_source_ip(request),
        )

    def _session_owner(request: Request) -> tuple[str, Any] | None:
        session_id = request.cookies.get(_SESSION_ID_COOKIE, "")
        token = request.cookies.get(_SESSION_TOKEN_COOKIE, "")
        if not session_id or not token:
            return None
        record = sessions.verify_session(session_id, token, source_ip=_source_ip(request))
        return (record.owner_id, record) if record else None

    def _basic_owner(credentials: HTTPBasicCredentials | None) -> str | None:
        if credentials is None:
            return None
        expected_username = os.getenv("ADMIN_USERNAME", "owner").strip() or "owner"
        if not secrets.compare_digest(credentials.username, expected_username):
            return None
        return verify_owner_password(credentials.password)

    def _auth(
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(security),
    ) -> str:
        session = _session_owner(request)
        if session is not None:
            request.state.owner_auth_mode = "session"
            request.state.owner_session = session[1]
            return session[0]
        basic = _basic_owner(credentials)
        if basic is not None:
            request.state.owner_auth_mode = "basic"
            return basic
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Owner 认证失败",
            headers={"WWW-Authenticate": "Basic"},
        )

    def _write_auth(request: Request, owner: str = Depends(_auth)) -> str:
        if getattr(request.state, "owner_auth_mode", "") == "session":
            record = getattr(request.state, "owner_session", None)
            supplied = str(request.headers.get("X-CSRF-Token") or "")
            cookie_value = str(request.cookies.get(_CSRF_COOKIE) or "")
            if record is None or not supplied or not cookie_value:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="缺少 CSRF 令牌")
            if not secrets.compare_digest(supplied, cookie_value) or not verify_csrf_token(record, supplied):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 令牌无效")
        return owner

    def _consume_confirmation(
        request: Request,
        *,
        owner: str,
        action: str,
        scope: str,
        resource_id: str | None,
    ) -> str:
        token_id = str(request.headers.get("X-Confirmation-ID") or "").strip()
        token = str(request.headers.get("X-Confirmation-Token") or "").strip()
        if not token_id or not token:
            raise HTTPException(status_code=428, detail="此操作需要一次性确认令牌")
        try:
            consumed = confirmations.consume(
                token_id,
                token,
                owner_id=owner,
                action=action,
                scope=scope,
                resource_id=resource_id,
            )
        except ConfirmationTokenError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return consumed.reason

    app.include_router(
        create_contacts_router(
            read_auth=_auth,
            write_auth=_write_auth,
            consume_confirmation=_consume_confirmation,
            audit=_audit,
        )
    )
    app.include_router(
        create_baseline_router(
            read_auth=_auth,
            write_auth=_write_auth,
            consume_confirmation=_consume_confirmation,
            audit=_audit,
        )
    )
    app.include_router(
        create_materials_router(read_auth=_auth, write_auth=_write_auth, audit=_audit)
    )
    app.include_router(
        create_evolution_router(
            read_auth=_auth,
            write_auth=_write_auth,
            consume_confirmation=_consume_confirmation,
            audit=_audit,
        )
    )
    app.include_router(create_wechat_status_router(read_auth=_auth))
    app.include_router(
        create_wechat_bindings_router(
            read_auth=_auth,
            write_auth=_write_auth,
            consume_confirmation=_consume_confirmation,
            audit=_audit,
        )
    )
    app.include_router(
        create_media_router(read_auth=_auth, write_auth=_write_auth, audit=_audit)
    )
    app.include_router(
        create_moments_router(
            read_auth=_auth,
            write_auth=_write_auth,
            consume_confirmation=_consume_confirmation,
            audit=_audit,
        )
    )
    app.include_router(
        create_runtime_prompt_router(read_auth=_auth, write_auth=_write_auth, audit=_audit)
    )
    app.include_router(
        create_snapshots_router(
            read_auth=_auth,
            write_auth=_write_auth,
            consume_confirmation=_consume_confirmation,
            audit=_audit,
        )
    )
    app.include_router(
        create_external_information_router(read_auth=_auth, write_auth=_write_auth, audit=_audit)
    )
    app.include_router(
        create_devices_router(read_auth=_auth, write_auth=_write_auth, audit=_audit)
    )
    app.include_router(
        create_platform_settings_router(read_auth=_auth, write_auth=_write_auth, audit=_audit)
    )
    app.include_router(
        create_owner_profile_router(
            read_auth=_auth,
            write_auth=_write_auth,
            consume_confirmation=_consume_confirmation,
            audit=_audit,
        )
    )
    app.include_router(
        create_persona_reviews_router(read_auth=_auth, write_auth=_write_auth, audit=_audit)
    )

    def _set_session_cookies(response: Response, *, session_id: str, token: str, csrf_token: str) -> None:
        common = {
            "path": "/",
            "secure": _cookie_secure(),
            "samesite": "strict",
            "max_age": _positive_int_env("OWNER_SESSION_TTL_SECONDS", 14_400),
        }
        response.set_cookie(_SESSION_ID_COOKIE, session_id, httponly=True, **common)
        response.set_cookie(_SESSION_TOKEN_COOKIE, token, httponly=True, **common)
        response.set_cookie(_CSRF_COOKIE, csrf_token, httponly=False, **common)

    def _clear_session_cookies(response: Response) -> None:
        for name in (_SESSION_ID_COOKIE, _SESSION_TOKEN_COOKIE, _CSRF_COOKIE):
            response.delete_cookie(name, path="/", secure=_cookie_secure(), samesite="strict")

    @app.post("/api/auth/login")
    def login(body: LoginRequest, request: Request, response: Response):
        source = _source_ip(request)
        try:
            login_limiter.check(source)
        except RateLimitExceeded as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="登录尝试过于频繁",
                headers={"Retry-After": str(max(1, int(exc.retry_after_seconds)))},
            ) from exc
        expected_username = os.getenv("ADMIN_USERNAME", "owner").strip() or "owner"
        owner = verify_owner_password(body.password) if secrets.compare_digest(body.username, expected_username) else None
        if owner is None:
            retry_after = login_limiter.record_failure(source)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="账号或密码错误",
                headers={"Retry-After": str(max(0, int(retry_after)))},
            )
        login_limiter.record_success(source)
        issued = sessions.create_session(owner, source_ip=source)
        _set_session_cookies(
            response,
            session_id=issued.session_id,
            token=issued.token,
            csrf_token=issued.csrf_token,
        )
        _audit(request, actor=owner, action="owner.login", scope_type="owner", outcome="success")
        return {
            "authenticated": True,
            "actor": owner,
            "username": expected_username,
            "expires_at": issued.record.expires_at.isoformat(),
            "csrf_token": issued.csrf_token,
        }

    @app.get("/api/auth/session")
    def session_info(request: Request):
        active = _session_owner(request)
        if active is None:
            return {"authenticated": False, "actor": "", "username": "", "expires_at": None}
        return {
            "authenticated": True,
            "actor": active[0],
            "username": os.getenv("ADMIN_USERNAME", "owner").strip() or "owner",
            "expires_at": active[1].expires_at.isoformat(),
        }

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, owner: str = Depends(_write_auth)):
        session_id = request.cookies.get(_SESSION_ID_COOKIE, "")
        if session_id:
            sessions.revoke_session(session_id)
        _clear_session_cookies(response)
        _audit(request, actor=owner, action="owner.logout", scope_type="owner", outcome="success")
        return {"ok": True}

    @app.post("/api/confirmations")
    def issue_confirmation(
        body: ConfirmationRequest,
        request: Request,
        owner: str = Depends(_write_auth),
    ):
        issued = confirmations.issue(
            owner_id=owner,
            action=body.action,
            scope=body.scope,
            resource_id=body.resource_id,
            reason=body.reason,
        )
        _audit(
            request,
            actor=owner,
            action="confirmation.issue",
            scope_type=body.scope,
            outcome="success",
            resource_id=body.resource_id,
            reason=body.reason,
            after={"confirmed_action": body.action},
        )
        return {
            "id": issued.token_id,
            "token": issued.token,
            "expires_at": issued.record.expires_at.isoformat(),
        }

    @app.get("/api/characters", dependencies=[Depends(_auth)])
    def characters():
        from scripts.character_registry import list_characters

        items = []
        records = list_characters()
        default_character_id = os.getenv("ADMIN_DEFAULT_CHARACTER_ID", "default").strip().lower() or "default"
        records.sort(key=lambda record: (str(record.get("id")) != default_character_id, str(record.get("display_name"))))
        for record in records:
            context = CharacterContext.from_record(record)
            store = store_for_context(context)
            with closing(sqlite3.connect(store.database_path)) as connection:
                snapshot_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='persona_snapshot'"
                ).fetchone()
                active = (
                    connection.execute(
                        "SELECT version, note FROM persona_snapshot WHERE character_id=? AND is_active=1",
                        (context.character_id,),
                    ).fetchone()
                    if snapshot_table
                    else None
                )
                contacts = int(connection.execute("SELECT COUNT(*) FROM contacts WHERE enabled=1").fetchone()[0])
            items.append(
                {
                    "id": context.character_id,
                    "display_name": context.display_name,
                    "account_id": str(record.get("account_id") or ""),
                    "account_label": str(record.get("account_label") or ""),
                    "wechat_window_nickname": str(record.get("wechat_window_nickname") or ""),
                    "wechat_id": str(record.get("wechat_id") or ""),
                    "owner_remark": str(record.get("owner_remark") or ""),
                    "binding_enabled": bool(record.get("binding_enabled")),
                    "identity_confirmed": bool(record.get("identity_confirmed")),
                    "binding_version": int(record.get("binding_version") or 1),
                    "enabled": bool(record.get("enabled")),
                    "persona_status": str(record.get("persona_status") or "draft"),
                    "active_snapshot_version": int(active[0]) if active else None,
                    "active_snapshot_note": str(active[1]) if active else "",
                    "enabled_contact_count": contacts,
                }
            )
        return {"items": items}

    @app.get("/api/overview", dependencies=[Depends(_auth)])
    def overview(request: Request):
        from scripts.character_registry import list_characters

        context = request.state.character_context
        store = store_for_context(context)
        with closing(sqlite3.connect(store.database_path)) as connection:
            pending_materials = int(connection.execute(
                "SELECT COUNT(*) FROM material_reviews WHERE decision='pending'"
            ).fetchone()[0])
            enabled_contacts = int(connection.execute(
                "SELECT COUNT(*) FROM contacts WHERE enabled=1"
            ).fetchone()[0])
            inbound = int(connection.execute(
                "SELECT COUNT(*) FROM inbound_events WHERE status IN ('pending','aggregating','processing')"
            ).fetchone()[0])
        return {
            "character_count": len(list_characters()),
            "selected_character_id": context.character_id,
            "selected_character_name": context.display_name,
            "enabled_contact_count": enabled_contacts,
            "pending_material_reviews": pending_materials,
            "inbound_pending": inbound,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/settings", dependencies=[Depends(_auth)])
    def settings():
        from api.llm_client import embedding_health
        from core.runtime_settings import runtime_value

        embedding = embedding_health()
        return {
            "admin_username": os.getenv("ADMIN_USERNAME", "owner").strip() or "owner",
            "session_timeout_minutes": _positive_int_env("OWNER_SESSION_TTL_SECONDS", 14_400) // 60,
            "owner_credential_mode": owner_credential_mode(),
            "deepseek_model": runtime_value("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            "embedding_model": embedding.get("model", "BAAI/bge-m3"),
            "embedding_provider": embedding.get("provider", "not-loaded"),
            "log_path": "data/logs/soul-mate-core.log",
            "log_encoding": "UTF-8",
            "log_retention_files": 7,
            "pwa_chat_path": "/chat",
            "pwa_token_configured": bool(runtime_value("PWA_CHAT_TOKEN", "").strip()),
            "lan_access_enabled": os.getenv("SOUL_MATE_BIND_ADDRESS", "0.0.0.0").strip() != "127.0.0.1",
        }

    def _pwa_target(character_id: str):
        from scripts.character_registry import get_character, get_character_context

        normalized = str(character_id or "default").strip().lower() or "default"
        record = get_character(normalized)
        context = get_character_context(normalized)
        if record is None or context is None or not bool(record.get("enabled")):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="人物不存在或已停用")
        binding = get_pwa_contact_binding(context.character_id)
        if binding is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="请先在管理台联系人页选择“在 PWA 中接续此人”",
            )
        store = store_for_context(context)
        contact = store.get_contact(binding.contact_id)
        if contact is None or not contact.enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="PWA 接续联系人不存在或已停用，请在管理台重新选择",
            )
        return context, store, contact

    def _pwa_auth(x_pwa_token: str = Header(default="", alias="X-PWA-Token")) -> str:
        from core.runtime_settings import runtime_value

        expected = runtime_value("PWA_CHAT_TOKEN", "").strip()
        if expected and not secrets.compare_digest(x_pwa_token.strip(), expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="PWA 对话口令无效")
        return "pwa"

    @app.get("/api/pwa/status")
    def pwa_status():
        from core.runtime_settings import runtime_value

        return {"auth_required": bool(runtime_value("PWA_CHAT_TOKEN", "").strip())}

    @app.get("/api/pwa/profiles", dependencies=[Depends(_pwa_auth)])
    def pwa_profiles():
        from scripts.character_registry import list_characters

        items = []
        for record in list_characters(include_disabled=False):
            context = CharacterContext.from_record(record)
            binding = get_pwa_contact_binding(context.character_id)
            if binding is None:
                continue
            contact = store_for_context(context).get_contact(binding.contact_id)
            if contact is None or not contact.enabled:
                continue
            items.append(
                {
                    "id": context.character_id,
                    "display_name": context.display_name,
                    "contact_display_name": contact.display_name,
                }
            )
        return {"items": items}

    @app.get("/api/pwa/profile", dependencies=[Depends(_pwa_auth)])
    def pwa_profile(character_id: str = "default"):
        context, _, contact = _pwa_target(character_id)
        return {
            "id": context.character_id,
            "display_name": context.display_name,
            "contact_display_name": contact.display_name,
        }

    @app.get("/api/pwa/history", dependencies=[Depends(_pwa_auth)])
    def pwa_history(
        character_id: str = "default",
        limit: int = 40,
        before_created_at: str | None = None,
        before_id: str | None = None,
    ):
        _, store, contact = _pwa_target(character_id)
        timeline, has_more = ContactRuntimeStore(store.database_path).get_timeline_page(
            contact.id,
            limit=max(1, min(limit, 100)),
            before_created_at=before_created_at,
            before_id=before_id,
        )
        oldest = timeline[-1] if timeline else None
        return {
            "messages": [
                {
                    "id": item["id"],
                    "role": item["role"],
                    "content": item["content"],
                    "created_at": item["created_at"],
                }
                for item in reversed(timeline)
            ],
            "has_more": has_more,
            "next_before_created_at": oldest["created_at"] if oldest else None,
            "next_before_id": oldest["id"] if oldest else None,
        }

    @app.post("/api/pwa/send", dependencies=[Depends(_pwa_auth)])
    async def pwa_send(body: PwaChatSendRequest, request: Request):
        context, store, contact = _pwa_target(body.character_id)
        event_id = f"pwa:{body.message_id}"
        service = ContactConversationService(
            character_context=context,
            bridge_store=store,
            runtime_store=ContactRuntimeStore(store.database_path),
        )
        result = await service.process_direct(
            contact_id=contact.id,
            message=body.message,
            source_event_id=event_id,
            conversation_id=event_id,
        )
        chunks = result.chunks if result else []
        _audit(
            request,
            actor="pwa",
            action="pwa.chat",
            scope_type="conversation",
            outcome="success" if chunks else "skipped",
            resource_id=context.character_id,
            character_id=context.character_id,
        )
        return {
            "reply": "\n".join(chunks) if chunks else None,
            "chunks": chunks,
            "status": "ok" if chunks else "skipped",
        }

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        path = _STATIC_DIR / "sw.js"
        if path.is_file():
            script = path.read_text(encoding="utf-8")
            index = _STATIC_DIR / "index.html"
            revision_source = index.read_bytes() if index.is_file() else script.encode("utf-8")
            revision = hashlib.sha256(revision_source).hexdigest()[:12]
            script = script.replace(
                "soul-mate-static-v3",
                f"soul-mate-static-{revision}",
                1,
            )
            return Response(
                content=script,
                media_type="application/javascript",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Service-Worker-Allowed": "/",
                },
            )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="service worker not found")

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str = ""):
        if path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        index = _STATIC_DIR / "index.html"
        if index.is_file():
            # iOS chooses the manifest from the HTML document when adding a
            # site to the Home Screen.  The admin shell and chat shell are
            # different apps, so they must advertise different start URLs.
            # Keep the built index as the single source, replacing only the
            # manifest href for the route being served.
            content = index.read_text(encoding="utf-8")
            manifest = (
                "/static/chat-manifest.webmanifest"
                if path.strip("/").split("/", 1)[0] == "chat"
                else "/static/admin-manifest.webmanifest"
            )
            content = content.replace(
                "/static/manifest.webmanifest",
                manifest,
                1,
            )
            return HTMLResponse(
                content=content,
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        raise HTTPException(status_code=404, detail="管理台尚未构建")

    return app
