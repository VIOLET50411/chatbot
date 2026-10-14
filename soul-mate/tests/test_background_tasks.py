from __future__ import annotations

import asyncio
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.background_tasks import BackgroundTaskSupervisor
from core.character_context import CharacterContext, require_character_context
from scripts.init_db import init


class BackgroundTaskSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_dir.cleanup)
        self.context = CharacterContext(
            "role-a",
            "角色甲",
            Path(self._temporary_dir.name) / "role-a",
        )

    def test_task_is_strongly_tracked_and_rebinds_its_explicit_context(self) -> None:
        async def run() -> None:
            supervisor = BackgroundTaskSupervisor()
            started = asyncio.Event()
            release = asyncio.Event()
            observed: list[CharacterContext] = []

            async def work() -> None:
                observed.append(require_character_context())
                started.set()
                await release.wait()

            task = supervisor.spawn(
                self.context,
                "context_check",
                work(),
                drain_on_shutdown=True,
            )
            self.assertIsNotNone(task)
            await started.wait()
            self.assertEqual(supervisor.pending_count, 1)
            self.assertEqual(observed, [self.context])

            release.set()
            await supervisor.wait_for_idle()
            self.assertEqual(supervisor.pending_count, 0)

        asyncio.run(run())

    def test_task_exception_is_consumed_by_the_supervisor(self) -> None:
        async def run() -> None:
            supervisor = BackgroundTaskSupervisor()
            unhandled: list[dict] = []
            loop = asyncio.get_running_loop()
            previous_handler = loop.get_exception_handler()
            loop.set_exception_handler(lambda _, details: unhandled.append(details))
            try:
                supervisor.spawn(
                    self.context,
                    "failing_work",
                    _raise_runtime_error(),
                    drain_on_shutdown=True,
                )
                await supervisor.wait_for_idle()
            finally:
                loop.set_exception_handler(previous_handler)

            self.assertEqual(unhandled, [])
            self.assertEqual(supervisor.pending_count, 0)

        with self.assertLogs("core.background_tasks", level="ERROR") as captured:
            asyncio.run(run())
        self.assertTrue(any(
            "后台任务失败 character=role-a operation=failing_work type=RuntimeError" in line
            for line in captured.output
        ))

    def test_shutdown_drains_state_work_and_cancels_send_work(self) -> None:
        async def run() -> None:
            supervisor = BackgroundTaskSupervisor()
            durable_started = asyncio.Event()
            durable_release = asyncio.Event()
            durable_finished = asyncio.Event()
            send_started = asyncio.Event()
            send_cancelled = asyncio.Event()

            async def durable_work() -> None:
                durable_started.set()
                await durable_release.wait()
                durable_finished.set()

            async def send_work() -> None:
                send_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    send_cancelled.set()
                    raise

            supervisor.spawn(
                self.context,
                "state_commit",
                durable_work(),
                drain_on_shutdown=True,
            )
            supervisor.spawn(
                self.context,
                "delayed_send",
                send_work(),
                drain_on_shutdown=False,
            )
            await durable_started.wait()
            await send_started.wait()
            durable_release.set()

            await supervisor.shutdown(drain_timeout=1.0)
            await supervisor.wait_for_idle()

            self.assertTrue(durable_finished.is_set())
            self.assertTrue(send_cancelled.is_set())
            self.assertEqual(supervisor.pending_count, 0)

        asyncio.run(run())


class EngineBackgroundTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_dir.cleanup)
        self.context = CharacterContext(
            "role-a",
            "角色甲",
            Path(self._temporary_dir.name) / "role-a",
        )
        init(self.context.workspace_dir)

    def test_post_process_failure_does_not_block_reply_or_escape_task_boundary(self) -> None:
        from core.engine import ConversationEngine

        engine = ConversationEngine()
        async def run() -> None:
            with (
                patch("persona.dossier.get_active_snapshot", return_value={
                    "is_active": True,
                    "stable_profile": {"name": "快照角色"},
                }),
                patch("core.engine.manager.save_message", new=AsyncMock(return_value=17)),
                patch("core.engine.manager.get_today_agenda", new=AsyncMock(return_value=None)),
                patch("core.engine.manager.get_recent_messages", new=AsyncMock(return_value=[])),
                patch("core.engine.retriever.retrieve_context", new=AsyncMock(return_value={})),
                patch("core.engine.prompt_builder.build_decision_prompt", new=AsyncMock(return_value=[])),
                patch("core.engine.prompt_builder.build_render_prompt", new=AsyncMock(return_value=[])),
                patch("core.engine.llm_client.structured_chat", new=AsyncMock(return_value={
                    "should_reply": True,
                    "memory_candidates": [],
                })),
                patch("core.engine.llm_client.text_chat", new=AsyncMock(return_value="收到")),
                patch("core.engine.postprocess.humanize_reply", return_value=["收到"]),
                patch("core.typing_rhythm.calculate_typing_sequence", return_value=[]),
                patch("core.reply_delay.calculate_reply_delay", return_value=0),
                patch.object(
                    engine,
                    "_async_post_process",
                    new=AsyncMock(side_effect=RuntimeError("post process failure")),
                ),
            ):
                result = await engine.process(self.context, "user-a", "你好")
                self.assertEqual(result["chunks"], ["收到"])
                await engine.wait_for_background_tasks()

        with self.assertLogs("core.background_tasks", level="ERROR") as captured:
            asyncio.run(run())

        self.assertTrue(any(
            "后台任务失败 character=role-a operation=interaction_post_process type=RuntimeError" in line
            for line in captured.output
        ))


class BackgroundTaskSupervisorSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_dir.cleanup)
        self.context = CharacterContext(
            "role-a",
            "角色甲",
            Path(self._temporary_dir.name) / "role-a",
        )

    def test_snapshot_running_entry_has_only_plain_fields(self) -> None:
        async def run() -> None:
            supervisor = BackgroundTaskSupervisor()
            started = asyncio.Event()
            release = asyncio.Event()

            async def work() -> None:
                started.set()
                await release.wait()

            supervisor.spawn(
                self.context,
                "state_commit",
                work(),
                drain_on_shutdown=True,
            )
            await started.wait()

            snapshot = supervisor.snapshot()
            self.assertEqual(len(snapshot["running"]), 1)
            entry = snapshot["running"][0]
            self.assertEqual(
                set(entry.keys()),
                {"character_id", "operation", "drain_on_shutdown", "started_at", "elapsed_ms"},
            )
            self.assertEqual(entry["character_id"], "role-a")
            self.assertEqual(entry["operation"], "state_commit")
            self.assertTrue(entry["drain_on_shutdown"])
            self.assertIsInstance(entry["started_at"], str)
            self.assertIsInstance(entry["elapsed_ms"], int)
            self.assertGreaterEqual(entry["elapsed_ms"], 0)
            self.assertEqual(snapshot["counts"]["running"], 1)

            release.set()
            await supervisor.wait_for_idle()

        asyncio.run(run())

    def test_snapshot_records_recent_success_and_failure_history(self) -> None:
        async def run() -> None:
            supervisor = BackgroundTaskSupervisor()

            async def ok_work() -> None:
                return None

            supervisor.spawn(
                self.context,
                "interaction_post_process",
                ok_work(),
                drain_on_shutdown=True,
            )
            supervisor.spawn(
                self.context,
                "failing_work",
                _raise_runtime_error(),
                drain_on_shutdown=True,
            )
            await supervisor.wait_for_idle()

            output = io.StringIO()
            with redirect_stdout(output):
                await supervisor.wait_for_idle()

            snapshot = supervisor.snapshot()
            self.assertEqual(snapshot["counts"]["succeeded"], 1)
            self.assertEqual(snapshot["counts"]["failed"], 1)
            self.assertEqual(snapshot["counts"]["running"], 0)
            self.assertEqual(len(snapshot["recent"]), 2)

            by_operation = {entry["operation"]: entry for entry in snapshot["recent"]}
            succeeded_entry = by_operation["interaction_post_process"]
            self.assertEqual(succeeded_entry["outcome"], "succeeded")
            self.assertEqual(succeeded_entry["error_summary"], "")

            failed_entry = by_operation["failing_work"]
            self.assertEqual(failed_entry["outcome"], "failed")
            self.assertIn("expected failure", failed_entry["error_summary"])
            self.assertEqual(
                set(failed_entry.keys()),
                {
                    "character_id",
                    "operation",
                    "outcome",
                    "error_summary",
                    "started_at",
                    "finished_at",
                    "elapsed_ms",
                },
            )

        asyncio.run(run())

    def test_snapshot_does_not_leak_task_or_work_objects(self) -> None:
        async def run() -> None:
            supervisor = BackgroundTaskSupervisor()
            release = asyncio.Event()

            async def work() -> None:
                await release.wait()

            supervisor.spawn(
                self.context,
                "delayed_send",
                work(),
                drain_on_shutdown=False,
            )
            await asyncio.sleep(0)

            snapshot = supervisor.snapshot()

            def _assert_no_task_or_awaitable(value: object) -> None:
                self.assertNotIsInstance(value, asyncio.Task)
                self.assertFalse(asyncio.iscoroutine(value))
                self.assertFalse(hasattr(value, "cr_frame"))
                if isinstance(value, dict):
                    for item in value.values():
                        _assert_no_task_or_awaitable(item)
                elif isinstance(value, list):
                    for item in value:
                        _assert_no_task_or_awaitable(item)

            _assert_no_task_or_awaitable(snapshot)

            release.set()
            await supervisor.wait_for_idle()

        asyncio.run(run())

    def test_recent_history_is_bounded_by_a_ring_buffer(self) -> None:
        async def run() -> None:
            supervisor = BackgroundTaskSupervisor()
            limit = supervisor._recent_history.maxlen
            assert limit is not None

            async def ok_work() -> None:
                return None

            total_spawned = limit + 5
            for index in range(total_spawned):
                supervisor.spawn(
                    self.context,
                    f"op-{index}",
                    ok_work(),
                    drain_on_shutdown=True,
                )
            await supervisor.wait_for_idle()

            snapshot = supervisor.snapshot()
            self.assertEqual(len(snapshot["recent"]), limit)
            self.assertEqual(snapshot["counts"]["succeeded"], total_spawned)
            newest_operations = {entry["operation"] for entry in snapshot["recent"]}
            self.assertIn(f"op-{total_spawned - 1}", newest_operations)
            self.assertNotIn("op-0", newest_operations)

        asyncio.run(run())

    def test_snapshot_never_raises_even_on_internal_error(self) -> None:
        supervisor = BackgroundTaskSupervisor()
        # 破坏内部状态，验证 snapshot() 吞掉异常而不向业务传播。
        supervisor._recent_history = None  # type: ignore[assignment]
        snapshot = supervisor.snapshot()
        self.assertEqual(snapshot["running"], [])
        self.assertEqual(snapshot["recent"], [])
        self.assertEqual(snapshot["counts"]["running"], 0)


async def _raise_runtime_error() -> None:
    raise RuntimeError("expected failure")


if __name__ == "__main__":
    unittest.main()
