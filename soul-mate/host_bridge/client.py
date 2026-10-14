"""宿主机代理访问 Docker 核心的同步 HTTP 客户端。"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


class BridgeHttpError(RuntimeError):
    """核心桥接接口不可用或拒绝了请求。"""


class BridgeHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 15.0,
        allow_remote: bool = False,
        transport: httpx.BaseTransport | None = None,
        account_id: str = "du-jiaying",
    ) -> None:
        normalized_url = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("桥接核心地址无效")
        if not allow_remote and parsed.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("桥接核心默认只允许连接本机地址")
        normalized_token = str(token or "").strip()
        if len(normalized_token) < 24:
            raise ValueError("WECHAT_BRIDGE_TOKEN 至少需要 24 个字符")
        normalized_account_id = str(account_id or "").strip().lower()
        if not normalized_account_id:
            raise ValueError("微信账号 ID 不能为空")
        self.account_id = normalized_account_id
        self._client = httpx.Client(
            base_url=normalized_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {normalized_token}"},
        )

    def close(self) -> None:
        self._client.close()

    def _json_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BridgeHttpError(f"核心桥接请求失败：{method} {path}") from exc
        if not isinstance(payload, dict):
            raise BridgeHttpError("核心桥接返回了无效响应")
        return payload

    def contacts(self) -> list[dict[str, str]]:
        payload = self._json_request(
            "GET",
            "/api/bridge/v1/contacts",
            headers={"X-WeChat-Account-ID": self.account_id},
        )
        contacts = payload.get("contacts")
        if not isinstance(contacts, list):
            raise BridgeHttpError("白名单联系人响应无效")
        return [
            {
                "external_contact_id": str(item.get("external_contact_id") or "").strip(),
                "display_name": str(item.get("display_name") or "").strip(),
            }
            for item in contacts
            if isinstance(item, dict)
            and str(item.get("external_contact_id") or "").strip()
            and str(item.get("display_name") or "").strip()
        ]

    def accounts(self) -> dict[str, Any]:
        payload = self._json_request("GET", "/api/bridge/v1/accounts")
        accounts = payload.get("accounts")
        configuration_version = str(payload.get("configuration_version") or "").strip()
        if not isinstance(accounts, list) or not configuration_version:
            raise BridgeHttpError("微信账号目录响应无效")
        return {
            "configuration_version": configuration_version,
            "accounts": [item for item in accounts if isinstance(item, dict)],
        }

    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json_request(
            "POST",
            "/api/bridge/v1/heartbeat",
            json={**payload, "account_id": self.account_id},
        )

    def report_contact_candidates(self, *, agent_id: str, display_names: list[str]) -> int:
        payload = self._json_request(
            "POST",
            "/api/bridge/v1/contact-candidates",
            json={
                "agent_id": agent_id,
                "account_id": self.account_id,
                "display_names": display_names,
            },
        )
        return int(payload.get("recorded") or 0)

    def upload_attachment(
        self,
        *,
        external_contact_id: str,
        kind: str,
        file_path: str | Path,
    ) -> dict[str, Any]:
        path = Path(file_path)
        content = path.read_bytes()
        mime_type = mimetypes.guess_type(path.name)[0] or ""
        if mime_type == "image/jpg":
            mime_type = "image/jpeg"
        if mime_type not in {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}:
            mime_type = self._sniff_image_mime(content)
        if not mime_type:
            raise BridgeHttpError("无法识别待上传图片格式")
        digest = hashlib.sha256(content).hexdigest()
        return self._json_request(
            "POST",
            "/api/bridge/v1/attachments",
            headers={
                "X-External-Contact-ID": external_contact_id,
                "X-Attachment-Kind": kind,
                "X-Attachment-SHA256": digest,
                "X-Attachment-Mime": mime_type,
                "X-WeChat-Account-ID": self.account_id,
                "Content-Type": "application/octet-stream",
            },
            content=content,
        )

    @staticmethod
    def _sniff_image_mime(content: bytes) -> str:
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if content.startswith(b"BM"):
            return "image/bmp"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return "image/webp"
        return ""

    def inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json_request(
            "POST",
            "/api/bridge/v1/inbound",
            json={**payload, "account_id": self.account_id},
        )

    def lease(self, *, agent_id: str, limit: int = 10, lease_seconds: int = 60) -> list[dict[str, Any]]:
        payload = self._json_request(
            "POST",
            "/api/bridge/v1/outbox/lease",
            json={
                "agent_id": agent_id,
                "account_id": self.account_id,
                "limit": limit,
                "lease_seconds": lease_seconds,
            },
        )
        actions = payload.get("actions")
        if not isinstance(actions, list):
            raise BridgeHttpError("出站租约响应无效")
        return [item for item in actions if isinstance(item, dict)]

    def ack(self, *, action_id: str, agent_id: str, lease_token: str) -> dict[str, Any]:
        return self._json_request(
            "POST",
            f"/api/bridge/v1/outbox/{action_id}/ack",
            json={"agent_id": agent_id, "account_id": self.account_id, "lease_token": lease_token},
        )

    def fail(
        self,
        *,
        action_id: str,
        agent_id: str,
        lease_token: str,
        reason: str,
        retryable: bool,
        delivery_unknown: bool,
        retry_delay_seconds: int = 5,
    ) -> dict[str, Any]:
        return self._json_request(
            "POST",
            f"/api/bridge/v1/outbox/{action_id}/fail",
            json={
                "agent_id": agent_id,
                "account_id": self.account_id,
                "lease_token": lease_token,
                "reason": reason[:500],
                "retryable": retryable,
                "delivery_unknown": delivery_unknown,
                "retry_delay_seconds": retry_delay_seconds,
            },
        )

    def download_attachment(
        self,
        *,
        attachment_ref: str,
        external_contact_id: str,
        destination_dir: str | Path,
    ) -> Path:
        try:
            response = self._client.get(
                f"/api/bridge/v1/attachments/{attachment_ref}",
                headers={
                    "X-External-Contact-ID": external_contact_id,
                    "X-WeChat-Account-ID": self.account_id,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BridgeHttpError("附件下载失败") from exc
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        extension = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }.get(content_type)
        if extension is None:
            raise BridgeHttpError("附件响应格式无效")
        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"{attachment_ref}{extension}"
        target.write_bytes(response.content)
        return target
