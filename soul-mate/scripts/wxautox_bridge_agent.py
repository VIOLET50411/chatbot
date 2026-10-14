"""启动 Windows wxautox4 Plus 薄桥接代理。"""
from __future__ import annotations

import logging
import argparse
import json
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from host_bridge.client import BridgeHttpClient, BridgeHttpError
from host_bridge.wxautox_agent import FavoriteStickerPanelDriver, UiaStickerCapture, WxautoxBridgeAgent


_ACCOUNT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class HostAccountConfig:
    account_id: str
    nickname: str
    agent_id: str
    character_id: str = ""
    wechat_id: str = ""
    owner_remark: str = ""
    binding_version: int = 1
    configuration_version: str = ""


def _load_account_configs() -> list[HostAccountConfig]:
    base_agent_id = os.getenv("WECHAT_BRIDGE_AGENT_ID", f"windows-{socket.gethostname()}").strip()
    raw = os.getenv("WECHAT_ACCOUNTS_JSON", "").strip()
    records: list[dict[str, object]] = []
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("WECHAT_ACCOUNTS_JSON 不是有效 JSON") from exc
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("WECHAT_ACCOUNTS_JSON 必须是非空数组")
        records = [item for item in parsed if isinstance(item, dict)]
        if len(records) != len(parsed):
            raise ValueError("WECHAT_ACCOUNTS_JSON 每一项都必须是对象")
    else:
        raise ValueError("灾难恢复模式必须显式配置 WECHAT_ACCOUNTS_JSON")

    result: list[HostAccountConfig] = []
    for record in records:
        account_id = str(record.get("account_id") or "").strip().lower()
        nickname = str(record.get("nickname") or "").strip()
        if not _ACCOUNT_ID.fullmatch(account_id):
            raise ValueError("微信账号 ID 格式无效")
        result.append(HostAccountConfig(
            account_id=account_id,
            nickname=nickname,
            agent_id=f"{base_agent_id}:{account_id}",
        ))
    if len({item.account_id for item in result}) != len(result):
        raise ValueError("微信账号 ID 不能重复")
    if len(result) > 1 and any(not item.nickname for item in result):
        raise ValueError("多微信模式必须为每个账号填写微信昵称")
    if len({item.nickname for item in result if item.nickname}) != len([item for item in result if item.nickname]):
        raise ValueError("多微信模式的微信昵称不能重复")
    return result


