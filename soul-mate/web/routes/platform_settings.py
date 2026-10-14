"""Owner 可视化调整模型、媒体、资讯和部署准备项。"""
from __future__ import annotations

import base64
import json
import os
import re
import struct
import time
import zlib
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator, model_validator

from core.character_context import character_scope
from core.external_information import get_external_information_service
from core.runtime_settings import (
    CHARACTER_SETTING_KEYS,
    EDITABLE_SETTING_KEYS,
    SECRET_SETTING_KEYS,
    get_runtime_settings_store,
)


_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _vision_test_image() -> str:
    """生成足够大的无敏感内容 PNG，避免 1×1 图片被视觉服务判为无效。"""
    width = height = 64
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend((72, 114, 196) if x < width // 2 else (242, 181, 64))
        rows.append(bytes(row))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), level=9))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _http_url(value: str, *, allow_local_http: bool = False) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请输入有效的 HTTP(S) 地址")
    if parsed.scheme == "http" and not allow_local_http and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("非本机地址必须使用 HTTPS")
    return normalized


class RssSourceInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=1000)

    @field_validator("name", "url")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _http_url(value)


class RuntimeSettingsUpdate(BaseModel):
    deepseek_api_key: str | None = Field(default=None, max_length=1000)
    deepseek_base_url: str | None = Field(default=None, max_length=1000)
    deepseek_model: str | None = Field(default=None, max_length=128)
    deepseek_planner_model: str | None = Field(default=None, max_length=128)
    deepseek_renderer_model: str | None = Field(default=None, max_length=128)
    vision_api_key: str | None = Field(default=None, max_length=1000)
    vision_base_url: str | None = Field(default=None, max_length=1000)
    vision_model: str | None = Field(default=None, max_length=128)
    audio_api_key: str | None = Field(default=None, max_length=1000)
    audio_base_url: str | None = Field(default=None, max_length=1000)
    audio_model: str | None = Field(default=None, max_length=128)
    rss_sources: list[RssSourceInput] | None = Field(default=None, max_length=20)
    external_refresh_minutes: int | None = Field(default=None, ge=5, le=1440)
    x_bearer_token: str | None = Field(default=None, max_length=4000)
    x_usernames: list[str] | None = Field(default=None, max_length=20)
    social_browser_proxy_url: str | None = Field(default=None, max_length=1000)
    social_browser_proxy_token: str | None = Field(default=None, max_length=1000)
    social_browser_x_enabled: bool | None = None
    social_browser_xhs_enabled: bool | None = None
    proactive_active_start_hour: int | None = Field(default=None, ge=0, le=23)
    proactive_active_end_hour: int | None = Field(default=None, ge=1, le=24)
    proactive_check_in_after_hours: int | None = Field(default=None, ge=1, le=168)
    proactive_cooldown_hours: int | None = Field(default=None, ge=1, le=48)
    proactive_max_daily: int | None = Field(default=None, ge=1, le=10)
    vps_target_url: str | None = Field(default=None, max_length=1000)
    pwa_chat_token: str | None = Field(default=None, max_length=1000)
    clear_secrets: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("pwa_chat_token")
    @classmethod
    def validate_pwa_token(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized and len(normalized) < 24:
            raise ValueError("PWA 口令至少需要 24 位")
        return normalized

    @field_validator("deepseek_base_url", "vision_base_url", "audio_base_url")
    @classmethod
    def validate_api_url(cls, value: str | None) -> str | None:
        return _http_url(value, allow_local_http=True) if value is not None else None

    @field_validator("social_browser_proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str | None) -> str | None:
        return _http_url(value, allow_local_http=True) if value is not None else None

    @field_validator("vps_target_url")
    @classmethod
    def validate_vps_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return "" if value is not None else None
        normalized = _http_url(value)
        if not normalized.startswith("https://"):
            raise ValueError("VPS 核心必须使用 HTTPS")
        return normalized

    @field_validator("deepseek_model", "deepseek_planner_model", "deepseek_renderer_model", "vision_model", "audio_model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized and not _MODEL_NAME.fullmatch(normalized):
            raise ValueError("模型名称格式无效")
        return normalized

    @field_validator("x_usernames")
    @classmethod
    def validate_x_usernames(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        result: list[str] = []
        for item in value:
            normalized = str(item or "").strip().lstrip("@").lower()
            if not re.fullmatch(r"[a-z0-9_]{1,15}", normalized):
                raise ValueError(f"X 用户名格式无效：{item}")
            if normalized not in result:
                result.append(normalized)
        return result

    @field_validator("clear_secrets")
    @classmethod
    def validate_clear_secrets(cls, value: list[str]) -> list[str]:
        result = list(dict.fromkeys(str(item or "").strip().upper() for item in value))
        if any(item not in SECRET_SETTING_KEYS for item in result):
            raise ValueError("包含不可清除的密钥名称")
        return result

    @model_validator(mode="after")
    def validate_active_hours(self) -> "RuntimeSettingsUpdate":
        if (
            self.proactive_active_start_hour is not None
            and self.proactive_active_end_hour is not None
            and self.proactive_active_start_hour == self.proactive_active_end_hour
        ):
            raise ValueError("主动联系开始和结束时间不能相同")
        return self


class RuntimeConnectionTest(BaseModel):
    target: Literal["dialogue", "vision", "audio", "external", "vps"]


def _friendly_connection_error(exc: BaseException) -> str:
    name = type(exc).__name__
    if "Authentication" in name or "Permission" in name:
        return "服务拒绝了密钥或账号权限"
    if "Timeout" in name:
        return "连接超时，请检查网络、地址或服务状态"
    if "Connection" in name:
        return "无法连接到目标地址"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"目标返回 HTTP {exc.response.status_code}"
    return f"连接测试失败（{name}）"


async def _run_connection_test(character_id: str, target: str) -> dict[str, Any]:
    store = get_runtime_settings_store()
    started = time.perf_counter()
    message = ""
    try:
        from scripts.character_registry import get_character_context

        context = get_character_context(character_id)
        with character_scope(context):
            if target == "dialogue":
                if not store.is_character_configured(character_id, "DEEPSEEK_API_KEY"):
                    raise ValueError("missing_dialogue_key")
                from api import llm_client

                result = await llm_client.text_chat(
                    [
                        {"role": "system", "content": "这是连通性测试，只回复 OK"},
                        {"role": "user", "content": "ping"},
                    ],
                    # Reasoning-capable models may spend a small budget before
                    # emitting the visible "OK" token. A tiny limit produces a
                    # false failure even though the provider returned HTTP 200.
                    max_tokens=256,
                )
                if not result.strip():
                    raise RuntimeError("empty_dialogue_response")
                message = "文字模型已返回有效响应"
            elif target == "vision":
                if not store.is_character_configured(character_id, "VISION_API_KEY"):
                    raise ValueError("missing_vision_key")
                from api.vision import describe_image

                result = await describe_image(_vision_test_image())
                if not result.strip():
                    raise RuntimeError("empty_vision_response")
                message = "识图模型已成功读取测试图片"
            elif target == "audio":
                key = store.get_character(character_id, "AUDIO_API_KEY", "").strip()
                if not key:
                    key = store.get_character(character_id, "VISION_API_KEY", "").strip()
                if not key:
                    raise ValueError("missing_audio_key")
                client = AsyncOpenAI(
                    api_key=key,
                    base_url=store.get_character(
                        character_id,
                        "AUDIO_BASE_URL",
                        "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    ),
                    timeout=15.0,
                    max_retries=0,
                )
                await client.models.list()
                message = "语音服务地址和密钥可访问；实际转写仍需用一段音频验收"
            elif target == "external":
                service = get_external_information_service()
                configuration = service.configuration()
                social = configuration.get("social_browser", {})
                if not configuration.get("rss_sources") and not configuration.get("x_configured") and not any(
                    bool(value) for value in social.values() if isinstance(value, bool)
                ):
                    raise ValueError("missing_external_source")
                counts = await service.refresh(force=True)
                if counts.get("failed", 0) and not counts.get("items", 0):
                    raise RuntimeError("all_external_sources_failed")
                message = f"资讯读取完成，本次获得 {counts.get('items', 0)} 条"
            elif target == "vps":
                target_url = store.get("VPS_TARGET_URL", "").strip().rstrip("/")
                if not target_url:
                    raise ValueError("missing_vps_url")
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(12.0, connect=5.0),
                    follow_redirects=False,
                ) as client:
                    response = await client.get(f"{target_url}/healthz")
                    response.raise_for_status()
                    payload = response.json()
                if payload.get("status") != "ok":
                    raise RuntimeError("invalid_vps_health")
                message = "备用 VPS 健康检查通过"
            else:
                raise ValueError("unknown_target")
    except ValueError as exc:
        missing = {
            "missing_dialogue_key": "请先保存 DeepSeek API Key",
            "missing_vision_key": "请先保存识图 API Key",
            "missing_audio_key": "请先保存语音或识图 API Key",
            "missing_external_source": "请先配置至少一个 RSS、X 或只读社交来源",
            "missing_vps_url": "请先填写并保存备用 VPS HTTPS 地址",
        }
        message = missing.get(str(exc), _friendly_connection_error(exc))
        return {
            "target": target,
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": message,
        }
    except Exception as exc:
        return {
            "target": target,
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": _friendly_connection_error(exc),
        }
    return {
        "target": target,
        "ok": True,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "message": message,
    }


def _integer(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _view(character_id: str) -> dict[str, Any]:
    store = get_runtime_settings_store()
    rss_raw = store.get_character(character_id, "EXTERNAL_RSS_FEEDS_JSON", "[]")
    try:
        rss_sources = json.loads(rss_raw)
    except json.JSONDecodeError:
        rss_sources = []
    if not isinstance(rss_sources, list):
        rss_sources = []
    current_mode = os.getenv("SOUL_MATE_RUNTIME_MODE", "local").strip().lower()
    if current_mode not in {"local", "vps"}:
        current_mode = "local"
    return {
        "dialogue": {
            "provider": "DeepSeek",
            "api_key_configured": store.is_character_configured(character_id, "DEEPSEEK_API_KEY"),
            "base_url": store.get_character(character_id, "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            "model": store.get_character(character_id, "DEEPSEEK_MODEL", "deepseek-v4-pro"),
            "planner_model": store.get_character(character_id, "DEEPSEEK_PLANNER_MODEL", ""),
            "renderer_model": store.get_character(character_id, "DEEPSEEK_RENDERER_MODEL", ""),
        },
        "vision": {
            "provider": "阿里云百炼",
            "api_key_configured": store.is_character_configured(character_id, "VISION_API_KEY"),
            "base_url": store.get_character(character_id, "VISION_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "model": store.get_character(character_id, "VISION_MODEL", "qwen3.7-flash"),
        },
        "audio": {
            "provider": "微信 Plus / 阿里云百炼",
            "api_key_configured": store.is_character_configured(character_id, "AUDIO_API_KEY"),
            "uses_vision_key_fallback": not store.is_character_configured(character_id, "AUDIO_API_KEY") and store.is_character_configured(character_id, "VISION_API_KEY"),
            "base_url": store.get_character(character_id, "AUDIO_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "model": store.get_character(character_id, "AUDIO_MODEL", "qwen3-asr-flash"),
            "wechat_voice_mode": "plus_to_text",
        },
        "external_information": {
            "rss_sources": rss_sources,
            "refresh_minutes": _integer(store.get_character(character_id, "EXTERNAL_REFRESH_MINUTES", "30"), 30),
            "x_bearer_token_configured": store.is_character_configured(character_id, "X_BEARER_TOKEN"),
            "x_usernames": [item for item in store.get_character(character_id, "X_FOLLOW_USERNAMES", "").split(",") if item],
            "social_browser_proxy_url": store.get("SOCIAL_BROWSER_PROXY_URL", "http://host.docker.internal:8280"),
            "social_browser_proxy_token_configured": store.is_configured("SOCIAL_BROWSER_PROXY_TOKEN"),
            "social_browser_x_enabled": store.get("SOCIAL_BROWSER_X_ENABLED", "false").lower() == "true",
            "social_browser_xhs_enabled": store.get("SOCIAL_BROWSER_XHS_ENABLED", "false").lower() == "true",
        },
        "proactive_defaults": {
            "active_start_hour": _integer(store.get("PROACTIVE_ACTIVE_START_HOUR", "8"), 8),
            "active_end_hour": _integer(store.get("PROACTIVE_ACTIVE_END_HOUR", "24"), 24),
            "check_in_after_hours": _integer(store.get("PROACTIVE_CHECK_IN_AFTER_HOURS", "8"), 8),
            "cooldown_hours": _integer(store.get("PROACTIVE_COOLDOWN_HOURS", "4"), 4),
            "max_daily": _integer(store.get("PROACTIVE_MAX_DAILY", "4"), 4),
        },
        "deployment": {
            "current_mode": current_mode,
            "vps_target_url": store.get("VPS_TARGET_URL", ""),
            "automatic_switch_supported": False,
            "reason": "数据迁移和单写源确认必须在 Windows 宿主执行，管理台负责准备和显示配置。",
        },
        "pwa": {
            "token_configured": store.is_configured("PWA_CHAT_TOKEN"),
            "token_is_strong": len(store.get("PWA_CHAT_TOKEN", "").strip()) >= 24,
            "chat_path": "/chat",
        },
        "secret_storage": {
            "mode": store.encryption_mode,
            "encrypted_at_rest": True,
        },
        "character_id": character_id,
    }


def create_platform_settings_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/settings/runtime", tags=["owner-runtime-settings"])

    @router.get("")
    def read_settings(request: Request, owner: str = Depends(read_auth)) -> dict[str, Any]:
        del owner
        return _view(request.state.character_context.character_id)

    @router.put("")
    def update_settings(
        body: RuntimeSettingsUpdate,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        field_map = {
            "deepseek_api_key": "DEEPSEEK_API_KEY",
            "deepseek_base_url": "DEEPSEEK_BASE_URL",
            "deepseek_model": "DEEPSEEK_MODEL",
            "deepseek_planner_model": "DEEPSEEK_PLANNER_MODEL",
            "deepseek_renderer_model": "DEEPSEEK_RENDERER_MODEL",
            "vision_api_key": "VISION_API_KEY",
            "vision_base_url": "VISION_BASE_URL",
            "vision_model": "VISION_MODEL",
            "audio_api_key": "AUDIO_API_KEY",
            "audio_base_url": "AUDIO_BASE_URL",
            "audio_model": "AUDIO_MODEL",
            "external_refresh_minutes": "EXTERNAL_REFRESH_MINUTES",
            "x_bearer_token": "X_BEARER_TOKEN",
            "social_browser_proxy_url": "SOCIAL_BROWSER_PROXY_URL",
            "social_browser_proxy_token": "SOCIAL_BROWSER_PROXY_TOKEN",
            "social_browser_x_enabled": "SOCIAL_BROWSER_X_ENABLED",
            "social_browser_xhs_enabled": "SOCIAL_BROWSER_XHS_ENABLED",
            "proactive_active_start_hour": "PROACTIVE_ACTIVE_START_HOUR",
            "proactive_active_end_hour": "PROACTIVE_ACTIVE_END_HOUR",
            "proactive_check_in_after_hours": "PROACTIVE_CHECK_IN_AFTER_HOURS",
            "proactive_cooldown_hours": "PROACTIVE_COOLDOWN_HOURS",
            "proactive_max_daily": "PROACTIVE_MAX_DAILY",
            "vps_target_url": "VPS_TARGET_URL",
            "pwa_chat_token": "PWA_CHAT_TOKEN",
        }
        supplied = body.model_dump(exclude_unset=True)
        values: dict[str, str] = {}
        for field, key in field_map.items():
            if field not in supplied:
                continue
            value = supplied[field]
            if key in SECRET_SETTING_KEYS and not str(value or "").strip():
                continue
            values[key] = "true" if value is True else "false" if value is False else str(value or "").strip()
        if body.rss_sources is not None:
            values["EXTERNAL_RSS_FEEDS_JSON"] = json.dumps(
                [item.model_dump() for item in body.rss_sources],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if body.x_usernames is not None:
            values["X_FOLLOW_USERNAMES"] = ",".join(body.x_usernames)
        if any(key not in EDITABLE_SETTING_KEYS for key in values):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="包含不可修改的运行设置")
        store = get_runtime_settings_store()
        character_id = request.state.character_context.character_id
        character_values = {key: value for key, value in values.items() if key in CHARACTER_SETTING_KEYS}
        global_values = {key: value for key, value in values.items() if key not in CHARACTER_SETTING_KEYS}
        character_clear = [key for key in body.clear_secrets if key in CHARACTER_SETTING_KEYS]
        global_clear = [key for key in body.clear_secrets if key not in CHARACTER_SETTING_KEYS]
        try:
            store.set_character_many(character_id, character_values, actor=owner)
            if character_id == "default":
                store.set_many(character_values, actor=owner)
            store.set_many(global_values, actor=owner)
            store.clear_character(character_id, character_clear, actor=owner)
            if character_id == "default":
                store.clear(character_clear, actor=owner)
            store.clear(global_clear, actor=owner)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="runtime_settings.update",
            scope_type="runtime_settings",
            outcome="success",
            after={
                "updated_setting_names": sorted(values),
                "cleared_setting_names": sorted(body.clear_secrets),
            },
            character_id=character_id,
        )
        return _view(character_id)

    @router.post("/test")
    async def test_connection(
        body: RuntimeConnectionTest,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        character_id = request.state.character_context.character_id
        result = await _run_connection_test(character_id, body.target)
        audit(
            request,
            actor=owner,
            action="runtime_settings.connection_test",
            scope_type="runtime_settings",
            outcome="success" if result["ok"] else "failed",
            after={
                "target": body.target,
                "ok": result["ok"],
                "latency_ms": result["latency_ms"],
            },
            character_id=character_id,
        )
        return result

    return router
