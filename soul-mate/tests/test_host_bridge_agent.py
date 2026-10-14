from __future__ import annotations

import sys
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from host_bridge.wxautox_agent import (
    FavoriteStickerPanelDriver,
    PreSendFailure,
    UiaStickerCapture,
    WxautoxBridgeAgent,
)
from scripts.wxautox_bridge_agent import _safe_window_error


class FakeBridgeClient:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.contact_rows = [
            {"external_contact_id": "wxid-alice", "display_name": "Alice"},
        ]
        self.uploads: list[dict[str, Any]] = []
        self.inbounds: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.acks: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.candidates: list[list[str]] = []
        self.heartbeat_response: dict[str, Any] = {"status": "online"}

    def contacts(self) -> list[dict[str, str]]:
        return self.contact_rows

    def upload_attachment(self, **kwargs: Any) -> dict[str, Any]:
        self.uploads.append(kwargs)
        return {"attachment_ref": f"attachment-{len(self.uploads)}"}

    def inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.inbounds.append(payload)
        return {"action": "accepted"}

    def lease(self, **kwargs: Any) -> list[dict[str, Any]]:
        del kwargs
        actions, self.actions = self.actions, []
        return actions

    def ack(self, **kwargs: Any) -> dict[str, Any]:
        self.acks.append(kwargs)
        return {"status": "sent"}

    def fail(self, **kwargs: Any) -> dict[str, Any]:
        self.failures.append(kwargs)
        return {"status": "failed"}

    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.heartbeats.append(payload)
        return self.heartbeat_response

    def report_contact_candidates(self, *, agent_id: str, display_names: list[str]) -> int:
        del agent_id
        self.candidates.append(display_names)
        return len(display_names)

    def download_attachment(self, **kwargs: Any) -> Path:
        destination = Path(kwargs["destination_dir"])
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / f"{kwargs['attachment_ref']}.png"
        path.write_bytes(b"outbound-image")
        return path


class FakeChat:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.files: list[list[str]] = []
        self.fail_text = False

    def SendMsg(self, *, msg: str) -> bool:
        if self.fail_text:
            raise RuntimeError("模拟发送调用异常")
        self.texts.append(msg)
        return True

    def SendFiles(self, *, filepath: list[str]) -> bool:
        self.files.append(filepath)
        return True


class FakeWx:
    def __init__(self) -> None:
        self.callbacks: dict[str, Any] = {}
        self.chats: dict[str, FakeChat] = {}
        self.moments: list[dict[str, Any]] = []
        self.removed_listeners: list[str] = []
        self.sessions: list[Any] = []

    def AddListenChat(self, *, nickname: str, callback: Any) -> FakeChat:
        chat = FakeChat()
        self.callbacks[nickname] = callback
        self.chats[nickname] = chat
        return chat

    def IsOnline(self) -> bool:
        return True

    def RemoveListenChat(self, *, nickname: str) -> bool:
        self.removed_listeners.append(nickname)
        self.callbacks.pop(nickname, None)
        self.chats.pop(nickname, None)
        return True

    def PublishMoment(self, **kwargs: Any) -> bool:
        self.moments.append(kwargs)
        return True

    def GetSession(self) -> list[Any]:
        return self.sessions


class FakeMessage:
    def __init__(self, message_type: str, *, content: str = "") -> None:
        self.attr = "friend"
        self.type = message_type
        self.content = content
        self.hash = f"hash-{message_type}-{content}"
        self.id = f"ui-{message_type}"
        self.info = {"create_time": "2026-08-06T06:00:00+00:00"}
        self.download_called = False

    def download(self, *, dir_path: Path, original: bool) -> Path:
        self.download_called = True
        self.download_original = original
        dir_path.mkdir(parents=True, exist_ok=True)
        path = dir_path / "incoming.png"
        path.write_bytes(b"incoming-image")
        return path

    def to_text(self) -> str:
        return self.content


class SelfMessage(FakeMessage):
    direction = "self"


class FakeStickerCapture:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def capture(self, *, message: Any, destination_dir: Path) -> Path:
        del message
        self.calls += 1
        if self.fail:
            raise RuntimeError("选择器失效")
        destination_dir.mkdir(parents=True, exist_ok=True)
        path = destination_dir / f"sticker-{self.calls}.png"
        path.write_bytes(b"sticker")
        return path


class HostBridgeAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.root = Path(self.temporary_dir.name)
        self.client = FakeBridgeClient(self.root)
        self.wx = FakeWx()
        self.sleeps: list[float] = []
        self.sticker_capture = FakeStickerCapture()
        self.agent = WxautoxBridgeAgent(
            wx=self.wx,
            client=self.client,  # type: ignore[arg-type]
            agent_id="agent-test",
            capture_dir=self.root / "capture",
            sticker_capture=self.sticker_capture,  # type: ignore[arg-type]
            sleep=self.sleeps.append,
        )
        self.agent.sync_listeners()

    def test_callback_captures_text_image_and_sticker_before_async_upload(self) -> None:
        callback = self.wx.callbacks["Alice"]
        text_message = FakeMessage("text", content="你在干嘛")
        image_message = FakeMessage("image")
        sticker_message = FakeMessage("emotion")

        callback(text_message, self.wx.chats["Alice"])
        callback(image_message, self.wx.chats["Alice"])
        callback(sticker_message, self.wx.chats["Alice"])

        self.assertTrue(image_message.download_called)
        self.assertEqual(self.sticker_capture.calls, 1)
        self.assertEqual(len(list((self.root / "capture").glob("image-*.png"))), 1)
        self.assertEqual(self.client.inbounds, [])

        self.assertTrue(self.agent.handle_inbound_once())
        self.assertTrue(self.agent.handle_inbound_once())
        self.assertTrue(self.agent.handle_inbound_once())
        self.assertEqual([item["type"] for item in self.client.inbounds], ["text", "image", "sticker"])
        self.assertEqual(self.client.inbounds[0]["text"], "你在干嘛")
        self.assertEqual(len(self.client.uploads), 2)
        self.assertEqual(self.client.uploads[0]["kind"], "image")
        self.assertEqual(self.client.uploads[1]["kind"], "sticker")
        self.assertTrue(all(item["occurred_at"].endswith("+00:00") for item in self.client.inbounds))

    def test_voice_is_transcribed_immediately_and_keeps_voice_type(self) -> None:
        callback = self.wx.callbacks["Alice"]
        callback(FakeMessage("voice", content="今天有点累"), self.wx.chats["Alice"])

        self.assertTrue(self.agent.handle_inbound_once())
        inbound = self.client.inbounds[-1]
        self.assertEqual(inbound["type"], "voice")
        self.assertEqual(inbound["text"], "[对方发来语音，Plus 转写：今天有点累]")
        self.assertEqual(self.client.uploads, [])

    def test_listener_ignores_messages_marked_as_self(self) -> None:
        callback = self.wx.callbacks["Alice"]
        callback(SelfMessage("text", content="机器人刚刚发过的内容"), self.wx.chats["Alice"])
        self.assertFalse(self.agent.handle_inbound_once())
        self.assertEqual(self.client.inbounds, [])

    def test_outbox_allows_natural_longer_message_sequences(self) -> None:
        self.client.actions = [{
            "action_id": "action-eight-chunks",
            "external_contact_id": "wxid-alice",
            "type": "text",
            "lease_token": "lease-eight-token",
            "payload": {"chunks": [str(index) for index in range(8)]},
        }]
        self.assertEqual(self.agent.poll_outbox_once(), 1)
        self.assertEqual(self.wx.chats["Alice"].texts, [str(index) for index in range(8)])
        self.assertEqual(len(self.client.acks), 1)

    def test_validation_mode_is_persisted_with_captured_message(self) -> None:
        wx = FakeWx()
        agent = WxautoxBridgeAgent(
            wx=wx,
            client=self.client,  # type: ignore[arg-type]
            agent_id="agent-validation",
            capture_dir=self.root / "validation-capture",
            validation_mode=True,
        )
        agent.sync_listeners()
        wx.callbacks["Alice"](FakeMessage("text", content="这是一条验收消息"), wx.chats["Alice"])

        self.assertTrue(agent.handle_inbound_once())
        self.assertEqual(self.client.inbounds[-1]["context_mode"], "validation")

    def test_captured_message_survives_agent_restart_before_upload(self) -> None:
        callback = self.wx.callbacks["Alice"]
        callback(FakeMessage("text", content="重启后也不能丢"), self.wx.chats["Alice"])
        self.assertEqual(self.client.inbounds, [])

        restarted = WxautoxBridgeAgent(
            wx=self.wx,
            client=self.client,  # type: ignore[arg-type]
            agent_id="agent-test",
            capture_dir=self.root / "capture",
            sticker_capture=self.sticker_capture,  # type: ignore[arg-type]
        )
        self.assertTrue(restarted.handle_inbound_once())
        self.assertEqual(self.client.inbounds[-1]["text"], "重启后也不能丢")
        self.assertEqual(restarted._inbound_spool.counts(), {"pending": 0, "failed": 0})

    def test_outbox_text_and_image_are_acked_and_delayed_chunks_are_bounded(self) -> None:
        self.client.actions = [
            {
                "action_id": "action-text",
                "external_contact_id": "wxid-alice",
                "type": "text",
                "lease_token": "lease-text-token",
                "payload": {"chunks": ["第一句", "第二句"], "chunk_delays_seconds": [99]},
            },
            {
                "action_id": "action-image",
                "external_contact_id": "wxid-alice",
                "type": "image",
                "lease_token": "lease-image-token",
                "payload": {"attachment_ref": "attachment-reviewed"},
            },
        ]

        self.assertEqual(self.agent.poll_outbox_once(), 2)
        chat = self.wx.chats["Alice"]
        self.assertEqual(chat.texts, ["第一句", "第二句"])
        self.assertEqual(self.sleeps, [8.0])
        self.assertEqual(len(chat.files), 1)
        self.assertEqual([item["action_id"] for item in self.client.acks], ["action-text", "action-image"])
        self.assertEqual(self.client.failures, [])

    def test_send_exception_is_delivery_unknown_and_missing_chat_is_pre_send_retry(self) -> None:
        self.wx.chats["Alice"].fail_text = True
        self.client.actions = [
            {
                "action_id": "action-unknown",
                "external_contact_id": "wxid-alice",
                "type": "text",
                "lease_token": "lease-unknown-token",
                "payload": {"chunks": ["可能已经发出"]},
            },
            {
                "action_id": "action-no-chat",
                "external_contact_id": "wxid-missing",
                "type": "text",
                "lease_token": "lease-no-chat-token",
                "payload": {"chunks": ["发送前失败"]},
            },
        ]

        self.agent.poll_outbox_once()
        unknown, pre_send = self.client.failures
        self.assertTrue(unknown["delivery_unknown"])
        self.assertFalse(unknown["retryable"])
        self.assertFalse(pre_send["delivery_unknown"])
        self.assertTrue(pre_send["retryable"])
        self.assertEqual(self.client.acks, [])

    def test_sticker_selector_circuit_breaker_does_not_stop_text(self) -> None:
        failing_capture = FakeStickerCapture(fail=True)
        agent = WxautoxBridgeAgent(
            wx=self.wx,
            client=self.client,  # type: ignore[arg-type]
            agent_id="agent-test",
            capture_dir=self.root / "capture-failing",
            sticker_capture=failing_capture,  # type: ignore[arg-type]
        )
        for _ in range(4):
            agent.capture_callback(
                message=FakeMessage("emotion"),
                chat=self.wx.chats["Alice"],
                external_contact_id="wxid-alice",
            )
        agent.capture_callback(
            message=FakeMessage("text", content="文字仍然正常"),
            chat=self.wx.chats["Alice"],
            external_contact_id="wxid-alice",
        )
        self.assertEqual(failing_capture.calls, 3)
        self.assertFalse(agent.sticker_capture_ready)
        self.assertTrue(agent.handle_inbound_once())
        self.assertEqual(self.client.inbounds[-1]["text"], "文字仍然正常")

    def test_heartbeat_reports_only_current_capabilities(self) -> None:
        self.agent.heartbeat_once()
        heartbeat = self.client.heartbeats[-1]
        self.assertTrue(heartbeat["window_ready"])
        self.assertIn("sticker_capture", heartbeat["capabilities"])
        self.assertIn("contact_discovery", heartbeat["capabilities"])
        self.assertIn("voice_to_text", heartbeat["capabilities"])
        self.assertNotIn("sticker_send", heartbeat["capabilities"])

    def test_binding_mismatch_fails_closed_before_background_loops_start(self) -> None:
        self.client.heartbeat_response = {
            "status": "online",
            "binding_valid": False,
            "message": "绑定版本不一致",
        }
        isolated = WxautoxBridgeAgent(
            wx=FakeWx(),
            client=self.client,  # type: ignore[arg-type]
            agent_id="agent-invalid-binding",
            capture_dir=self.root / "invalid-binding",
            configuration_version="stale",
            binding_version=1,
            window_nickname="错误窗口",
            wechat_id="wrong",
        )
        with self.assertRaises(PreSendFailure):
            isolated.start()
        self.assertFalse(isolated._started)
        self.assertFalse(isolated._binding_valid)

    def test_contact_discovery_reports_names_only_and_skips_known_or_system_sessions(self) -> None:
        class Session:
            def __init__(self, name: str) -> None:
                self.name = name
                self.content = "不应上传的消息正文"

        self.wx.sessions = [Session("Alice"), Session("文件传输助手"), Session("Owner 新备注")]
        self.assertEqual(self.agent.discover_contact_candidates(), 1)
        self.assertEqual(self.client.candidates, [["Owner 新备注"]])
        self.assertNotIn("不应上传的消息正文", str(self.client.candidates))

    def test_listener_is_removed_and_recreated_when_display_name_changes(self) -> None:
        self.client.contact_rows = [
            {"external_contact_id": "wxid-alice", "display_name": "Alice 新备注"},
        ]
        self.assertEqual(self.agent.sync_listeners(), 1)
        self.assertEqual(self.wx.removed_listeners, ["Alice"])
        self.assertNotIn("Alice", self.wx.callbacks)
        self.assertIn("Alice 新备注", self.wx.callbacks)

        self.client.contact_rows = []
        self.assertEqual(self.agent.sync_listeners(), 0)
        self.assertEqual(self.wx.removed_listeners, ["Alice", "Alice 新备注"])


