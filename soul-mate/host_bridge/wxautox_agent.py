"""wxautox4 Plus 监听、即时捕获与出站动作执行器。"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from host_bridge.client import BridgeHttpClient, BridgeHttpError
from host_bridge.inbound_spool import HostInboundSpool


LOGGER = logging.getLogger("soul_mate.host_bridge")


class PreSendFailure(RuntimeError):
    """确认尚未触发微信发送前发生的错误。"""


class DeliveryUnknown(RuntimeError):
    """已调用微信 UI 发送，结果无法确认，禁止自动重发。"""


class FavoriteStickerPanelDriver:
    """固定布局下的收藏表情网格驱动，完全独立于文字和图片发送。"""

    def __init__(
        self,
        *,
        open_button_xy: tuple[int, int] | None,
        panel_bbox: tuple[int, int, int, int] | None,
        grid: tuple[int, int] = (5, 4),
        maximum_hamming_distance: int = 6,
        grabber: Callable[..., Any] | None = None,
        clicker: Callable[[int, int], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.open_button_xy = open_button_xy
        self.panel_bbox = panel_bbox
        self.grid = grid
        self.maximum_hamming_distance = max(0, min(int(maximum_hamming_distance), 64))
        self._grabber = grabber
        self._clicker = clicker
        self._sleep = sleep

    @property
    def ready(self) -> bool:
        if self.open_button_xy is None or self.panel_bbox is None:
            return False
        columns, rows = self.grid
        return columns > 0 and rows > 0 and self.panel_bbox[2] > self.panel_bbox[0] and self.panel_bbox[3] > self.panel_bbox[1]

    @staticmethod
    def perceptual_hash(image: Any) -> str:
        grayscale = image.convert("L").resize((8, 8))
        pixels = list(grayscale.getdata())
        average = sum(int(pixel) for pixel in pixels) / len(pixels)
        bits = 0
        for pixel in pixels:
            bits = (bits << 1) | int(int(pixel) >= average)
        return f"{bits:016x}"

    @staticmethod
    def _hamming(left: str, right: str) -> int:
        try:
            return (int(left, 16) ^ int(right, 16)).bit_count()
        except ValueError:
            return 65

    @staticmethod
    def _default_click(x: int, y: int) -> None:
        import ctypes

        user32 = ctypes.windll.user32
        if not user32.SetCursorPos(int(x), int(y)):
            raise RuntimeError("无法移动鼠标到收藏表情面板")
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)

    def send(self, *, target_fingerprint: str) -> None:
        if not self.ready:
            raise PreSendFailure("微信收藏表情面板驱动未配置")
        normalized_target = str(target_fingerprint or "").strip().lower()
        if len(normalized_target) != 16 or any(char not in "0123456789abcdef" for char in normalized_target):
            raise PreSendFailure("收藏表情指纹无效")
        clicker = self._clicker or self._default_click
        if self._grabber is None:
            from PIL import ImageGrab

            grabber = ImageGrab.grab
        else:
            grabber = self._grabber
        clicker(*self.open_button_xy)
        self._sleep(0.6)
        panel = grabber(bbox=self.panel_bbox)
        columns, rows = self.grid
        width, height = panel.size
        candidates: list[tuple[int, int, int]] = []
        for row in range(rows):
            for column in range(columns):
                left = round(column * width / columns)
                top = round(row * height / rows)
                right = round((column + 1) * width / columns)
                bottom = round((row + 1) * height / rows)
                cell_hash = self.perceptual_hash(panel.crop((left, top, right, bottom)))
                candidates.append((self._hamming(cell_hash, normalized_target), column, row))
        distance, column, row = min(candidates, key=lambda item: item[0])
        if distance > self.maximum_hamming_distance:
            raise PreSendFailure("收藏表情面板中未找到匹配指纹")
        panel_left, panel_top, panel_right, panel_bottom = self.panel_bbox
        cell_x = panel_left + round((column + 0.5) * (panel_right - panel_left) / columns)
        cell_y = panel_top + round((row + 0.5) * (panel_bottom - panel_top) / rows)
        try:
            clicker(cell_x, cell_y)
        except Exception as exc:
            raise DeliveryUnknown("点击收藏表情后结果无法确认") from exc


@dataclass(frozen=True, slots=True)
class CapturedInbound:
    external_contact_id: str
    occurred_at: datetime
    message_type: str
    text: str | None
    file_path: Path | None
    source_fingerprint: str
    attempts: int = 0
    context_mode: str = "live"


def _rect_tuple(value: Any) -> tuple[int, int, int, int] | None:
    if callable(value):
        value = value()
    if isinstance(value, (tuple, list)) and len(value) == 4:
        left, top, right, bottom = value
    else:
        left = getattr(value, "left", None)
        top = getattr(value, "top", None)
        right = getattr(value, "right", None)
        bottom = getattr(value, "bottom", None)
    try:
        rect = (int(left), int(top), int(right), int(bottom))
    except (TypeError, ValueError):
        return None
    width = rect[2] - rect[0]
    height = rect[3] - rect[1]
    if width < 8 or height < 8 or width > 1600 or height > 1600:
        return None
    return rect


class UiaStickerCapture:
    """隔离的 UIA 截图能力；选择器失效时由上层熔断，不影响其他消息。"""

    def __init__(self, grabber: Callable[..., Any] | None = None) -> None:
        self._grabber = grabber

    def capture(self, *, message: Any, destination_dir: Path) -> Path:
        try:
            message.roll_into_view()
        except Exception:
            pass
        controls = []
        for name in ("control", "_control", "ui", "_ui", "element", "_element", "msg_control", "item"):
            candidate = getattr(message, name, None)
            if candidate is not None:
                controls.append(candidate)
        controls.append(message)
        rect = None
        for control in controls:
            for name in ("BoundingRectangle", "bounding_rectangle", "rectangle"):
                candidate = getattr(control, name, None)
                if candidate is None:
                    continue
                try:
                    rect = _rect_tuple(candidate)
                except Exception:
                    rect = None
                if rect is not None:
                    break
            if rect is not None:
                break
        if rect is None:
            raise RuntimeError("未找到表情消息的可截图边界")
        if self._grabber is None:
            from PIL import ImageGrab

            grabber = ImageGrab.grab
        else:
            grabber = self._grabber
        image = grabber(bbox=rect)
        destination_dir.mkdir(parents=True, exist_ok=True)
        path = destination_dir / f"sticker-{uuid.uuid4().hex}.png"
        image.save(path, format="PNG")
        return path


def _message_time(message: Any) -> datetime:
    info = getattr(message, "info", None)
    candidates: list[Any] = []
    if isinstance(info, dict):
        candidates.extend(info.get(key) for key in ("create_time", "timestamp", "time") if info.get(key))
    candidates.append(getattr(message, "time", None))
    for value in candidates:
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            except (ValueError, OSError):
                continue
        text = str(value or "").strip()
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _stable_source_fingerprint(message: Any, *, content_digest: str = "") -> str:
    message_hash = str(getattr(message, "hash", "") or "").strip()
    message_id = str(getattr(message, "id", "") or "").strip()
    info = getattr(message, "info", None)
    stable_info: dict[str, Any] = {}
    if isinstance(info, dict):
        for key in ("create_time", "timestamp", "time", "sender", "content", "type"):
            value = info.get(key)
            if isinstance(value, (str, int, float, bool)):
                stable_info[key] = value
    material = {
        "hash": message_hash,
        "id_fallback": "" if message_hash else message_id,
        "type": str(getattr(message, "type", "") or "").lower(),
        "sender": str(getattr(message, "sender", "") or ""),
        "content": str(getattr(message, "content", "") or ""),
        "info": stable_info,
        "content_digest": content_digest,
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _downloaded_path(result: Any) -> Path | None:
    if isinstance(result, Path):
        return result if result.is_file() else None
    if isinstance(result, str):
        path = Path(result)
        return path if path.is_file() else None
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, (str, Path)):
            path = Path(data)
            return path if path.is_file() else None
    return None


def _stabilize_capture(path: Path, destination_dir: Path) -> Path:
    """把 Plus 返回的临时下载结果立即移动到唯一文件，避免并发回调覆盖。"""
    destination_dir.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower() if path.suffix else ".dat"
    target = destination_dir / f"image-{uuid.uuid4().hex}{suffix}"
    if path.resolve() == target.resolve():
        return path
    path.replace(target)
    return target


class WxautoxBridgeAgent:
    def __init__(
        self,
        *,
        wx: Any,
        client: BridgeHttpClient,
        agent_id: str,
        capture_dir: str | Path,
        sticker_capture: UiaStickerCapture | None = None,
        sticker_sender: FavoriteStickerPanelDriver | None = None,
        sleep: Callable[[float], None] = time.sleep,
        bridge_version: str = "0.1.0",
        wechat_version: str = "",
        plus_active: bool = True,
        ui_lock: Any | None = None,
        inbound_spool: HostInboundSpool | None = None,
        configuration_version: str = "",
        binding_version: int = 0,
        window_nickname: str = "",
        wechat_id: str = "",
        validation_mode: bool = False,
    ) -> None:
        self.wx = wx
        self.client = client
        self.agent_id = str(agent_id).strip()
        self.capture_dir = Path(capture_dir)
        self.sticker_capture = sticker_capture
        self.sticker_sender = sticker_sender
        self.sleep = sleep
        self.bridge_version = bridge_version
        self.wechat_version = wechat_version
        self.plus_active = plus_active
        self._ui_lock = ui_lock or threading.RLock()
        self._inbound_spool = inbound_spool or HostInboundSpool(self.capture_dir / "inbound-spool.db")
        self.configuration_version = str(configuration_version or "")
        self.binding_version = max(0, int(binding_version))
        self.window_nickname = str(window_nickname or "")
        self.wechat_id = str(wechat_id or "")
        self.validation_mode = bool(validation_mode)
        self._binding_valid = True
        self._chats: dict[str, Any] = {}
        self._name_by_external: dict[str, str] = {}
        self._external_by_name: dict[str, str] = {}
        self._sticker_failures = 0
        # wxauto 部分版本会把自己刚发出的消息也回传给监听回调。
        # 记录短暂的出站回声，避免机器人把自己的消息再次当成入站消息。
        self._recent_outbound_text: dict[str, list[tuple[float, str]]] = {}
        self._stop = threading.Event()
        self._started = False

    @property
    def sticker_capture_ready(self) -> bool:
        return self.sticker_capture is not None and self._sticker_failures < 3

    def sync_listeners(self) -> int:
        with self._ui_lock:
            contacts = self.client.contacts()
            names = [item["display_name"] for item in contacts]
            if len(names) != len(set(names)):
                raise RuntimeError("白名单中存在重复微信备注名，无法建立唯一监听映射")
            active_external_ids = {item["external_contact_id"] for item in contacts}
            for external_id in list(self._chats):
                next_item = next(
                    (item for item in contacts if item["external_contact_id"] == external_id),
                    None,
                )
                next_name = str(next_item["display_name"]) if next_item else ""
                if external_id not in active_external_ids or self._name_by_external.get(external_id) != next_name:
                    previous_name = self._name_by_external.get(external_id, "")
                    if previous_name:
                        self.wx.RemoveListenChat(nickname=previous_name)
                    self._chats.pop(external_id, None)
                    self._name_by_external.pop(external_id, None)
            self._external_by_name = {
                item["display_name"]: item["external_contact_id"] for item in contacts
            }
            for item in contacts:
                external_id = item["external_contact_id"]
                if external_id in self._chats:
                    continue

                def callback(message: Any, chat: Any, current_external_id: str = external_id) -> None:
                    self.capture_callback(message=message, chat=chat, external_contact_id=current_external_id)

                chat = self.wx.AddListenChat(nickname=item["display_name"], callback=callback)
                if not chat:
                    raise RuntimeError("微信监听窗口创建失败")
                self._chats[external_id] = chat
                self._name_by_external[external_id] = item["display_name"]
            return len(self._chats)

    def discover_contact_candidates(self) -> int:
        """只上报会话名称供 Owner 确认，不读取未知联系人的消息内容。"""
        if not hasattr(self.wx, "GetSession"):
            return 0
        with self._ui_lock:
            known_names = {item["display_name"].casefold() for item in self.client.contacts()}
            sessions = self.wx.GetSession() or []
            names: list[str] = []
            ignored = {"文件传输助手", "微信团队", "服务通知", "订阅号消息"}
            for session in sessions[:100]:
                info = getattr(session, "info", {})
                fallback = info.get("name") if isinstance(info, dict) else ""
                name = str(getattr(session, "name", "") or fallback or "").strip()
                if not name or name in ignored or name.casefold() in known_names:
                    continue
                names.append(name)
        if not names:
            return 0
        return self.client.report_contact_candidates(
            agent_id=self.agent_id,
            display_names=list(dict.fromkeys(names)),
        )

    def _message_is_self(self, message: Any) -> bool:
        """兼容 wxauto 4.0/4.1 的自己消息标记。"""
        class_name = type(message).__name__.casefold()
        if "selfmessage" in class_name:
            return True
        for name in ("direction", "message_direction", "sender_type"):
            value = str(getattr(message, name, "") or "").strip().casefold()
            if value in {"self", "me", "mine", "outgoing", "out", "sent", "send", "right", "我"}:
                return True
        attr = str(getattr(message, "attr", "") or "").strip().casefold()
        if attr in {"self", "me", "mine", "outgoing", "out", "sent", "send", "我"}:
            return True
        sender = str(getattr(message, "sender", "") or "").strip().casefold()
        own_names = {self.window_nickname.casefold(), self.wechat_id.casefold()}
        own_names.discard("")
        return bool(sender and sender in own_names)

    def _remember_outbound_text(self, external_contact_id: str, text: str) -> None:
        contact_id = str(external_contact_id or "").strip()
        normalized = str(text or "").strip()
        if not contact_id or not normalized:
            return
        now = time.monotonic()
        with self._ui_lock:
            entries = [
                item for item in self._recent_outbound_text.get(contact_id, [])
                if now - item[0] <= 8.0
            ]
            entries.append((now, normalized))
            self._recent_outbound_text[contact_id] = entries[-16:]

    def _is_recent_outbound_echo(self, external_contact_id: str, text: str) -> bool:
        contact_id = str(external_contact_id or "").strip()
        normalized = str(text or "").strip()
        if not contact_id or not normalized:
            return False
        now = time.monotonic()
        with self._ui_lock:
            entries = [
                item for item in self._recent_outbound_text.get(contact_id, [])
                if now - item[0] <= 8.0
            ]
            for index, (_, value) in enumerate(entries):
                if value == normalized:
                    entries.pop(index)
                    self._recent_outbound_text[contact_id] = entries
                    return True
            self._recent_outbound_text[contact_id] = entries
        return False

    def capture_callback(self, *, message: Any, chat: Any, external_contact_id: str) -> None:
        del chat
        if not self._binding_valid:
            return
        if self._message_is_self(message):
            LOGGER.debug("忽略微信自己消息 external_contact_id=%s", external_contact_id[:8])
            return
        if str(getattr(message, "attr", "") or "").lower() != "friend":
            return
        message_type = str(getattr(message, "type", "") or "").lower()
        text: str | None = None
        file_path: Path | None = None
        normalized_type = message_type
        try:
            if message_type == "text":
                text = str(getattr(message, "content", "") or "").strip()
                if not text:
                    return
                if self._is_recent_outbound_echo(external_contact_id, text):
                    LOGGER.debug("忽略微信出站回声 external_contact_id=%s", external_contact_id[:8])
                    return
            elif message_type == "voice":
                transcribe = getattr(message, "to_text", None)
                if not callable(transcribe):
                    LOGGER.warning("当前 Plus 消息对象不支持语音转文字，已跳过一条语音")
                    return
                transcript = str(transcribe() or "").strip()
                if not transcript:
                    LOGGER.warning("Plus 未能识别这条语音，已跳过以免机器人猜测内容")
                    return
                normalized_type = "voice"
                text = f"[对方发来语音，Plus 转写：{transcript}]"
            elif message_type == "image":
                downloaded = _downloaded_path(
                    message.download(dir_path=self.capture_dir, original=False)
                )
                if downloaded is None:
                    raise RuntimeError("图片下载失败")
                file_path = _stabilize_capture(downloaded, self.capture_dir)
            elif message_type == "emotion":
                normalized_type = "sticker"
                if not self.sticker_capture_ready:
                    LOGGER.warning("自定义表情截图能力不可用，已跳过一条表情消息")
                    return
                try:
                    file_path = self.sticker_capture.capture(
                        message=message,
                        destination_dir=self.capture_dir,
                    )
                    self._sticker_failures = 0
                except Exception:
                    self._sticker_failures += 1
                    LOGGER.warning("自定义表情截图失败（%d/3）", self._sticker_failures)
                    return
            else:
                return
            content_digest = hashlib.sha256(file_path.read_bytes()).hexdigest() if file_path else ""
            fingerprint = _stable_source_fingerprint(message, content_digest=content_digest)
            captured = CapturedInbound(
                external_contact_id=external_contact_id,
                occurred_at=_message_time(message),
                message_type=normalized_type,
                text=text,
                file_path=file_path,
                source_fingerprint=fingerprint,
                context_mode="validation" if self.validation_mode else "live",
            )
            inserted = self._inbound_spool.put(captured)
            if not inserted and file_path is not None:
                file_path.unlink(missing_ok=True)
        except OverflowError:
            LOGGER.error("宿主持久入站队列已满，停止接收新消息以避免磁盘无界增长")
        except Exception:
            LOGGER.exception("微信回调内捕获失败")

    def handle_inbound_once(self) -> bool:
        if not self._binding_valid:
            return False
        item = self._inbound_spool.next(CapturedInbound)
        if item is None:
            return False
        assert isinstance(item, CapturedInbound)
        try:
            attachment_refs: list[str] = []
            if item.file_path is not None:
                uploaded = self.client.upload_attachment(
                    external_contact_id=item.external_contact_id,
                    kind=item.message_type,
                    file_path=item.file_path,
                )
                attachment_refs.append(str(uploaded["attachment_ref"]))
            event_seed = f"{item.external_contact_id}:{item.source_fingerprint}"
            event_digest = hashlib.sha256(event_seed.encode("utf-8")).hexdigest()
            self.client.inbound(
                {
                    "event_id": f"wx:{event_digest}",
                    "external_contact_id": item.external_contact_id,
                    "occurred_at": item.occurred_at.isoformat(),
                    "type": item.message_type,
                    "text": item.text,
                    "attachment_refs": attachment_refs,
                    "message_hash": event_digest,
                    "agent_id": self.agent_id,
                    "context_mode": item.context_mode,
                }
            )
            if item.file_path is not None:
                item.file_path.unlink(missing_ok=True)
            self._inbound_spool.complete(item.source_fingerprint)
            return True
        except (BridgeHttpError, KeyError, OSError) as exc:
            if not self._inbound_spool.fail(item.source_fingerprint, exc, maximum_attempts=4):
                LOGGER.error("一条入站消息连续上报失败，已停止自动重试")
            raise

    def _chat_for(self, external_contact_id: str) -> Any:
        chat = self._chats.get(external_contact_id)
        if chat is None:
            raise PreSendFailure("联系人监听窗口尚未就绪")
        return chat

    @staticmethod
    def _wx_send(call: Callable[..., Any], **kwargs: Any) -> None:
        try:
            result = call(**kwargs)
        except Exception as exc:
            raise DeliveryUnknown("微信发送调用异常") from exc
        if not result:
            raise DeliveryUnknown("微信发送结果无法确认")

    def _send_text(
        self,
        *,
        chat: Any,
        payload: dict[str, Any],
        external_contact_id: str = "",
    ) -> None:
        chunks = payload.get("chunks")
        if not isinstance(chunks, list):
            raise PreSendFailure("文字动作缺少 chunks")
        normalized = [str(chunk).strip() for chunk in chunks if str(chunk).strip()]
        if not normalized or len(normalized) > 8:
            raise PreSendFailure("文字动作分段数量无效")
        delays = payload.get("chunk_delays_seconds")
        delay_values = delays if isinstance(delays, list) else []
        for index, chunk in enumerate(normalized):
            if index:
                requested = delay_values[index - 1] if index - 1 < len(delay_values) else 0.8
                try:
                    delay = min(max(float(requested), 0.2), 8.0)
                except (TypeError, ValueError):
                    delay = 0.8
                self.sleep(delay)
            self._wx_send(chat.SendMsg, msg=chunk)
            self._remember_outbound_text(external_contact_id, chunk)

    def _download_payload_files(
        self,
        *,
        payload: dict[str, Any],
        external_contact_id: str,
    ) -> list[Path]:
        refs = payload.get("attachment_refs")
        if not isinstance(refs, list):
            single_ref = payload.get("attachment_ref")
            refs = [single_ref] if single_ref else []
        normalized_refs = [str(ref).strip() for ref in refs if str(ref).strip()]
        if not normalized_refs:
            raise PreSendFailure("媒体动作缺少附件引用")
        try:
            return [
                self.client.download_attachment(
                    attachment_ref=ref,
                    external_contact_id=external_contact_id,
                    destination_dir=self.capture_dir / "outbound",
                )
                for ref in normalized_refs
            ]
        except (BridgeHttpError, OSError) as exc:
            raise PreSendFailure("媒体附件尚未准备完成") from exc

    def _dispatch_action(self, action: dict[str, Any]) -> None:
        action_type = str(action.get("type") or "")
        external_contact_id = str(action.get("external_contact_id") or "")
        payload = action.get("payload")
        if not isinstance(payload, dict):
            raise PreSendFailure("出站动作载荷无效")
        if action_type in {"text", "emoji"}:
            self._send_text(
                chat=self._chat_for(external_contact_id),
                payload=payload,
                external_contact_id=external_contact_id,
            )
            return
        if action_type == "image":
            files = self._download_payload_files(
                payload=payload,
                external_contact_id=external_contact_id,
            )
            self._wx_send(
                self._chat_for(external_contact_id).SendFiles,
                filepath=[str(path.resolve()) for path in files],
            )
            return
        if action_type == "sticker":
            if self.sticker_sender is None:
                raise PreSendFailure("微信收藏表情面板驱动尚未启用")
            chat = self._chat_for(external_contact_id)
            show = getattr(chat, "Show", None)
            if callable(show):
                show()
            self.sticker_sender.send(target_fingerprint=str(payload.get("favorite_fingerprint") or ""))
            return
        if action_type == "moment":
            tags = payload.get("privacy_tags")
            if not isinstance(tags, list) or not [str(tag).strip() for tag in tags if str(tag).strip()]:
                raise PreSendFailure("朋友圈动作缺少白名单标签")
            files = self._download_payload_files(
                payload=payload,
                external_contact_id=external_contact_id,
            ) if payload.get("attachment_refs") else []
            self._wx_send(
                self.wx.PublishMoment,
                text=str(payload.get("text") or ""),
                media_files=[str(path.resolve()) for path in files],
                privacy_config={"privacy": "白名单", "tags": tags},
            )
            return
        raise PreSendFailure("不支持的出站动作类型")

    def poll_outbox_once(self) -> int:
        if not self._binding_valid:
            return 0
        actions = self.client.lease(agent_id=self.agent_id, limit=10, lease_seconds=120)
        for action in actions:
            action_id = str(action.get("action_id") or "")
            lease_token = str(action.get("lease_token") or "")
            try:
                with self._ui_lock:
                    self._dispatch_action(action)
            except PreSendFailure as exc:
                self.client.fail(
                    action_id=action_id,
                    agent_id=self.agent_id,
                    lease_token=lease_token,
                    reason=str(exc),
                    retryable=True,
                    delivery_unknown=False,
                )
            except DeliveryUnknown as exc:
                self.client.fail(
                    action_id=action_id,
                    agent_id=self.agent_id,
                    lease_token=lease_token,
                    reason=str(exc),
                    retryable=False,
                    delivery_unknown=True,
                )
            except Exception:
                LOGGER.exception("出站动作执行出现未分类异常")
                self.client.fail(
                    action_id=action_id,
                    agent_id=self.agent_id,
                    lease_token=lease_token,
                    reason="出站动作执行出现未分类异常",
                    retryable=False,
                    delivery_unknown=True,
                )
            else:
                self.client.ack(
                    action_id=action_id,
                    agent_id=self.agent_id,
                    lease_token=lease_token,
                )
        return len(actions)

    def heartbeat_once(self) -> None:
        try:
            with self._ui_lock:
                online = bool(self.wx.IsOnline())
        except Exception:
            online = False
        capabilities = ["text", "image", "voice_to_text", "unicode_emoji", "moment", "contact_discovery"]
        if self.sticker_capture_ready:
            capabilities.append("sticker_capture")
        if self.sticker_sender is not None and self.sticker_sender.ready:
            capabilities.append("sticker_send")
        response = self.client.heartbeat(
            {
                "agent_id": self.agent_id,
                "bridge_version": self.bridge_version,
                "wechat_version": self.wechat_version,
                "plus_active": self.plus_active,
                "window_ready": online and bool(self._chats),
                "capabilities": capabilities,
                "configuration_version": self.configuration_version,
                "binding_version": self.binding_version,
                "window_nickname": self.window_nickname,
                "wechat_id": self.wechat_id,
            }
        )
        self._binding_valid = bool(response.get("binding_valid", True))
        if not self._binding_valid:
            LOGGER.error("核心拒绝当前微信绑定：%s", str(response.get("message") or "绑定版本不一致"))

    def _loop(self, operation: Callable[[], Any], interval: float) -> None:
        while not self._stop.is_set():
            try:
                operation()
            except Exception:
                LOGGER.exception("桥接后台循环执行失败")
            self._stop.wait(interval)

    def start(self) -> None:
        if self._started:
            return
        self.heartbeat_once()
        if not self._binding_valid:
            raise PreSendFailure("核心微信绑定校验失败，已禁止监听和发送")
        self.sync_listeners()
        try:
            self.discover_contact_candidates()
        except Exception:
            LOGGER.warning("首次发现微信会话失败，文字桥接仍会启动并在后台重试", exc_info=True)
        threads = [
            threading.Thread(target=self._loop, args=(self.handle_inbound_once, 0.2), daemon=True),
            threading.Thread(target=self._loop, args=(self.poll_outbox_once, 1.0), daemon=True),
            threading.Thread(target=self._loop, args=(self.heartbeat_once, 10.0), daemon=True),
            threading.Thread(target=self._loop, args=(self.sync_listeners, 60.0), daemon=True),
            threading.Thread(target=self._loop, args=(self.discover_contact_candidates, 60.0), daemon=True),
        ]
        for thread in threads:
            thread.start()
        self._started = True

    def run(self) -> None:
        self.start()
        self.wx.KeepRunning()

    def stop(self) -> None:
        self._stop.set()
        with self._ui_lock:
            for name in list(self._name_by_external.values()):
                try:
                    self.wx.RemoveListenChat(nickname=name)
                except Exception:
                    LOGGER.warning("停止桥接时移除监听失败")
            self._chats.clear()
            self._name_by_external.clear()
            self._external_by_name.clear()
