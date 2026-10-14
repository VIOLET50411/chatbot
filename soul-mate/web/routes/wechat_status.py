"""Owner 管理台的微信桥接与媒体运行状态。"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from bridge.v1_store import BridgeStore
from web.character_access import store_from_request


class BridgeAgentStatus(BaseModel):
    online: bool
    agent_id: str | None
    last_seen_at: str | None
    wechat_version: str
    bridge_version: str
    plus_active: bool
    window_ready: bool
    capabilities: list[str]


class WechatStatusView(BaseModel):
    bridge: BridgeAgentStatus
    enabled_contacts: int
    inbound_pending: int
    outbox: dict[str, int]
    attachments: dict[str, int]
    stickers: dict[str, int]
    moments: dict[str, int]


def create_wechat_status_router(*, read_auth: Callable[..., str]) -> APIRouter:
    router = APIRouter(prefix="/api/wechat", tags=["owner-wechat"])

    def store(request: Request) -> BridgeStore:
        return store_from_request(request)

    @router.get("/status", response_model=WechatStatusView)
    def status(
        owner: str = Depends(read_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> WechatStatusView:
        del owner
        connection = sqlite3.connect(bridge_store.database_path)
        connection.row_factory = sqlite3.Row
        try:
            heartbeat = connection.execute(
                """
                SELECT agent_id, bridge_version, wechat_version, plus_active,
                       window_ready, capabilities_json, last_seen_at
                FROM bridge_heartbeats
                ORDER BY last_seen_at DESC LIMIT 1
                """
            ).fetchone()
            now = datetime.now(timezone.utc)
            if heartbeat is None:
                bridge = BridgeAgentStatus(
                    online=False,
                    agent_id=None,
                    last_seen_at=None,
                    wechat_version="",
                    bridge_version="",
                    plus_active=False,
                    window_ready=False,
                    capabilities=[],
                )
            else:
                try:
                    last_seen = datetime.fromisoformat(str(heartbeat["last_seen_at"]))
                    if last_seen.tzinfo is None:
                        last_seen = last_seen.replace(tzinfo=timezone.utc)
                    online = (now - last_seen.astimezone(timezone.utc)).total_seconds() <= 30
                except ValueError:
                    online = False
                try:
                    parsed_capabilities = json.loads(str(heartbeat["capabilities_json"] or "[]"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed_capabilities = []
                bridge = BridgeAgentStatus(
                    online=online,
                    agent_id=str(heartbeat["agent_id"]),
                    last_seen_at=str(heartbeat["last_seen_at"]),
                    wechat_version=str(heartbeat["wechat_version"] or ""),
                    bridge_version=str(heartbeat["bridge_version"] or ""),
                    plus_active=bool(heartbeat["plus_active"]),
                    window_ready=bool(heartbeat["window_ready"]),
                    capabilities=[str(item) for item in parsed_capabilities if str(item).strip()]
                    if isinstance(parsed_capabilities, list)
                    else [],
                )
            outbox = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM outbound_actions GROUP BY status"
                ).fetchall()
            }
            attachments = {
                str(row["review_status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT review_status, COUNT(*) AS count FROM attachments GROUP BY review_status"
                ).fetchall()
            }
            moments = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM moment_posts GROUP BY status"
                ).fetchall()
            }
            sticker_rows = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END), 0) AS enabled
                FROM sticker_assets
                """
            ).fetchone()
            enabled_contacts = int(connection.execute(
                "SELECT COUNT(*) FROM contacts WHERE enabled=1"
            ).fetchone()[0])
            inbound_pending = int(connection.execute(
                "SELECT COUNT(*) FROM inbound_events WHERE status IN ('pending','aggregating','processing')"
            ).fetchone()[0])
            return WechatStatusView(
                bridge=bridge,
                enabled_contacts=enabled_contacts,
                inbound_pending=inbound_pending,
                outbox=outbox,
                attachments=attachments,
                stickers={"total": int(sticker_rows["total"]), "enabled": int(sticker_rows["enabled"])},
                moments=moments,
            )
        finally:
            connection.close()

    return router
