from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.device_context import DeviceContextService


class DeviceContextServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = DeviceContextService(Path(self.temporary.name) / "devices.db")

    def test_token_is_returned_once_and_only_hash_is_persisted(self) -> None:
        issued = self.service.create_device(display_name="iPhone 17")
        self.assertGreaterEqual(len(issued.token), 24)
        raw = self.service.database_path.read_bytes()
        self.assertNotIn(issued.token.encode("utf-8"), raw)
        self.assertNotIn("device_token", str(self.service.list_devices()))

    def test_ingest_keeps_trend_deduplicates_and_drops_unapproved_fields(self) -> None:
        issued = self.service.create_device(
            display_name="iPhone 17",
            consent={"city": False, "health_heart": False},
        )
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "battery": 63,
            "charging": True,
            "app_name": "小红书",
            "city": "重庆",
            "latitude": 29.5,
            "health": {"steps": 4321, "heart_rate_avg": 72},
        }
        first = self.service.ingest(token=issued.token, payload=payload)
        second = self.service.ingest(token=issued.token, payload=payload)
        status = self.service.status()

        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(status["record_count"], 1)
        saved = status["records"][0]["data"]
        self.assertEqual(saved["app_name"], "小红书")
        self.assertEqual(saved["steps"], 4321)
        self.assertNotIn("city", saved)
        self.assertNotIn("heart_rate_avg", saved)
        self.assertNotIn("latitude", saved)

    def test_prompt_context_is_observation_not_medical_conclusion(self) -> None:
        issued = self.service.create_device(display_name="iPhone 17")
        self.service.ingest(
            token=issued.token,
            payload={
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "battery": 18,
                "charging": False,
                "app_name": "微信",
                "sleep_minutes": 360,
            },
        )
        context = self.service.prompt_context()
        self.assertIn("电量 18%", context)
        self.assertIn("最近打开 微信", context)
        self.assertIn("不要诊断疾病", context)

    def test_disabled_device_rejects_future_pushes(self) -> None:
        issued = self.service.create_device(display_name="iPhone 17")
        self.service.update_device(issued.device["id"], enabled=False)
        with self.assertRaises(PermissionError):
            self.service.ingest(
                token=issued.token,
                payload={"timestamp": datetime.now(timezone.utc).isoformat(), "battery": 50},
            )

    def test_owner_confirmed_birth_date_is_available_without_device_data(self) -> None:
        self.service.set_owner_fact(fact_key="birth_date", fact_value="2005-10-12")
        self.assertEqual(self.service.owner_profile()["birth_date"], "2005-10-12")
        context = self.service.prompt_context()
        self.assertIn("Owner 已亲自确认", context)
        self.assertIn("2005-10-12", context)
        self.assertNotIn("设备状态", context)


if __name__ == "__main__":
    unittest.main()
