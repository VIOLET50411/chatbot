"""共享的只读外界信息缓存：RSS 与 X 公开账号，不写入人格或联系人记忆。"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx
from core.social_browser import SocialBrowserConnector
from core.runtime_settings import runtime_value


logger = logging.getLogger(__name__)
_TRIGGERS = ("新闻", "时事", "热点", "热搜", "最近发生", "最新", "推特", "twitter", " x ", "怎么看")
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class ExternalItem:
    id: str
    provider: str
    source_name: str
    title: str
    summary: str
    url: str
    published_at: str
    fetched_at: str


def _data_path() -> Path:
    configured = Path(os.getenv("DATA_DIR", "data"))
    root = configured if configured.is_absolute() else Path(__file__).resolve().parents[1] / configured
    return root.resolve() / "platform" / "external_information.db"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(timezone.utc).isoformat()


def _safe_public_https(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower().strip("[]")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)


def _clean_text(value: object, limit: int) -> str:
    text = _TAG.sub(" ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _parse_time(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return _iso()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return _iso()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _iso(parsed)


class ExternalInformationService:
    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path) if database_path else _data_path()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS external_items (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    url TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_external_items_recent
                    ON external_items(published_at DESC, fetched_at DESC);
                CREATE TABLE IF NOT EXISTS external_refresh_state (
                    source_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    last_attempt_at TEXT NOT NULL,
                    last_success_at TEXT,
                    error_summary TEXT NOT NULL DEFAULT '',
                    item_count INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    @staticmethod
    def rss_sources() -> list[dict[str, str]]:
        raw = runtime_value("EXTERNAL_RSS_FEEDS_JSON", "[]").strip() or "[]"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("EXTERNAL_RSS_FEEDS_JSON 不是有效 JSON，RSS 外界信息已跳过")
            return []
        if not isinstance(parsed, list):
            return []
        sources: list[dict[str, str]] = []
        for item in parsed[:20]:
            if not isinstance(item, dict):
                continue
            name = _clean_text(item.get("name"), 100)
            url = str(item.get("url") or "").strip()
            if name and _safe_public_https(url):
                sources.append({"name": name, "url": url})
        return sources

    @staticmethod
    def x_usernames() -> list[str]:
        result: list[str] = []
        for raw in runtime_value("X_FOLLOW_USERNAMES", "").split(","):
            username = raw.strip().lstrip("@").lower()
            if re.fullmatch(r"[a-z0-9_]{1,15}", username) and username not in result:
                result.append(username)
        return result[:20]

    def configuration(self) -> dict[str, Any]:
        rss = self.rss_sources()
        usernames = self.x_usernames()
        return {
            "rss_sources": [{"name": item["name"], "url": item["url"]} for item in rss],
            "x_usernames": usernames,
            "x_configured": bool(runtime_value("X_BEARER_TOKEN", "").strip() and usernames),
            "refresh_minutes": self._refresh_minutes(),
            "social_browser": SocialBrowserConnector().configuration(),
        }

    @staticmethod
    def _refresh_minutes() -> int:
        try:
            value = int(runtime_value("EXTERNAL_REFRESH_MINUTES", "30"))
        except ValueError:
            value = 30
        return max(5, min(value, 1440))

    def _due(self, source_key: str, *, force: bool) -> bool:
        if force:
            return True
        with self._connection() as connection:
            row = connection.execute(
                "SELECT last_attempt_at FROM external_refresh_state WHERE source_key=?",
                (source_key,),
            ).fetchone()
        if row is None:
            return True
        try:
            previous = datetime.fromisoformat(str(row["last_attempt_at"]))
        except ValueError:
            return True
        return _now() - previous.astimezone(timezone.utc) >= timedelta(minutes=self._refresh_minutes())

    def _record_state(self, source_key: str, *, status: str, item_count: int = 0, error: str = "") -> None:
        now = _iso()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO external_refresh_state(source_key, status, last_attempt_at, last_success_at, error_summary, item_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    status=excluded.status,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=CASE WHEN excluded.status='ok' THEN excluded.last_success_at ELSE external_refresh_state.last_success_at END,
                    error_summary=excluded.error_summary,
                    item_count=excluded.item_count
                """,
                (source_key, status, now, now if status == "ok" else None, error[:300], item_count),
            )

    def _upsert(self, items: list[ExternalItem]) -> int:
        if not items:
            return 0
        with self._connection() as connection:
            for item in items:
                fingerprint = hashlib.sha256(
                    f"{item.provider}|{item.source_name}|{item.url}|{item.title}".encode("utf-8")
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO external_items(id, provider, source_name, title, summary, url, published_at, fetched_at, fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        title=excluded.title,
                        summary=excluded.summary,
                        published_at=excluded.published_at,
                        fetched_at=excluded.fetched_at
                    """,
                    (item.id, item.provider, item.source_name, item.title, item.summary, item.url, item.published_at, item.fetched_at, fingerprint),
                )
        return len(items)

    async def refresh(self, *, force: bool = False) -> dict[str, int]:
        counts = {"sources": 0, "items": 0, "failed": 0}
        timeout = httpx.Timeout(12.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers={"User-Agent": "SoulMate/1.0 read-only-feed"}) as client:
            for source in self.rss_sources():
                key = f"rss:{source['url']}"
                if not self._due(key, force=force):
                    continue
                counts["sources"] += 1
                try:
                    response = await client.get(source["url"])
                    response.raise_for_status()
                    items = self._parse_rss(response.content, source["name"])
                    counts["items"] += self._upsert(items)
                    self._record_state(key, status="ok", item_count=len(items))
                except Exception as exc:
                    counts["failed"] += 1
                    self._record_state(key, status="failed", error=type(exc).__name__)
                    logger.warning("RSS 外界信息刷新失败 source=%s type=%s", source["name"], type(exc).__name__)
            if runtime_value("X_BEARER_TOKEN", "").strip() and self.x_usernames():
                key = "x:public-users"
                if self._due(key, force=force):
                    counts["sources"] += 1
                    try:
                        items = await self._fetch_x(client)
                        counts["items"] += self._upsert(items)
                        self._record_state(key, status="ok", item_count=len(items))
                    except Exception as exc:
                        counts["failed"] += 1
                        self._record_state(key, status="failed", error=type(exc).__name__)
                        logger.warning("X 外界信息刷新失败 type=%s", type(exc).__name__)
            social = SocialBrowserConnector()
            for source in social.sources:
                key = f"social-browser:{source.platform}"
                if not self._due(key, force=force):
                    continue
                counts["sources"] += 1
                try:
                    entries = await social.fetch(source, client, limit=10)
                    now = _iso()
                    items = [ExternalItem(
                        id=hashlib.sha256(f"social-browser|{source.platform}|{entry['url']}".encode("utf-8")).hexdigest(),
                        provider=f"social_browser_{source.platform}",
                        source_name=entry["source_name"],
                        title=entry["title"],
                        summary=entry["summary"],
                        url=entry["url"],
                        published_at=_parse_time(entry["published_at"]),
                        fetched_at=now,
                    ) for entry in entries]
                    counts["items"] += self._upsert(items)
                    self._record_state(key, status="ok", item_count=len(items))
                except Exception as exc:
                    counts["failed"] += 1
                    self._record_state(key, status="failed", error=type(exc).__name__)
                    logger.warning("社交浏览器读取失败 platform=%s type=%s", source.platform, type(exc).__name__)
        self._prune()
        return counts

    @staticmethod
    def _parse_rss(content: bytes, source_name: str) -> list[ExternalItem]:
        root = ET.fromstring(content)
        candidates = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        now = _iso()
        items: list[ExternalItem] = []
        for node in candidates[:20]:
            def find_text(*names: str) -> str:
                for name in names:
                    found = node.find(name)
                    if found is not None and found.text:
                        return found.text
                return ""
            title = _clean_text(find_text("title", "{http://www.w3.org/2005/Atom}title"), 300)
            summary = _clean_text(find_text("description", "summary", "{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content"), 1200)
            link = find_text("link")
            if not link:
                atom_link = node.find("{http://www.w3.org/2005/Atom}link")
                link = str(atom_link.attrib.get("href") or "") if atom_link is not None else ""
            if not title or not _safe_public_https(link):
                continue
            published = _parse_time(find_text("pubDate", "published", "updated", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated"))
            identifier = hashlib.sha256(f"rss|{source_name}|{link}".encode("utf-8")).hexdigest()
            items.append(ExternalItem(identifier, "rss", source_name, title, summary, link, published, now))
        return items

    async def _fetch_x(self, client: httpx.AsyncClient) -> list[ExternalItem]:
        token = runtime_value("X_BEARER_TOKEN", "").strip()
        headers = {"Authorization": f"Bearer {token}"}
        usernames = self.x_usernames()
        lookup = await client.get(
            "https://api.x.com/2/users/by",
            params={"usernames": ",".join(usernames), "user.fields": "username,name"},
            headers=headers,
        )
        lookup.raise_for_status()
        users = lookup.json().get("data", [])
        now = _iso()
        result: list[ExternalItem] = []
        for user in users:
            user_id = str(user.get("id") or "")
            username = str(user.get("username") or "").strip()
            if not user_id or not username:
                continue
            response = await client.get(
                f"https://api.x.com/2/users/{user_id}/tweets",
                params={"max_results": 5, "exclude": "retweets,replies", "tweet.fields": "created_at"},
                headers=headers,
            )
            response.raise_for_status()
            for post in response.json().get("data", [])[:5]:
                post_id = str(post.get("id") or "")
                text = _clean_text(post.get("text"), 1200)
                if not post_id or not text:
                    continue
                result.append(ExternalItem(
                    id=hashlib.sha256(f"x|{post_id}".encode("utf-8")).hexdigest(),
                    provider="x",
                    source_name=f"@{username}",
                    title=text[:160],
                    summary=text,
                    url=f"https://x.com/{username}/status/{post_id}",
                    published_at=_parse_time(post.get("created_at")),
                    fetched_at=now,
                ))
        return result

    def _prune(self) -> None:
        threshold = _iso(_now() - timedelta(days=30))
        with self._connection() as connection:
            connection.execute("DELETE FROM external_items WHERE published_at < ?", (threshold,))

    def recent_items(self, *, limit: int = 8, max_age_hours: int = 72) -> list[dict[str, str]]:
        threshold = _iso(_now() - timedelta(hours=max(1, max_age_hours)))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, provider, source_name, title, summary, url, published_at, fetched_at
                FROM external_items WHERE published_at >= ?
                ORDER BY published_at DESC, fetched_at DESC LIMIT ?
                """,
                (threshold, max(1, min(limit, 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict[str, Any]:
        with self._connection() as connection:
            states = [dict(row) for row in connection.execute(
                "SELECT * FROM external_refresh_state ORDER BY source_key"
            ).fetchall()]
            count = int(connection.execute("SELECT COUNT(*) FROM external_items").fetchone()[0])
        return {"configuration": self.configuration(), "source_states": states, "cached_item_count": count, "recent_items": self.recent_items(limit=5)}

    async def context_for(self, message: str) -> list[dict[str, str]]:
        normalized = f" {str(message or '').lower()} "
        if not any(trigger in normalized for trigger in _TRIGGERS):
            return []
        try:
            await self.refresh(force=False)
        except Exception as exc:
            logger.warning("外界信息上下文刷新失败 type=%s", type(exc).__name__)
        return self.recent_items(limit=6)


_SERVICE: ExternalInformationService | None = None


def get_external_information_service() -> ExternalInformationService:
    global _SERVICE
    path = _data_path()
    if _SERVICE is None or _SERVICE.database_path.resolve() != path.resolve():
        _SERVICE = ExternalInformationService(path)
    return _SERVICE
