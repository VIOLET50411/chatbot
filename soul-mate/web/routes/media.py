"""入站图片和表情资产的 Owner 审核接口。"""
from __future__ import annotations

import hashlib
import ipaddress
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from bridge.v1_store import BridgeStore
from web.character_access import store_from_request


class MediaReview(BaseModel):
    review_status: Literal["pending", "approved", "rejected"]


class WebMediaImport(BaseModel):
    contact_id: str
    source_page_url: str
    image_url: str
    source_platform: Literal["xiaohongshu", "x", "other"] = "other"
    attribution: str = ""
    usage_scope: Literal["chat", "moments", "both"] = "chat"
    presentation_mode: Literal["source_explicit", "private_roleplay"] = "source_explicit"
    description: str


def _public_https_url(value: str) -> str:
    normalized = str(value or "").strip()
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("网络图片和来源页面必须使用公开 HTTPS 地址")
    host = parsed.hostname.strip("[]")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError("无法解析网络图片域名") from exc
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
            raise ValueError("网络图片地址不能指向本机或内网")
    return normalized


def _image_type(content: bytes) -> tuple[str, str]:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValueError("只支持 JPEG、PNG、WebP 或 GIF 图片")


def create_media_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/wechat/media", tags=["owner-media"])

    def store(request: Request) -> BridgeStore:
        return store_from_request(request)

    @router.get("")
    def list_media(
        owner: str = Depends(read_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> dict:
        del owner
        return {"items": bridge_store.list_media_assets()}

    @router.post("/import-web", status_code=status.HTTP_201_CREATED)
    async def import_web_media(
        body: WebMediaImport,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> dict:
        try:
            source_page_url = _public_https_url(body.source_page_url)
            image_url = _public_https_url(body.image_url)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        description = " ".join(str(body.description or "").split())
        if len(description) < 4 or len(description) > 2000:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="请填写 4–2000 字的图片内容说明")
        contact = bridge_store.get_contact(body.contact_id)
        if contact is None or not contact.enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="接收图片的白名单联系人不存在或已停用")
        try:
            chunks: list[bytes] = []
            total = 0
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=6.0), follow_redirects=False) as client:
                async with client.stream("GET", image_url, headers={"User-Agent": "SoulMate/1.0 reviewed-media-import"}) as response:
                    response.raise_for_status()
                    if 300 <= response.status_code < 400:
                        raise ValueError("网络图片地址不能重定向，请填写最终图片地址")
                    for_header = response.headers.get("content-length", "")
                    if for_header.isdigit() and int(for_header) > 10 * 1024 * 1024:
                        raise ValueError("网络图片不能超过 10 MB")
                    async for chunk in response.aiter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > 10 * 1024 * 1024:
                            raise ValueError("网络图片不能超过 10 MB")
                        chunks.append(chunk)
            content = b"".join(chunks)
            mime_type, suffix = _image_type(content)
        except (httpx.HTTPError, ValueError) as exc:
            detail = str(exc) if isinstance(exc, ValueError) else "网络图片下载失败"
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail) from exc
        digest = hashlib.sha256(content).hexdigest()
        relative = Path("attachments") / "web" / f"{digest}{suffix}"
        target = (bridge_store.database_path.parent / relative).resolve()
        data_root = bridge_store.database_path.parent.resolve()
        try:
            target.relative_to(data_root)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="媒体存储路径无效") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)
        try:
            attachment = bridge_store.register_attachment(
                external_contact_id=contact.external_id,
                kind="image",
                storage_path=relative.as_posix(),
                sha256=digest,
                mime_type=mime_type,
                size_bytes=len(content),
            )
            item = bridge_store.register_web_media_provenance(
                attachment_id=attachment.id,
                source_page_url=source_page_url,
                image_url=image_url,
                source_platform=body.source_platform,
                attribution=" ".join(str(body.attribution or "").split())[:300],
                usage_scope=body.usage_scope,
                presentation_mode=body.presentation_mode,
                vision_summary=description,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="media.web.import",
            scope_type="attachment",
            outcome="success",
            resource_id=attachment.id,
            after={
                "source_platform": body.source_platform,
                "usage_scope": body.usage_scope,
                "presentation_mode": body.presentation_mode,
                "review_status": item["review_status"],
            },
        )
        return item

    @router.patch("/{attachment_id}")
    def review_media(
        attachment_id: str,
        body: MediaReview,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> dict:
        try:
            item = bridge_store.review_media_asset(
                attachment_id,
                review_status=body.review_status,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="媒体资产不存在") from exc
        audit(
            request,
            actor=owner,
            action="media.review",
            scope_type="attachment",
            outcome="success",
            resource_id=attachment_id,
            after={"review_status": body.review_status, "kind": item["kind"]},
        )
        return item

    @router.get("/{attachment_id}/content", response_class=FileResponse)
    def media_content(
        attachment_id: str,
        owner: str = Depends(read_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> FileResponse:
        del owner
        try:
            path, mime_type = bridge_store.get_media_file(attachment_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="媒体文件不存在") from exc
        return FileResponse(
            path,
            media_type=mime_type,
            headers={"Cache-Control": "private, no-store"},
        )

    return router
