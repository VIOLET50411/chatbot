from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.run_ledger import (
    RuntimeJobRunStateConflict,
    complete_job_run,
    count_job_runs,
    get_job_run,
    list_job_runs,
    skip_job_run,
    start_job_run,
    summarize_job_runs,
)


NOW = datetime(2026, 7, 28, 12, 0, 0)


class RuntimeLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_dir.name)
        self._environment = patch.dict(
            os.environ,
            {"SOUL_MATE_ROOT": str(self.root)},
            clear=False,
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)
        self.addCleanup(self._temporary_dir.cleanup)

    def test_start_and_complete_succeeded_run(self) -> None:
        run = start_job_run(
            "nightly_consolidation",
            character_id="role-a",
            now=NOW,
            details={"batch": 1},
        )
        self.assertEqual(run.status, "running")
        self.assertEqual(run.job_name, "nightly_consolidation")
        self.assertEqual(run.character_id, "role-a")
        self.assertIsNone(run.finished_at)
        self.assertIsNone(run.duration_ms)

        finished = complete_job_run(
            run.id,
            status="succeeded",
            now=NOW + timedelta(seconds=2),
            details={"processed": 5},
        )
        self.assertEqual(finished.status, "succeeded")
        self.assertEqual(finished.duration_ms, 2000)
        self.assertEqual(finished.error_summary, "")
        self.assertEqual(finished.details, {"batch": 1, "processed": 5})

        loaded = get_job_run(run.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.status, "succeeded")

    def test_complete_failed_run_records_error_summary(self) -> None:
        run = start_job_run("proactive_check", character_id="role-b", now=NOW)
        finished = complete_job_run(
            run.id,
            status="failed",
            now=NOW + timedelta(milliseconds=750),
            error=RuntimeError("send_failed: connection reset"),
        )
        self.assertEqual(finished.status, "failed")
        self.assertEqual(finished.duration_ms, 750)
        self.assertIn("send_failed", finished.error_summary)

    def test_complete_job_run_rejects_double_completion(self) -> None:
        run = start_job_run("agenda_generation", character_id="role-a", now=NOW)
        complete_job_run(run.id, status="succeeded", now=NOW + timedelta(seconds=1))
        with self.assertRaises(RuntimeJobRunStateConflict):
            complete_job_run(run.id, status="succeeded", now=NOW + timedelta(seconds=2))

    def test_complete_job_run_missing_id_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            complete_job_run("does-not-exist", status="succeeded", now=NOW)

    def test_skip_job_run_records_terminal_state_without_duration(self) -> None:
        run = skip_job_run(
            "proactive_check",
            character_id="role-c",
            reason="daily_success_limit_reached",
            now=NOW,
        )
        self.assertEqual(run.status, "skipped")
        self.assertEqual(run.error_summary, "daily_success_limit_reached")
        self.assertEqual(run.duration_ms, 0)
        self.assertEqual(run.finished_at, run.started_at)

    def test_list_job_runs_paginates_and_orders_by_started_at_desc(self) -> None:
        for index in range(5):
            start_job_run(
                "ticker",
                character_id="role-a",
                now=NOW + timedelta(seconds=index),
            )

        first_page = list_job_runs(job_name="ticker", limit=2, offset=0)
        second_page = list_job_runs(job_name="ticker", limit=2, offset=2)

        self.assertEqual(len(first_page), 2)
        self.assertEqual(len(second_page), 2)
        self.assertGreater(first_page[0].started_at, first_page[1].started_at)
        self.assertGreater(first_page[-1].started_at, second_page[0].started_at)

        total = count_job_runs(job_name="ticker")
        self.assertEqual(total, 5)

    def test_list_job_runs_filters_by_character_and_status(self) -> None:
        run_a = start_job_run("nightly_consolidation", character_id="role-a", now=NOW)
        complete_job_run(run_a.id, status="succeeded", now=NOW + timedelta(seconds=1))
        run_b = start_job_run(
            "nightly_consolidation", character_id="role-b", now=NOW + timedelta(seconds=2)
        )
        complete_job_run(
            run_b.id, status="failed", now=NOW + timedelta(seconds=3), error="boom"
        )

        role_a_runs = list_job_runs(character_id="role-a")
        self.assertEqual([run.character_id for run in role_a_runs], ["role-a"])

        failed_runs = list_job_runs(status="failed")
        self.assertEqual([run.id for run in failed_runs], [run_b.id])

        role_b_runs = list_job_runs(character_id="role-b", status="succeeded")
        self.assertEqual(role_b_runs, [])

    def test_summarize_job_runs_counts_by_status(self) -> None:
        succeeded = start_job_run("ticker", character_id="role-a", now=NOW)
        complete_job_run(succeeded.id, status="succeeded", now=NOW + timedelta(seconds=1))
        failed = start_job_run("ticker", character_id="role-a", now=NOW + timedelta(seconds=2))
        complete_job_run(
            failed.id, status="failed", now=NOW + timedelta(seconds=3), error="boom"
        )
        skip_job_run(
            "ticker", character_id="role-a", reason="cooldown_active", now=NOW + timedelta(seconds=4)
        )
        start_job_run("ticker", character_id="role-a", now=NOW + timedelta(seconds=5))

        summary = summarize_job_runs(job_name="ticker")
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["running"], 1)

    def test_invalid_job_name_and_status_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            start_job_run("   ", character_id="role-a", now=NOW)
        with self.assertRaises(ValueError):
            list_job_runs(status="not-a-real-status")

    def test_database_path_is_confined_to_project_root(self) -> None:
        expected_db = self.root / "data" / "platform" / "runtime_ledger.db"
        start_job_run("ticker", character_id="role-a", now=NOW)
        self.assertTrue(expected_db.is_file())


if __name__ == "__main__":
    unittest.main()
