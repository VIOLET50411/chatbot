from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from core.social_browser import SocialBrowserConnector
from host_bridge.social_browser_proxy import app


class SocialBrowserTests(unittest.TestCase):
    def test_connector_reads_structured_feed_without_exposing_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {
                "DATA_DIR": temporary,
                "SOCIAL_BROWSER_PROXY_URL": "http://host.docker.internal:8280",
                "SOCIAL_BROWSER_PROXY_TOKEN": "secret-browser-token",
                "SOCIAL_BROWSER_X_ENABLED": "true",
                "SOCIAL_BROWSER_XHS_ENABLED": "false",
            }, clear=False):
                connector = SocialBrowserConnector()

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], "Bearer secret-browser-token")
            return httpx.Response(200, json={"ok": True, "tweets": [{
                "text": "一条公开时间线内容",
                "url": "https://x.com/example/status/1",
                "handle": "@example",
                "time": "2026-08-08T10:00:00+08:00",
            }]})

        async def run() -> list[dict[str, str]]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await connector.fetch(connector.sources[0], client)

        items = asyncio.run(run())
        self.assertEqual(items[0]["source_name"], "@example")
        self.assertNotIn("secret-browser-token", str(connector.configuration()))

    def test_proxy_rejects_missing_auth_and_all_write_actions(self) -> None:
        with patch.dict(os.environ, {"SOCIAL_BROWSER_PROXY_TOKEN": "proxy-secret"}, clear=False):
            client = TestClient(app)
            self.assertEqual(client.post("/twitter", json={"action": "feed"}).status_code, 401)
            response = client.post(
                "/twitter",
                headers={"Authorization": "Bearer proxy-secret"},
                json={"action": "like", "url": "https://x.com/example/status/1"},
            )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
