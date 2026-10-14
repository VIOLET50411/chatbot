from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.logging_config import RedactingFilter, configure_noise_loggers, redact_log_text


class LoggingConfigTests(unittest.TestCase):
    def test_secret_and_contact_markers_are_redacted(self) -> None:
        value = redact_log_text("token=abc123 sk-secretvalue wxid_contact123")
        self.assertNotIn("abc123", value)
        self.assertNotIn("sk-secretvalue", value)
        self.assertNotIn("wxid_contact123", value)
        self.assertIn("[REDACTED]", value)

    def test_filter_redacts_format_arguments_before_rendering(self) -> None:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="token=%s",
            args=("very-secret",),
            exc_info=None,
        )
        self.assertTrue(RedactingFilter().filter(record))
        self.assertNotIn("very-secret", record.getMessage())

    def test_scheduler_executor_success_noise_is_warning_by_default(self) -> None:
        logger = logging.getLogger("apscheduler.executors.default")
        previous_level = logger.level
        self.addCleanup(logger.setLevel, previous_level)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APSCHEDULER_EXECUTOR_LOG_LEVEL", None)
            configure_noise_loggers()
        self.assertEqual(logger.level, logging.WARNING)

    def test_scheduler_executor_level_can_be_overridden(self) -> None:
        logger = logging.getLogger("apscheduler.executors.default")
        previous_level = logger.level
        self.addCleanup(logger.setLevel, previous_level)
        with patch.dict(os.environ, {"APSCHEDULER_EXECUTOR_LOG_LEVEL": "ERROR"}, clear=False):
            configure_noise_loggers()
        self.assertEqual(logger.level, logging.ERROR)
