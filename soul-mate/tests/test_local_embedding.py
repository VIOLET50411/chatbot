from __future__ import annotations

import asyncio
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from api import llm_client


class LocalEmbeddingTests(unittest.TestCase):
    def test_local_embedding_is_deterministic_normalized_and_stable(self) -> None:
        first = llm_client._local_embedding("你好")
        second = llm_client._local_embedding("你好")

        self.assertEqual(len(first), 1024)
        self.assertEqual(first, second)
        norm = math.sqrt(sum(value * value for value in first))
        self.assertAlmostEqual(norm, 1.0, places=5)
        self.assertNotEqual(first, llm_client._local_embedding("你好呀"))

    def test_embed_falls_back_when_local_model_fails_without_remote_call(self) -> None:
        with patch(
            "api.llm_client._semantic_local_embedding",
            side_effect=RuntimeError("embedding unavailable"),
        ):
            vector = asyncio.run(llm_client.embed("你好"))

        self.assertEqual(len(vector), 1024)
        self.assertEqual(llm_client.embedding_health()["provider"], "hash-fallback")


if __name__ == "__main__":
    unittest.main()