def _load_account_configs_from_payload(payload: dict[str, object]) -> list[HostAccountConfig]:
    base_agent_id = os.getenv("WECHAT_BRIDGE_AGENT_ID", f"windows-{socket.gethostname()}").strip()
    configuration_version = str(payload.get("configuration_version") or "").strip()
    records = payload.get("accounts")
    if not configuration_version or not isinstance(records, list) or not records:
        raise ValueError("核心没有返回已批准的微信账号绑定")
    result: list[HostAccountConfig] = []
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("核心微信账号目录格式无效")
        account_id = str(item.get("account_id") or "").strip().lower()
        character_id = str(item.get("character_id") or "").strip().lower()
        nickname = str(item.get("window_nickname") or "").strip()
        wechat_id = str(item.get("wechat_id") or "").strip()
        owner_remark = str(item.get("owner_remark") or "").strip()
        try:
            binding_version = int(item.get("binding_version") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("微信绑定版本无效") from exc
        if not _ACCOUNT_ID.fullmatch(account_id) or not _ACCOUNT_ID.fullmatch(character_id):
            raise ValueError("核心微信账号标识格式无效")
        if not nickname or not wechat_id or not owner_remark or binding_version < 1:
            raise ValueError("核心微信账号绑定信息不完整")
        result.append(
            HostAccountConfig(
                account_id=account_id,
                nickname=nickname,
                agent_id=f"{base_agent_id}:{account_id}",
                character_id=character_id,
                wechat_id=wechat_id,
                owner_remark=owner_remark,
                binding_version=binding_version,
                configuration_version=configuration_version,
            )
        )
    if len({item.account_id for item in result}) != len(result):
        raise ValueError("核心微信账号 ID 不能重复")
    if len({item.character_id for item in result}) != len(result):
        raise ValueError("一个人物不能重复绑定多个微信窗口")
    if len({item.nickname.casefold() for item in result}) != len(result):
        raise ValueError("核心微信窗口昵称不能重复")
    if len({item.wechat_id.casefold() for item in result}) != len(result):
        raise ValueError("核心微信号不能重复")
    return result


def _coordinate_pair(value: str) -> tuple[int, int] | None:
    try:
        parts = tuple(int(item.strip()) for item in value.split(","))
    except ValueError:
        return None
    return parts if len(parts) == 2 else None


def _coordinate_box(value: str) -> tuple[int, int, int, int] | None:
    try:
        parts = tuple(int(item.strip()) for item in value.split(","))
    except ValueError:
        return None
    return parts if len(parts) == 4 else None


def _configure_logging(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = RotatingFileHandler(
        state_dir / "wxautox-bridge.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler], force=True)


def _safe_window_error(exc: Exception) -> str:
    """Return one useful wxautox window error line without leaking configuration."""
    first_line = next((line.strip() for line in str(exc).splitlines() if line.strip()), "")
    if not first_line:
        return type(exc).__name__
    return f"{type(exc).__name__}: {first_line[:160]}"


def _detect_windows_wechat_environment() -> dict[str, Any]:
    """只读枚举微信顶层窗口和客户端版本，不接触聊天内容。"""
    result: dict[str, Any] = {
        "windows_found": 0,
        "minimized_windows": 0,
        "client_versions": [],
    }
    if os.name != "nt":
        return result
    try:
        import psutil
        import win32api
        import win32gui
        import win32process
    except ImportError:
        return result

    executable_paths: set[str] = set()

    def collect(hwnd: int, _: object) -> bool:
        try:
            _, process_id = win32process.GetWindowThreadProcessId(hwnd)
            process = psutil.Process(process_id)
            if process.name().casefold() not in {"weixin.exe", "wechat.exe"}:
                return True
            if win32gui.GetClassName(hwnd) != "Qt51514QWindowIcon":
                return True
            if not win32gui.IsWindowVisible(hwnd):
                return True
            result["windows_found"] += 1
            if win32gui.IsIconic(hwnd):
                result["minimized_windows"] += 1
            executable_paths.add(process.exe())
        except (OSError, psutil.Error):
            return True
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except OSError:
        return result

    versions: set[str] = set()
    for executable_path in executable_paths:
        try:
            info = win32api.GetFileVersionInfo(executable_path, "\\")
            version_tuple = (
                info["FileVersionMS"] >> 16,
                info["FileVersionMS"] & 0xFFFF,
                info["FileVersionLS"] >> 16,
                info["FileVersionLS"] & 0xFFFF,
            )
            versions.add(".".join(str(part) for part in version_tuple))
        except (OSError, TypeError, KeyError):
            continue
    result["client_versions"] = sorted(versions)
    return result


def _window_failure_hint(environment: dict[str, Any], package_version: str) -> str:
    windows_found = int(environment.get("windows_found") or 0)
    minimized = int(environment.get("minimized_windows") or 0)
    versions = ",".join(str(item) for item in environment.get("client_versions") or []) or "unknown"
    if windows_found < 1:
        return "系统没有枚举到微信主窗口，请确认微信已登录并展开"
    if minimized >= windows_found:
        return f"检测到 {windows_found} 个微信主窗口但都已最小化，请恢复窗口后重试"
    parsed_versions: list[tuple[int, int, int]] = []
    for raw_version in environment.get("client_versions") or []:
        try:
            parts = tuple(int(part) for part in str(raw_version).split(".")[:3])
        except ValueError:
            continue
        if len(parts) == 3:
            parsed_versions.append(parts)
    if parsed_versions and all(version <= (4, 1, 9) for version in parsed_versions):
        return (
            f"检测到 {windows_found} 个微信主窗口，客户端版本 {versions}，wxautox4 {package_version}，"
            "客户端处于官方文档支持范围，但 Plus 仍无法附着 UI；请确认在运行桥接的同一个管理员 "
            "PowerShell 中完成授权，并恢复准确昵称的微信主窗口后重试"
        )
    return (
        f"检测到 {windows_found} 个微信主窗口，客户端版本 {versions}，wxautox4 {package_version}，"
        "但 Plus 无法附着当前 UI；官方更新日志目前只明确兼容到微信 4.1.9.x，"
        "请升级 Plus 到支持当前微信的版本，或将微信固定到兼容的 4.1.9.x"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Soul Mate wxautox4 host bridge")
    parser.add_argument("--check", action="store_true", help="validate approved window bindings without listening or sending")
    parser.add_argument("--account-id", default="", help="run one approved account only for a staged canary")
    parser.add_argument(
        "--validation-mode",
        action="store_true",
        help="reply normally but exclude captured turns from conversation, memory and relationship state",
    )
    arguments = parser.parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")
    state_dir = Path(os.getenv("WECHAT_BRIDGE_STATE_DIR", str(PROJECT_ROOT / "host-data"))).resolve()
    _configure_logging(state_dir / "logs")
    token = os.getenv("WECHAT_BRIDGE_TOKEN", "").strip()
    base_url = os.getenv("WECHAT_BRIDGE_BASE_URL", "http://127.0.0.1:8080").strip()
    allow_remote = os.getenv("WECHAT_BRIDGE_ALLOW_REMOTE", "false").strip().lower() == "true"
    try:
        from wxautox4 import WeChat
        from wxautox4.param import WxParam
    except ImportError:
        logging.error("未安装 wxautox4，请先安装 requirements-host.txt 并完成 Plus 激活")
        return 2
    # wxautox4 默认会把 DEBUG 级收发正文写进工作目录。桥接自身已经提供
    # 不记录聊天正文的滚动日志，因此必须关闭第三方正文文件日志，并在其
    # import 重设根 logger 后恢复我们的 UTF-8 日志配置。
    WxParam.ENABLE_FILE_LOGGER = False
    _configure_logging(state_dir / "logs")
    try:
        package_version = version("wxautox4")
    except PackageNotFoundError:
        package_version = "unknown"
    wechat_environment = _detect_windows_wechat_environment()
    try:
        discovery_client = BridgeHttpClient(
            base_url=base_url,
            token=token,
            allow_remote=allow_remote,
            account_id="du-jiaying",
        )
    except ValueError as exc:
        logging.error("微信桥接基础配置无效：%s", exc)
        return 2
    try:
        accounts = _load_account_configs_from_payload(discovery_client.accounts())
        logging.info("已从核心读取微信账号目录 version=%s", accounts[0].configuration_version)
    except (ValueError, BridgeHttpError) as exc:
        if os.getenv("WECHAT_ACCOUNT_CONFIG_FALLBACK", "false").strip().lower() != "true":
            logging.error("无法读取核心微信账号目录，已失败关闭：%s", exc)
            return 2
        logging.warning("核心账号目录不可用，启用灾难恢复静态映射：%s", exc)
        try:
            accounts = _load_account_configs()
        except ValueError as fallback_exc:
            logging.error("灾难恢复微信账号配置无效：%s", fallback_exc)
            return 2
    finally:
        discovery_client.close()
    requested_account = str(arguments.account_id or "").strip().lower()
    if requested_account:
        if not _ACCOUNT_ID.fullmatch(requested_account):
            logging.error("指定的微信账号 ID 格式无效")
            return 2
        accounts = [item for item in accounts if item.account_id == requested_account]
        if not accounts:
            logging.error("核心账号目录中没有已启用的账号：%s", requested_account)
            return 2
    if arguments.check:
        checks: list[dict[str, object]] = []
        for account in accounts:
            try:
                wx = WeChat(nickname=account.nickname)
                online = bool(wx.IsOnline())
                checks.append(
                    {
                        "account_id": account.account_id,
                        "character_id": account.character_id,
                        "window_nickname": account.nickname,
                        "wechat_id": account.wechat_id,
                        "window_ready": online,
                        "error": "" if online else "window_offline",
                    }
                )
            except Exception as exc:
                checks.append(
                    {
                        "account_id": account.account_id,
                        "character_id": account.character_id,
                        "window_nickname": account.nickname,
                        "wechat_id": account.wechat_id,
                        "window_ready": False,
                        "error": _safe_window_error(exc),
                        "diagnosis": _window_failure_hint(wechat_environment, package_version),
                    }
                )
        print(json.dumps({
            "operation": "check",
            "environment": {
                **wechat_environment,
                "wxautox4_version": package_version,
            },
            "accounts": checks,
        }, ensure_ascii=False))
        return 0 if checks and all(bool(item["window_ready"]) for item in checks) else 3
    sticker_enabled = os.getenv("WECHAT_STICKER_CAPTURE_ENABLED", "true").strip().lower() == "true"
    ui_lock = threading.RLock()
    clients: list[BridgeHttpClient] = []
    agents: list[WxautoxBridgeAgent] = []
    try:
        for account in accounts:
            client = BridgeHttpClient(
                base_url=base_url,
                token=token,
                allow_remote=allow_remote,
                account_id=account.account_id,
            )
            try:
                sticker_sender = FavoriteStickerPanelDriver(
                    open_button_xy=_coordinate_pair(os.getenv("WECHAT_FAVORITES_BUTTON_XY", "")),
                    panel_bbox=_coordinate_box(os.getenv("WECHAT_FAVORITES_PANEL_BBOX", "")),
                    grid=_coordinate_pair(os.getenv("WECHAT_FAVORITES_GRID", "5,4")) or (5, 4),
                    maximum_hamming_distance=int(os.getenv("WECHAT_FAVORITES_HASH_DISTANCE", "6")),
                )
                wx = WeChat(nickname=account.nickname)
                agent = WxautoxBridgeAgent(
                    wx=wx,
                    client=client,
                    agent_id=account.agent_id,
                    capture_dir=state_dir / "capture" / account.account_id,
                    sticker_capture=UiaStickerCapture() if sticker_enabled else None,
                    sticker_sender=sticker_sender if sticker_sender.ready else None,
                    bridge_version="0.3.0",
                    wechat_version=os.getenv("WECHAT_CLIENT_VERSION", "").strip(),
                    plus_active=(
                        package_version != "unknown"
                        and os.getenv("WECHAT_PLUS_ACTIVE", "false").strip().lower() == "true"
                    ),
                    ui_lock=ui_lock,
                    configuration_version=account.configuration_version,
                    binding_version=account.binding_version,
                    window_nickname=account.nickname,
                    wechat_id=account.wechat_id,
                    validation_mode=bool(arguments.validation_mode),
                )
                agent.start()
            except Exception:
                client.close()
                logging.exception(
                    "微信窗口启动失败，已只停用对应账号 account=%s nickname=%s",
                    account.account_id,
                    account.nickname,
                )
                continue
            clients.append(client)
            agents.append(agent)
            logging.info(
                "微信账号桥接已启动 account=%s character=%s nickname=%s binding_version=%s mode=%s",
                account.account_id,
                account.character_id or "灾难恢复",
                account.nickname,
                account.binding_version,
                "validation" if arguments.validation_mode else "live",
            )
        if not agents:
            logging.error("没有任何微信窗口通过预检，桥接未启动")
            return 2
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("收到退出信号，正在停止微信桥接")
    finally:
        for agent in reversed(agents):
            agent.stop()
        for client in reversed(clients):
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