class UiaStickerCaptureTests(unittest.TestCase):
    def test_capture_uses_validated_message_bounding_rectangle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            calls: list[tuple[int, int, int, int]] = []

            class FakeImage:
                def save(self, path: Path, *, format: str) -> None:
                    self.path = path
                    self.format = format
                    path.write_bytes(b"png")

            class FakeControl:
                BoundingRectangle = (10, 20, 110, 120)

            class Message:
                control = FakeControl()

                @staticmethod
                def roll_into_view() -> None:
                    return None

            def grabber(*, bbox: tuple[int, int, int, int]) -> FakeImage:
                calls.append(bbox)
                return FakeImage()

            capture = UiaStickerCapture(grabber=grabber)
            path = capture.capture(message=Message(), destination_dir=Path(temporary_dir))
            self.assertEqual(calls, [(10, 20, 110, 120)])
            self.assertEqual(path.read_bytes(), b"png")


class FavoriteStickerPanelDriverTests(unittest.TestCase):
    def test_fingerprint_locates_cell_and_clicks_native_favorite(self) -> None:
        from PIL import Image, ImageDraw

        panel = Image.new("RGB", (200, 100), "white")
        draw = ImageDraw.Draw(panel)
        draw.rectangle((0, 0, 49, 99), fill="black")
        draw.rectangle((150, 0, 199, 99), fill="black")
        target = FavoriteStickerPanelDriver.perceptual_hash(panel.crop((100, 0, 200, 100)))
        clicks: list[tuple[int, int]] = []
        driver = FavoriteStickerPanelDriver(
            open_button_xy=(20, 30),
            panel_bbox=(100, 100, 300, 200),
            grid=(2, 1),
            maximum_hamming_distance=0,
            grabber=lambda **_: panel,
            clicker=lambda x, y: clicks.append((x, y)),
            sleep=lambda _: None,
        )
        driver.send(target_fingerprint=target)
        self.assertEqual(clicks, [(20, 30), (250, 150)])

    def test_unconfigured_driver_fails_before_send(self) -> None:
        driver = FavoriteStickerPanelDriver(
            open_button_xy=None,
            panel_bbox=None,
        )
        self.assertFalse(driver.ready)
        with self.assertRaisesRegex(RuntimeError, "未配置"):
            driver.send(target_fingerprint="0" * 16)


