"""供 Windows wxautox4 Plus 薄代理调用的本机桥接 API。"""
from __future__ import annotations

import os
import secrets
import hashlib
import uuid
from datetime import timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import FileResponse

from bridge.v1_models import (
    AckRequest,
    ActionStatusResponse,
    AttachmentUploadResponse,
    BridgeAccountBinding,
    BridgeAccountsResponse,
    BridgeContact,
    BridgeContactsResponse,
    ContactCandidatesRequest,
    FailRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    InboundEnvelope,
    InboundReceipt,
    LeaseRequest,
    LeaseResponse,
    OutboundAction,
)
from bridge.v1_store import (
    BridgeStateConflict,
    BridgeStore,
    OutboundActionNotFound,
    UnknownContact,
)
from bridge.account_router import (
    DEFAULT_BRIDGE_ACCOUNT_ID,
    UnknownBridgeAccount,
    bridge_store_for_account,
    normalize_account_id,
)


router = APIRouter(prefix="/api/bridge/v1", tags=["wechat-bridge-v1"])


def _authorize_bridge(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("WECHAT_BRIDGE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="微信桥接 Token 尚未配置",
        )
    scheme, _, supplied = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="微信桥接认证失败",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _store_for(account_id: str) -> BridgeStore:
    try:
        return bridge_store_for_account(account_id)
    except UnknownBridgeAccount as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


_ATTACHMENT_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


@router.get(
    "/accounts",
    response_model=BridgeAccountsResponse,
    dependencies=[Depends(_authorize_bridge)],
)
def accounts() -> BridgeAccountsResponse:
    from scripts.character_registry import (
        list_wechat_bindings,
        wechat_binding_configuration_version,
    )

    bindings = list_wechat_bindings(approved_only=True)
    return BridgeAccountsResponse(
        configuration_version=wechat_binding_configuration_version(approved_only=True),
        accounts=[
            BridgeAccountBinding(
                account_id=str(item["account_id"]),
                character_id=str(item["character_id"]),
                display_name=str(item["display_name"]),
                window_nickname=str(item["window_nickname"]),
                wechat_id=str(item["wechat_id"]),
                owner_remark=str(item["owner_remark"]),
                binding_version=int(item["binding_version"]),
            )
            for item in bindings
        ],
    )


@router.get(
    "/contacts",
    response_model=BridgeContactsResponse,
    dependencies=[Depends(_authorize_bridge)],
)
def contacts(
    x_wechat_account_id: str = Header(default=DEFAULT_BRIDGE_ACCOUNT_ID, alias="X-WeChat-Account-ID"),
) -> BridgeContactsResponse:
    try:
        account_id = normalize_account_id(x_wechat_account_id)
    except UnknownBridgeAccount as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    store = _store_for(account_id)
    return BridgeContactsResponse(
        account_id=account_id,
        contacts=[
            BridgeContact(
                external_contact_id=contact.external_id,
                display_name=contact.display_name,
            )
            for contact in store.list_contacts(include_disabled=False)
        ]
    )


@router.post(
    "/contact-candidates",
    dependencies=[Depends(_authorize_bridge)],
)
def contact_candidates(payload: ContactCandidatesRequest) -> dict[str, int]:
    store = _store_for(payload.account_id)
    return {"recorded": store.record_contact_candidates(payload.display_names)}


@router.post(
    "/attachments",
    response_model=AttachmentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_authorize_bridge)],
)
async def upload_attachment(
    request: Request,
    x_external_contact_id: str = Header(alias="X-External-Contact-ID"),
    x_attachment_kind: str = Header(alias="X-Attachment-Kind"),
    x_attachment_sha256: str = Header(alias="X-Attachment-SHA256"),
    x_attachment_mime: str = Header(alias="X-Attachment-Mime"),
    x_wechat_account_id: str = Header(default=DEFAULT_BRIDGE_ACCOUNT_ID, alias="X-WeChat-Account-ID"),
) -> AttachmentUploadResponse:
    store = _store_for(x_wechat_account_id)
    kind = str(x_attachment_kind or "").strip().lower()
    mime_type = str(x_attachment_mime or "").split(";", 1)[0].strip().lower()
    expected_sha = str(x_attachment_sha256 or "").strip().lower()
    if kind not in {"image", "sticker"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="附件类型无效")
    extension = _ATTACHMENT_EXTENSIONS.get(mime_type)
    if extension is None:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="不支持的图片格式")
    if store.get_contact_by_external(x_external_contact_id) is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="联系人不在白名单中")

    maximum = min(max(int(os.getenv("BRIDGE_ATTACHMENT_MAX_BYTES", 20 * 1024 * 1024)), 1), 50 * 1024 * 1024)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Content-Length 无效") from exc
        if declared_size > maximum:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="附件超过大小限制")
    content = await request.body()
    if not content:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="附件内容不能为空")
    if len(content) > maximum:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="附件超过大小限制")
    actual_sha = hashlib.sha256(content).hexdigest()
    if not secrets.compare_digest(actual_sha, expected_sha):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="附件摘要校验失败")

    relative_path = Path("attachments") / kind / actual_sha[:2] / f"{actual_sha}{extension}"
    target_path = store.database_path.parent / relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not target_path.exists():
        temporary_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary_path.write_bytes(content)
            os.replace(temporary_path, target_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    try:
        attachment = store.register_attachment(
            external_contact_id=x_external_contact_id,
            kind=kind,
            storage_path=relative_path.as_posix(),
            sha256=actual_sha,
            mime_type=mime_type,
            size_bytes=len(content),
        )
    except UnknownContact as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="联系人不在白名单中") from exc
    except (BridgeStateConflict, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return AttachmentUploadResponse(
        attachment_ref=attachment.id,
        sha256=attachment.sha256,
        kind=attachment.kind,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        duplicate=attachment.duplicate,
    )


@router.get(
    "/attachments/{attachment_id}",
    response_class=FileResponse,
    dependencies=[Depends(_authorize_bridge)],
)
def download_attachment(
    attachment_id: str,
    x_external_contact_id: str = Header(alias="X-External-Contact-ID"),
    x_wechat_account_id: str = Header(default=DEFAULT_BRIDGE_ACCOUNT_ID, alias="X-WeChat-Account-ID"),
) -> FileResponse:
    store = _store_for(x_wechat_account_id)
    try:
        attachment = store.get_attachment_for_contact(
            attachment_id=attachment_id,
            external_contact_id=x_external_contact_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件不存在") from exc
    data_root = store.database_path.parent.resolve()
    attachment_path = (data_root / attachment.storage_path).resolve()
    try:
        attachment_path.relative_to(data_root)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="附件存储路径无效") from exc
    if not attachment_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件文件不存在")
    return FileResponse(
        attachment_path,
        media_type=attachment.mime_type,
        filename=f"{attachment.sha256}{attachment_path.suffix.lower()}",
    )


@router.post(
    "/heartbeat",
    response_model=HeartbeatResponse,
    dependencies=[Depends(_authorize_bridge)],
)
def heartbeat(payload: HeartbeatRequest) -> HeartbeatResponse:
    from scripts.character_registry import list_wechat_bindings, wechat_binding_configuration_version

    store = _store_for(payload.account_id)
    bindings = list_wechat_bindings(approved_only=True)
    expected = next((item for item in bindings if str(item["account_id"]) == payload.account_id), None)
    current_configuration_version = wechat_binding_configuration_version(approved_only=True)
    binding_valid = bool(
        expected
        and payload.configuration_version == current_configuration_version
        and payload.binding_version == int(expected["binding_version"])
        and payload.window_nickname == str(expected["window_nickname"])
        and payload.wechat_id == str(expected["wechat_id"])
    )
    server_time = store.record_heartbeat(
        agent_id=payload.agent_id,
        bridge_version=payload.bridge_version,
        wechat_version=payload.wechat_version,
        plus_active=payload.plus_active,
        window_ready=payload.window_ready and binding_valid,
        capabilities=payload.capabilities,
    )
    return HeartbeatResponse(
        server_time=server_time,
        binding_valid=binding_valid,
        configuration_version=current_configuration_version,
        expected_binding_version=int(expected["binding_version"]) if expected else 0,
        message="" if binding_valid else "微信绑定已变化或账号身份不一致，请停止该窗口并重新同步",
    )


@router.post(
    "/inbound",
    response_model=InboundReceipt,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_authorize_bridge)],
)
def inbound(payload: InboundEnvelope) -> InboundReceipt:
    store = _store_for(payload.account_id)
    try:
        receipt = store.record_inbound(payload)
    except UnknownContact as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="联系人不在白名单中") from exc
    except BridgeStateConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return InboundReceipt(action=receipt.action, event_id=receipt.event_id)


