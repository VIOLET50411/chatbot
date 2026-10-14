from __future__ import annotations

import os
import gc
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from bridge.v1_store import BridgeStore
from web.routes.materials import create_materials_router


class MaterialReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(self.temporary_dir.name)
        self.environment = patch.dict(
            os.environ,
            {"DATA_DIR": str(self.root / "data")},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        database = self.root / "data" / "soul_mate.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE source_material (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_time TEXT,
                    reliability REAL NOT NULL DEFAULT 1,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE persona_claim (
                    id TEXT PRIMARY KEY, facet TEXT, claim_key TEXT, claim_value TEXT,
                    confidence REAL, status TEXT, created_at TEXT
                );
                CREATE TABLE claim_evidence (
                    id TEXT PRIMARY KEY, claim_id TEXT, source_material_id TEXT,
                    quote_span_json TEXT, support_kind TEXT
                );
                INSERT INTO source_material VALUES
                    ('chat-1', 'chat', '聊天原文', '2026-01-01', 0.9, 0, '2026-01-01'),
                    ('moment-1', 'moments', '朋友圈原文', '2026-01-02', 0.8, 0, '2026-01-02'),
                    ('identity-1', 'canonical_setup', '身份原文', NULL, 1, 0, '2026-01-03');
                INSERT INTO persona_claim VALUES
                    ('claim-1', 'tone', 'reply_style', '短句自然', 0.91, 'approved', '2026-01-04');
                INSERT INTO claim_evidence VALUES
                    ('evidence-1', 'claim-1', 'chat-1', '{"start":0,"end":4}', 'supports');
                """
            )
        BridgeStore(database).initialize_schema()
        self.audits: list[dict] = []
        app = FastAPI()

        def audit(*args, **kwargs):
            del args
            self.audits.append(kwargs)

        app.include_router(
            create_materials_router(
                read_auth=lambda: "owner",
                write_auth=lambda: "owner",
                audit=audit,
            )
        )
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_all_existing_materials_start_pending_with_source_and_conclusions(self) -> None:
        response = self.client.get("/api/materials")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["summary"], {"pending": 3})
        chat = next(item for item in payload["items"] if item["source_material_id"] == "chat-1")
        identity = next(item for item in payload["items"] if item["source_material_id"] == "identity-1")
        self.assertEqual(chat["source_channel"], "chat")
        self.assertEqual(chat["content"], "聊天原文")
        self.assertEqual(chat["conclusions"][0]["value"], "短句自然")
        self.assertEqual(identity["source_channel"], "identity")

    def test_correct_split_and_batch_decisions_are_validated_and_audited(self) -> None:
        missing_correction = self.client.patch(
            "/api/materials/review:chat-1",
            json={"decision": "corrected"},
        )
        self.assertEqual(missing_correction.status_code, 422)

        corrected = self.client.patch(
            "/api/materials/review:chat-1",
            json={
                "decision": "corrected",
                "corrected_content": "修正后的聊天原文",
                "review_note": "修复口误",
            },
        )
        self.assertEqual(corrected.status_code, 200)
        self.assertEqual(corrected.json()["corrected_content"], "修正后的聊天原文")

        too_short_split = self.client.patch(
            "/api/materials/review:moment-1",
            json={"decision": "split", "split_contents": ["只有一段"]},
        )
        self.assertEqual(too_short_split.status_code, 422)
        split = self.client.patch(
            "/api/materials/review:moment-1",
            json={"decision": "split", "split_contents": ["公开表达", "生活事件"]},
        )
        self.assertEqual(split.status_code, 200)
        self.assertEqual(split.json()["split_contents"], ["公开表达", "生活事件"])

        batch = self.client.post(
            "/api/materials/batch",
            json={"ids": ["review:identity-1"], "decision": "keep"},
        )
        self.assertEqual(batch.status_code, 200)
        self.assertEqual(batch.json()["updated"], 1)
        self.assertEqual([item["action"] for item in self.audits], [
            "material.review",
            "material.review",
            "material.review_batch",
        ])

    def test_channel_and_text_filters_do_not_change_review_state(self) -> None:
        filtered = self.client.get(
            "/api/materials",
            params={"source_channel": "moments", "query": "朋友圈"},
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(filtered.json()["items"][0]["source_material_id"], "moment-1")
        all_items = self.client.get("/api/materials").json()["items"]
        self.assertTrue(all(item["decision"] == "pending" for item in all_items))


if __name__ == "__main__":
    unittest.main()
