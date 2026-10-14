"""Windows 微信薄代理与 Docker 核心之间的稳定契约。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


InboundType = Literal["text", "voice", "image", "emoji", "sticker"]
InboundContextMode = Literal["live", "validation"]
OutboundType = Literal["text", "image", "emoji", "sticker", "moment"]
DEFAULT_ACCOUNT_ID = "du-jiaying"


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(timezone.utc)


class InboundEnvelope(BaseModel):
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:._-]+$")
    external_contact_id: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    type: InboundType
    text: str | None = Field(default=None, max_length=20_000)
    attachment_refs: list[str] = Field(default_factory=list, max_length=9)
    message_hash: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9:._-]+$",
    )
    agent_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(default=DEFAULT_ACCOUNT_ID, min_length=1, max_length=64)
    context_mode: InboundContextMode = "live"

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("external_contact_id", "agent_id", "account_id")
    @classmethod
    def strip_identifiers(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("标识不能为空")
        return normalized

    @field_validator("attachment_refs")
    @classmethod
    def validate_attachment_refs(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if any(len(item) > 256 for item in normalized):
            raise ValueError("附件引用过长")
        return normalized

    @model_validator(mode="after")
    def validate_content(self) -> "InboundEnvelope":
        normalized_text = self.text.strip() if isinstance(self.text, str) else None
        self.text = normalized_text or None
        if self.type in {"text", "voice", "emoji"} and not self.text:
            raise ValueError("文字、语音转写或 Unicode 表情消息不能为空")
        if self.type in {"image", "sticker"} and not self.attachment_refs:
            raise ValueError("图片或表情消息必须包含附件引用")
        return self


class HeartbeatRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(default=DEFAULT_ACCOUNT_ID, min_length=1, max_length=64)
    bridge_version: str = Field(default="", max_length=64)
    wechat_version: str = Field(default="", max_length=64)
    plus_active: bool = False
    window_ready: bool = False
    capabilities: list[str] = Field(default_factory=list, max_length=32)
    configuration_version: str = Field(default="", max_length=128)
    binding_version: int = Field(default=0, ge=0)
    window_nickname: str = Field(default="", max_length=128)
    wechat_id: str = Field(default="", max_length=128)

    @field_validator("agent_id")
    @classmethod
    def strip_agent_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("代理标识不能为空")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, value: list[str]) -> list[str]:
        result = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if any(len(item) > 64 for item in result):
            raise ValueError("能力标识过长")
        return result


class LeaseRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(default=DEFAULT_ACCOUNT_ID, min_length=1, max_length=64)
    limit: int = Field(default=10, ge=1, le=50)
    lease_seconds: int = Field(default=30, ge=10, le=120)


class AckRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(default=DEFAULT_ACCOUNT_ID, min_length=1, max_length=64)
    lease_token: str = Field(min_length=16, max_length=128)
    provider_message_id: str | None = Field(default=None, max_length=256)


class FailRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(default=DEFAULT_ACCOUNT_ID, min_length=1, max_length=64)
    lease_token: str = Field(min_length=16, max_length=128)
    reason: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    delivery_unknown: bool = False
    retry_delay_seconds: int = Field(default=5, ge=0, le=300)


class InboundReceipt(BaseModel):
    action: Literal["accepted", "duplicate"]
    event_id: str


class HeartbeatResponse(BaseModel):
    status: Literal["online"] = "online"
    server_time: datetime
    binding_valid: bool = True
    configuration_version: str = ""
    expected_binding_version: int = 0
    message: str = ""


class BridgeContact(BaseModel):
    external_contact_id: str
    display_name: str


class BridgeContactsResponse(BaseModel):
    account_id: str = DEFAULT_ACCOUNT_ID
    contacts: list[BridgeContact]


class BridgeAccountBinding(BaseModel):
    account_id: str
    character_id: str
    display_name: str
    window_nickname: str
    wechat_id: str
    owner_remark: str
    binding_version: int = Field(ge=1)


class BridgeAccountsResponse(BaseModel):
    configuration_version: str
    accounts: list[BridgeAccountBinding]


class ContactCandidatesRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(default=DEFAULT_ACCOUNT_ID, min_length=1, max_length=64)
    display_names: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("display_names")
    @classmethod
    def normalize_display_names(cls, value: list[str]) -> list[str]:
        names = list(dict.fromkeys(str(item or "").strip() for item in value if str(item or "").strip()))
        if any(len(item) > 128 for item in names):
            raise ValueError("联系人候选名称过长")
        return names


class AttachmentUploadResponse(BaseModel):
    attachment_ref: str
    sha256: str
    kind: Literal["image", "sticker"]
    mime_type: str
    size_bytes: int
    duplicate: bool


class OutboundAction(BaseModel):
    action_id: str
    account_id: str = DEFAULT_ACCOUNT_ID
    external_contact_id: str
    type: OutboundType
    scheduled_at: datetime
    payload: dict[str, Any]
    lease_token: str
    lease_expires_at: datetime
    attempt: int


class LeaseResponse(BaseModel):
    actions: list[OutboundAction]


class ActionStatusResponse(BaseModel):
    action_id: str
    status: str
