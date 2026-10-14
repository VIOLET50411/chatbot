"""受限的 X / 小红书只读浏览器连接器。"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from core.runtime_settings import runtime_value


class SocialBrowserUnavailable(RuntimeError):
    """浏览器未登录、遇到验证或上游不可用。"""


def _safe_connector_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower().strip("[]")
    if parsed.scheme == "https":
        return True
    if host in {"localhost", "host.docker.internal"} or host.endswith((".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


@dataclass(frozen=True, slots=True)
class SocialBrowserSource:
    platform: str
    endpoint: str


class SocialBrowserConnector:
    def __init__(self) -> None:
        base = runtime_value("SOCIAL_BROWSER_PROXY_URL", "").strip().rstrip("/")
        self.token = runtime_value("SOCIAL_BROWSER_PROXY_TOKEN", "").strip()
        self.sources: list[SocialBrowserSource] = []
        if base and self.token and _safe_connector_url(base):
            if runtime_value("SOCIAL_BROWSER_X_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}:
                self.sources.append(SocialBrowserSource("x", f"{base}/twitter"))
            if runtime_value("SOCIAL_BROWSER_XHS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}:
                self.sources.append(SocialBrowserSource("xiaohongshu", f"{base}/xiaohongshu"))

    def configuration(self) -> dict[str, Any]:
        platforms = {source.platform for source in self.sources}
        return {
            "x_configured": "x" in platforms,
            "xiaohongshu_configured": "xiaohongshu" in platforms,
            "mode": "read_only",
        }

    async def fetch(self, source: SocialBrowserSource, client: httpx.AsyncClient, *, limit: int = 10) -> list[dict[str, str]]:
        response = await client.post(
            source.endpoint,
            headers={"Authorization": f"Bearer {self.token}"},
            json={"action": "feed", "n": max(1, min(int(limit), 20))},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise SocialBrowserUnavailable("上游返回格式无效")
        outcome = str(payload.get("outcome") or "").lower()
        if outcome == "challenge":
            raise SocialBrowserUnavailable("需要人工完成平台验证")
        if payload.get("ok") is False:
            raise SocialBrowserUnavailable(_text(payload.get("error") or payload.get("hint") or "浏览器读取失败", 200))
        raw_items: Any = []
        for key in ("items", "tweets", "notes", "results", "data"):
            if isinstance(payload.get(key), list):
                raw_items = payload[key]
                break
        result: list[dict[str, str]] = []
        for raw in raw_items[: max(1, min(int(limit), 20))]:
            if not isinstance(raw, dict):
                continue
            content = _text(raw.get("text") or raw.get("title") or raw.get("content") or raw.get("desc"), 1200)
            title = _text(raw.get("title") or content, 180)
            url = str(raw.get("url") or "").strip()
            if not content or not url.startswith("https://"):
                continue
            author = _text(raw.get("author") or raw.get("handle") or raw.get("nickname"), 100)
            result.append({
                "title": title,
                "summary": content,
                "url": url,
                "source_name": author or ("X 首页" if source.platform == "x" else "小红书发现页"),
                "published_at": _text(raw.get("time") or raw.get("published_at") or raw.get("date"), 64),
            })
        return result