@router.post(
    "/outbox/lease",
    response_model=LeaseResponse,
    dependencies=[Depends(_authorize_bridge)],
)
def lease(payload: LeaseRequest) -> LeaseResponse:
    store = _store_for(payload.account_id)
    actions = store.lease_actions(
        agent_id=payload.agent_id,
        limit=payload.limit,
        lease_seconds=payload.lease_seconds,
    )
    return LeaseResponse(
        actions=[
            OutboundAction(
                action_id=item.action_id,
                account_id=payload.account_id,
                external_contact_id=item.external_contact_id,
                type=item.action_type,
                scheduled_at=item.scheduled_at.astimezone(timezone.utc),
                payload=item.payload,
                lease_token=item.lease_token,
                lease_expires_at=item.lease_expires_at.astimezone(timezone.utc),
                attempt=item.attempt,
            )
            for item in actions
        ]
    )


@router.post(
    "/outbox/{action_id}/ack",
    response_model=ActionStatusResponse,
    dependencies=[Depends(_authorize_bridge)],
)
def ack(action_id: str, payload: AckRequest) -> ActionStatusResponse:
    store = _store_for(payload.account_id)
    try:
        action = store.ack_action(
            action_id=action_id,
            agent_id=payload.agent_id,
            lease_token=payload.lease_token,
            provider_message_id=payload.provider_message_id,
        )
    except OutboundActionNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="出站动作不存在") from exc
    except BridgeStateConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ActionStatusResponse(action_id=action.action_id, status=action.status)


@router.post(
    "/outbox/{action_id}/fail",
    response_model=ActionStatusResponse,
    dependencies=[Depends(_authorize_bridge)],
)
def fail(action_id: str, payload: FailRequest) -> ActionStatusResponse:
    store = _store_for(payload.account_id)
    try:
        action = store.fail_action(
            action_id=action_id,
            agent_id=payload.agent_id,
            lease_token=payload.lease_token,
            reason=payload.reason,
            retryable=payload.retryable,
            delivery_unknown=payload.delivery_unknown,
            retry_delay_seconds=payload.retry_delay_seconds,
        )
    except OutboundActionNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="出站动作不存在") from exc
    except BridgeStateConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ActionStatusResponse(action_id=action.action_id, status=action.status)
