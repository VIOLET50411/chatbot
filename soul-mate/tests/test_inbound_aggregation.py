from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from bridge.inbound_worker import InboundAggregationWorker
from bridge.v1_models import InboundEnvelope
from bridge.v1_store import BridgeStore


def _time(offset_seconds: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


class InboundAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.store = BridgeStore(Path(self.temporary_dir.name) / "soul_mate.db")
        self.store.initialize_schema()
        self.alice = self.store.create_contact(external_id="wxid-alice", display_name="Alice")
        self.bob = self.store.create_contact(external_id="wxid-bob", display_name="Bob")

    def _record(
        self,
        *,
        event_id: str,
        contact: str,
        text: str,
        occurred_at: datetime,
    ) -> None:
        self.store.record_inbound(
            InboundEnvelope(
                event_id=event_id,
                external_contact_id=contact,
                occurred_at=occurred_at,
                type="text",
                text=text,
                attachment_refs=[],
                message_hash=(event_id.replace("-", "") + "x" * 64)[:64],
                agent_id="agent-test",
            )
        )

    def test_consecutive_messages_are_grouped_per_contact_after_quiet_window(self) -> None:
        base = _time(-20)
        self._record(event_id="alice-1", contact="wxid-alice", text="第一条", occurred_at=base)
        self._record(event_id="alice-2", contact="wxid-alice", text="第二条", occurred_at=base + timedelta(seconds=2))
        self._record(event_id="alice-3", contact="wxid-alice", text="第三条", occurred_at=base + timedelta(seconds=4))
        self._record(
            event_id="bob-1",
            contact="wxid-bob",
            text="Bob 的消息",
            occurred_at=base + timedelta(seconds=4),
        )

        too_early = self.store.claim_ready_inbound_batches(
            now=_time(1),
            debounce_seconds=4,
        )
        self.assertEqual(too_early, [])

        ready = self.store.claim_ready_inbound_batches(
            now=_time(5),
            debounce_seconds=4,
        )
        self.assertEqual(len(ready), 2)
        alice_batch = next(item for item in ready if item.contact_id == self.alice.id)
        bob_batch = next(item for item in ready if item.contact_id == self.bob.id)
        self.assertEqual(alice_batch.event_ids, ("alice-1", "alice-2", "alice-3"))
        self.assertEqual(alice_batch.combined_text, "第一条\n第二条\n第三条")
        self.assertEqual(bob_batch.event_ids, ("bob-1",))
        self.assertNotEqual(alice_batch.batch_id, bob_batch.batch_id)

    def test_worker_completes_batch_once_and_does_not_replay(self) -> None:
        self._record(
            event_id="alice-worker-1",
            contact="wxid-alice",
            text="你好",
            occurred_at=_time(-30),
        )
        service = AsyncMock()
        service.process.return_value = None
        worker = InboundAggregationWorker(
            store=self.store,
            service=service,
            debounce_seconds=0,
        )

        async def run() -> None:
            first = await worker.run_once(now=_time())
            second = await worker.run_once(now=_time(1))
            self.assertEqual(first, {"completed": 1, "failed": 0})
            self.assertEqual(second, {"completed": 0, "failed": 0})

        asyncio.run(run())
        service.process.assert_awaited_once()
        entry = self.store.get_inbound_event("alice-worker-1")
        self.assertEqual(entry["status"], "completed")

    def test_failed_batch_reuses_same_batch_id_and_can_retry_without_duplicate_key(self) -> None:
        self._record(
            event_id="alice-retry-1",
            contact="wxid-alice",
            text="重试消息",
            occurred_at=_time(-30),
        )
        service = AsyncMock()
        service.process.side_effect = [RuntimeError("temporary"), None]
        worker = InboundAggregationWorker(
            store=self.store,
            service=service,
            debounce_seconds=0,
        )

        async def run() -> None:
            failed = await worker.run_once(now=_time())
            recovered = await worker.run_once(now=_time(1))
            self.assertEqual(failed, {"completed": 0, "failed": 1})
            self.assertEqual(recovered, {"completed": 1, "failed": 0})

        asyncio.run(run())
        self.assertEqual(service.process.await_count, 2)
        first_batch_id = service.process.await_args_list[0].kwargs["batch_id"]
        second_batch_id = service.process.await_args_list[1].kwargs["batch_id"]
        self.assertEqual(first_batch_id, second_batch_id)
        self.assertEqual(self.store.get_inbound_event("alice-retry-1")["status"], "completed")

    def test_image_is_understood_once_and_cached_for_retry(self) -> None:
        content = b"fake-png"
        attachment_path = Path(self.temporary_dir.name) / "attachments" / "image.png"
        attachment_path.parent.mkdir(parents=True)
        attachment_path.write_bytes(content)
        attachment = self.store.register_attachment(
            external_contact_id="wxid-alice",
            kind="image",
            storage_path="attachments/image.png",
            sha256="1" * 64,
            mime_type="image/png",
            size_bytes=len(content),
        )
        self.store.record_inbound(
            InboundEnvelope(
                event_id="alice-image-1",
                external_contact_id="wxid-alice",
                occurred_at=_time(-30),
                type="image",
                text=None,
                attachment_refs=[attachment.id],
                message_hash="2" * 64,
                agent_id="agent-test",
            )
        )
        service = AsyncMock()
        service.process.side_effect = [RuntimeError("temporary"), None]
        vision = AsyncMock(return_value="一只趴在窗边的橘猫")
        worker = InboundAggregationWorker(
            store=self.store,
            service=service,
            debounce_seconds=0,
            vision_describer=vision,
        )

        async def run() -> None:
            self.assertEqual((await worker.run_once(now=_time()))["failed"], 1)
            self.assertEqual((await worker.run_once(now=_time(1)))["completed"], 1)

        asyncio.run(run())
        self.assertEqual(vision.await_count, 1)
        self.assertEqual(
            service.process.await_args_list[0].kwargs["message"],
            "[图片内容：一只趴在窗边的橘猫]",
        )
        self.assertEqual(
            service.process.await_args_list[1].kwargs["message"],
            "[图片内容：一只趴在窗边的橘猫]",
        )

    def test_image_caption_and_vision_are_both_kept_in_one_message(self) -> None:
        content = b"captioned-image"
        attachment_path = Path(self.temporary_dir.name) / "attachments" / "captioned.png"
        attachment_path.parent.mkdir(parents=True)
        attachment_path.write_bytes(content)
        attachment = self.store.register_attachment(
            external_contact_id="wxid-alice",
            kind="image",
            storage_path="attachments/captioned.png",
            sha256="e" * 64,
            mime_type="image/png",
            size_bytes=len(content),
        )
        self.store.record_inbound(
            InboundEnvelope(
                event_id="alice-captioned-image",
                external_contact_id="wxid-alice",
                occurred_at=_time(-30),
                type="image",
                text="caption-before-vision",
                attachment_refs=[attachment.id],
                message_hash="f" * 64,
                agent_id="agent-test",
            )
        )
        service = AsyncMock()
        worker = InboundAggregationWorker(
            store=self.store,
            service=service,
            debounce_seconds=0,
            vision_describer=AsyncMock(return_value="cat-vision"),
        )

        asyncio.run(worker.run_once(now=_time()))

        message = service.process.await_args.kwargs["message"]
        self.assertIn("caption-before-vision", message)
        self.assertIn("cat-vision", message)

    def test_vision_failure_only_degrades_media_placeholder(self) -> None:
        content = b"fake-sticker"
        attachment_path = Path(self.temporary_dir.name) / "attachments" / "sticker.png"
        attachment_path.parent.mkdir(parents=True)
        attachment_path.write_bytes(content)
        attachment = self.store.register_attachment(
            external_contact_id="wxid-alice",
            kind="sticker",
            storage_path="attachments/sticker.png",
            sha256="3" * 64,
            mime_type="image/png",
            size_bytes=len(content),
        )
        self.store.record_inbound(
            InboundEnvelope(
                event_id="alice-sticker-1",
                external_contact_id="wxid-alice",
                occurred_at=_time(-30),
                type="sticker",
                text=None,
                attachment_refs=[attachment.id],
                message_hash="4" * 64,
                agent_id="agent-test",
            )
        )
        service = AsyncMock()
        vision = AsyncMock(return_value="")
        worker = InboundAggregationWorker(
            store=self.store,
            service=service,
            debounce_seconds=0,
            vision_describer=vision,
        )

        asyncio.run(worker.run_once(now=_time()))

        self.assertEqual(service.process.await_args.kwargs["message"], "[表情包]")
        service.process.assert_awaited_once()

    def test_image_then_late_voice_is_merged_before_generation(self) -> None:
        content = b"fake-cat"
        attachment_path = Path(self.temporary_dir.name) / "attachments" / "cat.png"
        attachment_path.parent.mkdir(parents=True)
        attachment_path.write_bytes(content)
        attachment = self.store.register_attachment(
            external_contact_id="wxid-alice",
            kind="image",
            storage_path="attachments/cat.png",
            sha256="5" * 64,
            mime_type="image/png",
            size_bytes=len(content),
        )
        self.store.record_inbound(
            InboundEnvelope(
                event_id="alice-image-late-voice",
                external_contact_id="wxid-alice",
                occurred_at=_time(-30),
                type="image",
                attachment_refs=[attachment.id],
                message_hash="6" * 64,
                agent_id="agent-test",
                context_mode="validation",
            )
        )

        async def describe_and_receive_voice(path: Path, mime_type: str) -> str:
            del path, mime_type
            self.store.record_inbound(
                InboundEnvelope(
                    event_id="alice-voice-after-image",
                    external_contact_id="wxid-alice",
                    occurred_at=_time(-29),
                    type="voice",
                    text="[对方发来语音，Plus 转写：我在吃饭的店里看到的]",
                    message_hash="7" * 64,
                    agent_id="agent-test",
                    context_mode="validation",
                )
            )
            return "一只趴在椅子上的狸花猫"

        service = AsyncMock()
        worker = InboundAggregationWorker(
            store=self.store,
            service=service,
            debounce_seconds=0,
            vision_describer=describe_and_receive_voice,
        )
        asyncio.run(worker.run_once(now=_time()))

        service.process.assert_awaited_once()
        call = service.process.await_args.kwargs
        self.assertEqual(call["context_mode"], "validation")
        self.assertEqual(
            call["source_event_ids"],
            ["alice-image-late-voice", "alice-voice-after-image"],
        )
        self.assertIn("[图片内容：一只趴在椅子上的狸花猫]", call["message"])
        self.assertIn("我在吃饭的店里看到的", call["message"])

    def test_voice_then_image_is_grouped_in_original_order(self) -> None:
        self.store.record_inbound(
            InboundEnvelope(
                event_id="alice-voice-first",
                external_contact_id="wxid-alice",
                occurred_at=_time(-31),
                type="voice",
                text="[对方发来语音，Plus 转写：你看这只小猫]",
                message_hash="8" * 64,
                agent_id="agent-test",
            )
        )
        content = b"fake-cat-two"
        attachment_path = Path(self.temporary_dir.name) / "attachments" / "cat-two.png"
        attachment_path.parent.mkdir(parents=True, exist_ok=True)
        attachment_path.write_bytes(content)
        attachment = self.store.register_attachment(
            external_contact_id="wxid-alice",
            kind="image",
            storage_path="attachments/cat-two.png",
            sha256="9" * 64,
            mime_type="image/png",
            size_bytes=len(content),
        )
        self.store.record_inbound(
            InboundEnvelope(
                event_id="alice-image-second",
                external_contact_id="wxid-alice",
                occurred_at=_time(-30),
                type="image",
                attachment_refs=[attachment.id],
                message_hash="a" * 64,
                agent_id="agent-test",
            )
        )
        service = AsyncMock()
        worker = InboundAggregationWorker(
            store=self.store,
            service=service,
            debounce_seconds=0,
            vision_describer=AsyncMock(return_value="一只狸花猫"),
        )
        asyncio.run(worker.run_once(now=_time()))

        call = service.process.await_args.kwargs
        self.assertEqual(
            call["message"],
            "[对方发来语音，Plus 转写：你看这只小猫]\n[图片内容：一只狸花猫]",
        )

    def test_text_image_and_voice_wait_for_one_rich_media_turn(self) -> None:
        received = _time()
        self._record(
            event_id="alice-look-text",
            contact="wxid-alice",
            text="你看这个",
            occurred_at=received,
        )
        self.assertEqual(
            self.store.claim_ready_inbound_batches(now=received + timedelta(seconds=7)),
            [],
        )

        content = b"fake-rich-turn"
        attachment_path = Path(self.temporary_dir.name) / "attachments" / "rich-turn.png"
        attachment_path.parent.mkdir(parents=True, exist_ok=True)
        attachment_path.write_bytes(content)
        attachment = self.store.register_attachment(
            external_contact_id="wxid-alice",
            kind="image",
            storage_path="attachments/rich-turn.png",
            sha256="b" * 64,
            mime_type="image/png",
            size_bytes=len(content),
        )
        self.store.record_inbound(
            InboundEnvelope(
                event_id="alice-look-image",
                external_contact_id="wxid-alice",
                occurred_at=received + timedelta(seconds=7),
                type="image",
                attachment_refs=[attachment.id],
                message_hash="c" * 64,
                agent_id="agent-test",
            )
        )
        self.store.record_inbound(
            InboundEnvelope(
                event_id="alice-look-voice",
                external_contact_id="wxid-alice",
                occurred_at=received + timedelta(seconds=11),
                type="voice",
                text="[对方发来语音，Plus 转写：我在吃饭的店里看到的]",
                message_hash="d" * 64,
                agent_id="agent-test",
            )
        )
        batches = self.store.claim_ready_inbound_batches(
            now=_time(13),
        )
        self.assertEqual(len(batches), 1)
        self.assertEqual(
            batches[0].event_ids,
            ("alice-look-text", "alice-look-image", "alice-look-voice"),
        )


if __name__ == "__main__":
    unittest.main()
