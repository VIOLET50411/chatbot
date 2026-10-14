from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.external_information import ExternalInformationService


class ExternalInformationTests(unittest.TestCase):
    def test_rss_items_are_cached_with_source_and_time_without_persona_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ExternalInformationService(Path(temporary) / "external.db")
            items = service._parse_rss(
                """<?xml version="1.0" encoding="UTF-8"?>
                <rss><channel><item><title>新的模型发布</title>
                <description>这是一条带来源的测试资讯</description>
                <link>https://news.example.com/item/1</link>
                <pubDate>Fri, 07 Aug 2026 12:00:00 GMT</pubDate>
                </item></channel></rss>""".encode("utf-8"),
                "测试资讯源",
            )
            self.assertEqual(service._upsert(items), 1)
            recent = service.recent_items(limit=3, max_age_hours=10_000)

        self.assertEqual(recent[0]["source_name"], "测试资讯源")
        self.assertEqual(recent[0]["published_at"], "2026-08-07T12:00:00+00:00")
        self.assertEqual(recent[0]["url"], "https://news.example.com/item/1")

    def test_configuration_never_exposes_x_bearer_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                "DATA_DIR": temporary,
                "X_BEARER_TOKEN": "sensitive-token",
                "X_FOLLOW_USERNAMES": "XDevelopers",
            },
            clear=False,
        ):
            status = ExternalInformationService(Path(temporary) / "external.db").status()
        serialized = str(status)
        self.assertTrue(status["configuration"]["x_configured"])
        self.assertNotIn("sensitive-token", serialized)


if __name__ == "__main__":
    unittest.main()
