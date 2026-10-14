from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from web.routes.baseline import create_baseline_router


class _FakeCompiler:
    def __init__(self) -> None:
        self.created = False
        self.activated = False

    def get_state(self):
        return {
            "review_summary": {"keep": 1},
            "active_snapshot": {"id": "active", "version": 1, "kind": "reviewed", "is_active": True},
            "draft": {
                "id": "draft-1",
                "status": "draft" if not self.activated else "activated",
                "profile_diff": [],
                "style_diff": [],
                "contact_profile_diff": [],
            } if self.created else None,
        }

    def create_draft(self, resolution, *, actor: str, note: str = ""):
        del resolution, actor, note
        self.created = True
        return self.get_state()

    def activate_draft(self, draft_id: str, *, actor: str):
        del actor
        if draft_id != "draft-1":
            raise KeyError("未找到人格基线草稿")
        self.activated = True
        state = self.get_state()
        state["active_snapshot"] = {
            "id": "active-2", "version": 2, "kind": "reviewed", "is_active": True,
        }
        return state


class PersonaBaselineApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compiler = _FakeCompiler()
        self.audits: list[dict] = []
        self.confirmations: list[dict] = []
        app = FastAPI()

        def consume(*args, **kwargs):
            del args
            self.confirmations.append(kwargs)
            return "已查看差异"

        def audit(*args, **kwargs):
            del args
            self.audits.append(kwargs)

        router = create_baseline_router(
            read_auth=lambda: "owner",
            write_auth=lambda: "owner",
            consume_confirmation=consume,
            audit=audit,
        )
        app.include_router(router)
        service_dependency = next(
            dependency.call
            for route in router.routes
            for dependency in route.dependant.dependencies
            if getattr(dependency.call, "__name__", "") == "service"
        )
        app.dependency_overrides[service_dependency] = lambda: self.compiler
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    @staticmethod
    def _body() -> dict:
        return {
            "contact_id": "contact-1",
            "addressing_style": "不固定称呼",
            "punctuation_style": "通常不用标点",
            "relationship_baseline": "普通同学",
            "known_partner_status": "当前认为对方有女朋友",
            "education_goal": "目标北京大学",
            "education_disclosure": "不主动告诉普通同学",
            "moments_excluded_are_style_evidence": True,
            "pet_status": "目前没有宠物",
            "last_shared_context": "聊过学习",
            "note": "预览",
        }

    def test_preview_and_activation_are_separate_confirmed_operations(self) -> None:
        self.assertIsNone(self.client.get("/api/persona/baseline").json()["draft"])
        created = self.client.post("/api/persona/baseline/drafts", json=self._body())
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["draft"]["status"], "draft")
        self.assertFalse(self.compiler.activated)

        activated = self.client.post("/api/persona/baseline/drafts/draft-1/activate")
        self.assertEqual(activated.status_code, 200)
        self.assertEqual(activated.json()["active_snapshot"]["version"], 2)
        self.assertTrue(self.compiler.activated)
        self.assertEqual(self.confirmations[0]["action"], "persona.baseline.activate")
        self.assertEqual(
            [item["action"] for item in self.audits],
            ["persona.baseline.preview", "persona.baseline.activate"],
        )


if __name__ == "__main__":
    unittest.main()
