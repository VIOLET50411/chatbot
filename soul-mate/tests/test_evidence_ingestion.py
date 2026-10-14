from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.character_context import CharacterContext, character_scope
from scripts.init_db import init


class EvidenceDrivenIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_dir.cleanup)
        self.workspace = Path(self._temporary_dir.name) / "characters" / "role-a"
        init(data_dir=self.workspace)
        self.context = CharacterContext(
            character_id="role-a",
            display_name="角色甲",
            workspace_dir=self.workspace,
        )

    def test_import_generates_pending_candidates_without_mutating_runtime_snapshot(self) -> None:
        from persona.corpus_importer import import_corpus
        from persona.dossier import get_active_snapshot, list_claims

        async def import_with_fixed_analysis():
            with (
                patch(
                    "persona.corpus_importer.analyze_style",
                    new=AsyncMock(return_value={"common_words": ["视频文案词"]}),
                ),
                patch(
                    "persona.corpus_importer.extract_persona_claims",
                    new=AsyncMock(return_value=[
                        {
                            "facet": "preference",
                            "claim_key": "dance",
                            "claim_value": "喜欢跳舞",
                            "context": {"source_type": "douyin_reference"},
                            "polarity": "supports",
                            "confidence": 0.7,
                            "quote": "两眼一睁就是跳",
                        }
                    ]),
                ),
            ):
                return await import_corpus("两眼一睁就是跳", "douyin")

        with character_scope(self.context):
            before = get_active_snapshot()
            result = asyncio.run(import_with_fixed_analysis())
            after = get_active_snapshot()
            candidates = {
                claim["id"]: claim
                for claim in list_claims(include_evidence=True)
                if claim["id"] in {item["id"] for item in result["candidates"]}
            }

        self.assertTrue(result["success"])
        self.assertEqual(result["source_type"], "douyin_reference")
        self.assertEqual(before["id"], after["id"])
        self.assertTrue(all(claim["status"] == "pending" for claim in candidates.values()))
        style_claim = next(item for item in result["candidates"] if item["kind"] == "style")
        self.assertEqual(candidates[style_claim["id"]]["context"]["channel"], "reference")
        self.assertEqual(len(candidates[style_claim["id"]]["evidence"]), 1)

    def test_reviewed_styles_remain_separated_by_channel(self) -> None:
        from persona.corpus_importer import import_corpus
        from persona.dossier import create_reviewed_snapshot, review_persona_claim

        async def import_two_sources():
            with (
                patch(
                    "persona.corpus_importer.analyze_style",
                    new=AsyncMock(side_effect=[
                        {"common_words": ["聊天短词"], "tone": "轻松"},
                        {"common_words": ["视频文案词"], "tone": "意象"},
                    ]),
                ),
                patch(
                    "persona.corpus_importer.extract_persona_claims",
                    new=AsyncMock(return_value=[]),
                ),
            ):
                chat = await import_corpus("聊天素材", "chat")
                douyin = await import_corpus("抖音素材", "douyin")
            return chat, douyin

        with character_scope(self.context):
            chat, douyin = asyncio.run(import_two_sources())
            for candidate in [*chat["candidates"], *douyin["candidates"]]:
                review_persona_claim(candidate["id"], "approved")
            snapshot = create_reviewed_snapshot(note="渠道隔离测试")

        chat_style = snapshot["channel_styles"]["wechat_chat"]["style"]
        reference_style = snapshot["channel_styles"]["reference"]["style"]
        self.assertEqual(chat_style["common_words"], ["聊天短词"])
        self.assertEqual(reference_style["common_words"], ["视频文案词"])
        self.assertNotIn("视频文案词", chat_style["common_words"])


if __name__ == "__main__":
    unittest.main()