class MultiAccountHostConfigurationTests(unittest.TestCase):
    def test_window_preflight_error_keeps_only_a_short_useful_line(self) -> None:
        message = _safe_window_error(Exception("未找到已登录的客户端主窗口。\nactivation=do-not-copy"))

        self.assertEqual(message, "Exception: 未找到已登录的客户端主窗口。")

    def test_two_wechat_windows_map_to_two_independent_core_accounts(self) -> None:
        from scripts.wxautox_bridge_agent import _load_account_configs

        with patch.dict(os.environ, {
            "WECHAT_BRIDGE_AGENT_ID": "windows-test",
            "WECHAT_ACCOUNTS_JSON": (
                '[{"account_id":"du-jiaying","nickname":"微信一"},'
                '{"account_id":"ji-yu","nickname":"微信二"}]'
            ),
        }, clear=False):
            accounts = _load_account_configs()

        self.assertEqual([item.account_id for item in accounts], ["du-jiaying", "ji-yu"])
        self.assertEqual([item.nickname for item in accounts], ["微信一", "微信二"])
        self.assertEqual([item.agent_id for item in accounts], ["windows-test:du-jiaying", "windows-test:ji-yu"])

    def test_disaster_recovery_mapping_fails_closed_when_catalog_is_missing(self) -> None:
        from scripts.wxautox_bridge_agent import _load_account_configs

        with patch.dict(os.environ, {"WECHAT_ACCOUNTS_JSON": ""}, clear=False):
            with self.assertRaisesRegex(ValueError, "必须显式配置"):
                _load_account_configs()

    def test_multi_account_mode_rejects_missing_or_duplicate_window_nicknames(self) -> None:
        from scripts.wxautox_bridge_agent import _load_account_configs

        with patch.dict(os.environ, {
            "WECHAT_ACCOUNTS_JSON": (
                '[{"account_id":"du-jiaying","nickname":"同一个微信"},'
                '{"account_id":"ji-yu","nickname":"同一个微信"}]'
            ),
        }, clear=False):
            with self.assertRaisesRegex(ValueError, "昵称不能重复"):
                _load_account_configs()


if __name__ == "__main__":
    unittest.main()
