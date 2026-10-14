from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.character_context import (
    CharacterContext,
    CharacterContextNotBoundError,
    character_scope,
    require_character_context,
)
from memory import fact_store
from memory.fact_store import MemoryEvidence
from scripts.init_db import init


async def _embedding(_: str) -> list[float]:
    return [0.1, 0.2, 0.3]


async def _successful_vector_write(memory, _: list[float]) -> None:
    context = require_character_context()
    fact_store.confirm_memory_vector_index(
        context,
        memory.fact_id,
        index_token=memory.index_token,
    )


class MemoryFactTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_dir.cleanup)
        self.root = Path(self._temporary_dir.name)
        self.environment = patch.dict(
            os.environ,
            {"SOUL_MATE_ROOT": str(self.root)},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.first = CharacterContext("role-a", "角色甲", self.root / "characters" / "role-a")
        self.second = CharacterContext("role-b", "角色乙", self.root / "characters" / "role-b")
        init(data_dir=self.first.workspace_dir)
        init(data_dir=self.second.workspace_dir)

    def _user_message(self, context: CharacterContext, content: str) -> int:
        from memory import manager

        with character_scope(context):
            return asyncio.run(manager.save_message("user", content))

    def _manual_candidate(self, context: CharacterContext, content: str = "对方喜欢周末安静休息"):
        return fact_store.create_memory_candidate(
            context,
            content=content,
            memory_type="preference",
            importance=4,
            valence=0,
            arousal=0.3,
            source_kind="manual",
            created_by="admin-user",
            evidence=(MemoryEvidence(None, "admin", "管理员确认：" + content),),
        )

    def _published_manual_fact(self, context: CharacterContext):
        from memory.fact_service import index_and_publish_fact

        candidate = self._manual_candidate(context)
        verified = fact_store.verify_manual_evidence(context, candidate.id, actor="admin-user")
        with patch(
            "memory.fact_service.vector_store.add_memory",
            new=_successful_vector_write,
        ):
            return asyncio.run(index_and_publish_fact(
                context,
                verified.id,
                embedder=_embedding,
            ))

    def test_conversation_evidence_and_index_token_gate_publication(self) -> None:
        message_id = self._user_message(self.first, "我喜欢手冲咖啡，不太喝奶茶。")
        candidate = fact_store.create_memory_candidate(
            self.first,
            content="对方偏好手冲咖啡",
            memory_type="preference",
            importance=4,
            valence=0.4,
            arousal=0.2,
            source_kind="conversation",
            created_by="conversation_memory_extractor",
            evidence=(MemoryEvidence(message_id, "user", "我喜欢手冲咖啡"),),
        )

        verified = fact_store.verify_conversation_evidence(self.first, candidate.id)
        self.assertEqual(verified.status, "verified")
        self.assertEqual(fact_store.get_published_memory_facts(self.first, [candidate.id]), {})
        with self.assertRaisesRegex(ValueError, "确认向量写入"):
            fact_store.publish_memory_fact(self.first, candidate.id)

        indexing = fact_store.begin_memory_index(self.first, candidate.id)
        with self.assertRaisesRegex(ValueError, "确认向量写入"):
            fact_store.publish_memory_fact(self.first, candidate.id)
        with self.assertRaisesRegex(ValueError, "索引凭据"):
            fact_store.confirm_memory_vector_index(
                self.first,
                candidate.id,
                index_token="incorrect-token",
            )

        from memory.fact_service import index_and_publish_fact

        with patch(
            "memory.fact_service.vector_store.add_memory",
            new=_successful_vector_write,
        ):
            published = asyncio.run(index_and_publish_fact(
                self.first,
                indexing.id,
                embedder=_embedding,
            ))
        self.assertEqual(published.status, "published")
        self.assertEqual(published.vector_status, "indexed")
        self.assertIn(candidate.id, fact_store.get_published_memory_facts(self.first, [candidate.id]))

    def test_invalid_conversation_evidence_is_rejected_and_never_retrievable(self) -> None:
        message_id = self._user_message(self.first, "今天下雨，我在家。")
        candidate = fact_store.create_memory_candidate(
            self.first,
            content="对方讨厌下雨",
            memory_type="emotion",
            importance=2,
            valence=-0.2,
            arousal=0.3,
            source_kind="conversation",
            created_by="conversation_memory_extractor",
            evidence=(MemoryEvidence(message_id, "user", "我讨厌下雨"),),
        )

        rejected = fact_store.verify_conversation_evidence(self.first, candidate.id)
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(fact_store.get_published_memory_facts(self.first, [candidate.id]), {})
        audit = fact_store.list_memory_fact_audit(self.first, fact_id=candidate.id)
        self.assertIn("conversation_evidence_rejected", [entry["action"] for entry in audit])

    def test_vector_failure_retry_and_archive_are_auditable(self) -> None:
        from memory.fact_service import archive_fact, ingest_conversation_facts, retry_failed_fact

        message_id = self._user_message(self.first, "我下周三要去复诊。")
        candidate = {"type": "event", "content": "对方下周三复诊", "evidence_quote": "我下周三要去复诊。"}
        with patch(
            "memory.fact_service.vector_store.add_memory",
            new=AsyncMock(side_effect=RuntimeError("vector unavailable")),
        ):
            result = asyncio.run(ingest_conversation_facts(
                self.first,
                [candidate],
                source_message_id=message_id,
                embedder=_embedding,
            ))
        self.assertEqual(len(result.index_failed_ids), 1)
        fact_id = result.index_failed_ids[0]
        self.assertEqual(fact_store.get_memory_fact(self.first, fact_id).status, "index_failed")
        self.assertEqual(fact_store.get_published_memory_facts(self.first, [fact_id]), {})

        with patch("memory.fact_service.vector_store.add_memory", new=_successful_vector_write):
            retried = asyncio.run(retry_failed_fact(
                self.first,
                fact_id,
                actor="admin-user",
                reason="向量服务已恢复",
                embedder=_embedding,
            ))
        self.assertEqual(retried.status, "published")
        retry_audit = fact_store.list_memory_fact_audit(self.first, fact_id=fact_id)
        retry_event = next(entry for entry in retry_audit if entry["action"] == "vector_index_retried")
        self.assertEqual(retry_event["actor"], "admin-user")
        self.assertEqual(retry_event["payload"]["reason"], "向量服务已恢复")

        with patch(
            "memory.fact_service.vector_store.delete_fact_index",
            new=AsyncMock(side_effect=RuntimeError("cleanup unavailable")),
        ):
            archived = asyncio.run(archive_fact(
                self.first,
                fact_id,
                actor="admin-user",
                reason="事实已过期",
            ))
        self.assertEqual(archived.status, "archived")
        self.assertEqual(fact_store.get_published_memory_facts(self.first, [fact_id]), {})
        actions = [entry["action"] for entry in fact_store.list_memory_fact_audit(self.first, fact_id=fact_id)]
        self.assertIn("vector_cleanup_failed", actions)

    def test_stale_indexing_recovers_to_non_retrievable_failure(self) -> None:
        candidate = self._manual_candidate(self.first)
        verified = fact_store.verify_manual_evidence(self.first, candidate.id, actor="admin-user")
        indexing = fact_store.begin_memory_index(self.first, verified.id)
        stale_at = (datetime.now() - timedelta(hours=1)).isoformat()
        with closing(sqlite3.connect(self.first.workspace_dir / "soul_mate.db")) as connection:
            connection.execute(
                "UPDATE memory_fact SET updated_at=? WHERE id=?",
                (stale_at, indexing.id),
            )
            connection.commit()

        recovered = fact_store.recover_stale_indexing(self.first, stale_after=timedelta(minutes=1))
        recovered_fact = fact_store.get_memory_fact(self.first, indexing.id)
        self.assertEqual(recovered, [indexing.id])
        self.assertEqual(recovered_fact.status, "index_failed")
        self.assertIsNone(recovered_fact.index_token)
        self.assertEqual(fact_store.get_published_memory_facts(self.first, [indexing.id]), {})

    def test_confirmed_vector_timeout_recovers_to_published_fact(self) -> None:
        candidate = self._manual_candidate(self.first)
        verified = fact_store.verify_manual_evidence(self.first, candidate.id, actor="admin-user")
        indexing = fact_store.begin_memory_index(self.first, verified.id)
        fact_store.confirm_memory_vector_index(
            self.first,
            indexing.id,
            index_token=indexing.index_token or "",
        )
        stale_at = (datetime.now() - timedelta(hours=1)).isoformat()
        with closing(sqlite3.connect(self.first.workspace_dir / "soul_mate.db")) as connection:
            connection.execute(
                "UPDATE memory_fact SET updated_at=? WHERE id=?",
                (stale_at, indexing.id),
            )
            connection.commit()

        recovered = fact_store.recover_stale_indexing(self.first, stale_after=timedelta(minutes=1))
        recovered_fact = fact_store.get_memory_fact(self.first, indexing.id)
        self.assertEqual(recovered, [indexing.id])
        self.assertEqual(recovered_fact.status, "published")
        self.assertEqual(recovered_fact.vector_status, "indexed")
        self.assertIsNone(recovered_fact.index_token)
        self.assertIn(indexing.id, fact_store.get_published_memory_facts(self.first, [indexing.id]))
        actions = [entry["action"] for entry in fact_store.list_memory_fact_audit(self.first, fact_id=indexing.id)]
        self.assertIn("memory_publication_recovered", actions)

    def test_publication_failure_stays_visible_as_pending(self) -> None:
        from memory.fact_service import ingest_conversation_facts

        message_id = self._user_message(self.first, "我不喜欢临时改变周末的安排。")
        candidate = {
            "type": "preference",
            "content": "对方不喜欢临时改变周末安排",
            "evidence_quote": "我不喜欢临时改变周末的安排。",
        }
        with (
            patch("memory.fact_service.vector_store.add_memory", new=_successful_vector_write),
            patch(
                "memory.fact_service.fact_store.publish_memory_fact",
                side_effect=RuntimeError("SQLite 发布暂时不可用"),
            ),
        ):
            result = asyncio.run(ingest_conversation_facts(
                self.first,
                [candidate],
                source_message_id=message_id,
                embedder=_embedding,
            ))

        self.assertEqual(len(result.created_ids), 1)
        self.assertEqual(result.publication_pending_ids, result.created_ids)
        self.assertEqual(result.published_ids, ())
        pending = fact_store.get_memory_fact(self.first, result.created_ids[0])
        self.assertEqual(pending.status, "indexing")
        self.assertTrue((pending.index_token or "").startswith("written:"))
        self.assertEqual(fact_store.get_published_memory_facts(self.first, result.created_ids), {})

    def test_late_vector_write_after_archive_is_compensated(self) -> None:
        from memory import vector_store

        candidate = self._manual_candidate(self.first)
        verified = fact_store.verify_manual_evidence(self.first, candidate.id, actor="admin-user")
        indexing = fact_store.begin_memory_index(self.first, verified.id)
        memory = fact_store.to_memory(self.first, indexing)

        class LateArchiveCollection:
            def __init__(self, context: CharacterContext, fact_id: str) -> None:
                self.context = context
                self.fact_id = fact_id
                self.deleted_ids: list[str] = []

            def upsert(self, **_: object) -> None:
                fact_store.archive_memory_fact(
                    self.context,
                    self.fact_id,
                    actor="admin-user",
                    reason="管理员在索引提交前归档",
                )

            def delete(self, *, ids: list[str]) -> None:
                self.deleted_ids.extend(ids)

        collection = LateArchiveCollection(self.first, indexing.id)
        with (
            character_scope(self.first),
            patch("memory.vector_store.get_collection", return_value=collection),
            self.assertRaisesRegex(ValueError, "向量写入确认不匹配"),
        ):
            asyncio.run(vector_store.add_memory(memory, [0.1, 0.2, 0.3]))

        self.assertEqual(collection.deleted_ids, [indexing.id])
        archived = fact_store.get_memory_fact(self.first, indexing.id)
        self.assertEqual(archived.status, "archived")
        self.assertEqual(fact_store.get_published_memory_facts(self.first, [indexing.id]), {})
        actions = [entry["action"] for entry in fact_store.list_memory_fact_audit(self.first, fact_id=indexing.id)]
        self.assertIn("vector_orphan_cleanup_succeeded", actions)

    def test_published_facts_do_not_cross_character_boundaries(self) -> None:
        published = self._published_manual_fact(self.first)
        self.assertIn(published.id, fact_store.get_published_memory_facts(self.first, [published.id]))
        self.assertEqual(fact_store.get_published_memory_facts(self.second, [published.id]), {})

    def test_retriever_refuses_an_unbound_character_context(self) -> None:
        from memory.retriever import retrieve_context

        with self.assertRaises(CharacterContextNotBoundError):
            asyncio.run(retrieve_context("测试消息"))


if __name__ == "__main__":
    unittest.main()
