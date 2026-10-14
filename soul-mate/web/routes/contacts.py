"""单人设模式下的微信白名单联系人管理。"""
from __future__ import annotations

from collections.abc import Callable
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from bridge.v1_store import BridgeStore, Contact, UnknownContact
from core.contact_runtime_store import ContactRuntimeStore
from core.pwa_bindings import get_pwa_contact_binding, set_pwa_contact_binding
from core.runtime_settings import runtime_value
from web.character_access import context_from_request, store_from_request


class ContactCreate(BaseModel):
    external_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)


class ContactUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    proactive_enabled: bool | None = None
    proactive_max_daily: int | None = Field(default=None, ge=1, le=10)
    proactive_cooldown_hours: int | None = Field(default=None, ge=1, le=48)
    proactive_active_start_hour: int | None = Field(default=None, ge=0, le=23)
    proactive_active_end_hour: int | None = Field(default=None, ge=1, le=24)
    proactive_check_in_after_hours: int | None = Field(default=None, ge=1, le=168)

    @model_validator(mode="after")
    def at_least_one_field(self) -> "ContactUpdate":
        if all(value is None for value in (
            self.display_name,
            self.enabled,
            self.proactive_enabled,
            self.proactive_max_daily,
            self.proactive_cooldown_hours,
            self.proactive_active_start_hour,
            self.proactive_active_end_hour,
            self.proactive_check_in_after_hours,
        )):
            raise ValueError("至少需要修改一个联系人字段")
        if (
            self.proactive_active_start_hour is not None
            and self.proactive_active_end_hour is not None
            and self.proactive_active_start_hour == self.proactive_active_end_hour
        ):
            raise ValueError("主动联系开始和结束时间不能相同")
        return self


class ContactView(BaseModel):
    id: str
    external_id: str
    display_name: str
    enabled: bool
    pwa_bound: bool = False
    proactive_enabled: bool = False
    proactive_max_daily: int = 4
    proactive_cooldown_hours: int = 4
    proactive_active_start_hour: int = 8
    proactive_active_end_hour: int = 24
    proactive_check_in_after_hours: int = 8


class ContactList(BaseModel):
    items: list[ContactView]


class MemoryReview(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=1000)
    status: str | None = None

    @model_validator(mode="after")
    def at_least_one_change(self) -> "MemoryReview":
        if self.content is None and self.status is None:
            raise ValueError("至少需要修改内容或状态")
        if self.status is not None and self.status not in {"candidate", "published", "rejected", "archived"}:
            raise ValueError("记忆状态无效")
        return self


class ContactCandidateReview(BaseModel):
    decision: str

    @model_validator(mode="after")
    def validate_decision(self) -> "ContactCandidateReview":
        if self.decision not in {"confirm", "ignore"}:
            raise ValueError("请选择确认或忽略")
        return self


def _setting_integer(name: str, default: int) -> int:
    try:
        return int(runtime_value(name, str(default)))
    except (TypeError, ValueError):
        return default


def _view(contact: Contact, *, store: BridgeStore, pwa_bound: bool = False) -> ContactView:
    policy = store.get_contact_delivery_policy(contact.id)
    return ContactView(
        id=contact.id,
        external_id=contact.external_id,
        display_name=contact.display_name,
        enabled=contact.enabled,
        pwa_bound=pwa_bound,
        proactive_enabled=bool(policy["proactive_enabled"]),
        proactive_max_daily=int(policy["max_daily"] or _setting_integer("PROACTIVE_MAX_DAILY", 4)),
        proactive_cooldown_hours=int(policy["cooldown_hours"] or _setting_integer("PROACTIVE_COOLDOWN_HOURS", 4)),
        proactive_active_start_hour=(
            int(policy["active_start_hour"])
            if policy["active_start_hour"] is not None
            else _setting_integer("PROACTIVE_ACTIVE_START_HOUR", 8)
        ),
        proactive_active_end_hour=int(policy["active_end_hour"] or _setting_integer("PROACTIVE_ACTIVE_END_HOUR", 24)),
        proactive_check_in_after_hours=int(
            policy["check_in_after_hours"] or _setting_integer("PROACTIVE_CHECK_IN_AFTER_HOURS", 8)
        ),
    )


def _audit_snapshot(contact: Contact) -> dict[str, object]:
    return {
        "contact_id": contact.id,
        "display_name": contact.display_name,
        "enabled": contact.enabled,
    }


def create_contacts_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    consume_confirmation: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/contacts", tags=["owner-contacts"])

    def store(request: Request) -> BridgeStore:
        return store_from_request(request)

    @router.get("", response_model=ContactList)
    def list_contacts(
        request: Request,
        owner: str = Depends(read_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> ContactList:
        del owner
        context = context_from_request(request)
        binding = get_pwa_contact_binding(context.character_id)
        return ContactList(
            items=[
                _view(item, store=bridge_store, pwa_bound=bool(binding and binding.contact_id == item.id))
                for item in bridge_store.list_contacts(include_disabled=True)
            ]
        )

    @router.post("", response_model=ContactView, status_code=status.HTTP_201_CREATED)
    def create_contact(
        body: ContactCreate,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> ContactView:
        if bridge_store.get_contact_by_external(body.external_id, enabled_only=False) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该微信联系人已存在")
        if any(
            item.display_name.casefold() == body.display_name.strip().casefold()
            for item in bridge_store.list_contacts(include_disabled=True)
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="微信备注名必须唯一")
        try:
            contact = bridge_store.create_contact(
                external_id=body.external_id,
                display_name=body.display_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="contact.create",
            scope_type="contact",
            outcome="success",
            resource_id=contact.id,
            after=_audit_snapshot(contact),
        )
        return _view(contact, store=bridge_store)

    @router.get("/candidates")
    def list_contact_candidates(
        owner: str = Depends(read_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> dict[str, Any]:
        del owner
        return {"items": bridge_store.list_contact_candidates()}

    @router.post("/candidates/{candidate_id}/review")
    def review_contact_candidate(
        candidate_id: str,
        body: ContactCandidateReview,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> dict[str, Any]:
        try:
            contact = bridge_store.review_contact_candidate(candidate_id, decision=body.decision)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="contact.candidate.review",
            scope_type="contact_candidate",
            outcome="success",
            resource_id=candidate_id,
            after={"decision": body.decision, "contact_id": contact.id if contact else ""},
        )
        return {
            "decision": body.decision,
            "contact": _view(contact, store=bridge_store).model_dump() if contact else None,
        }

    @router.patch("/{contact_id}", response_model=ContactView)
    def update_contact(
        contact_id: str,
        body: ContactUpdate,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> ContactView:
        current = bridge_store.get_contact(contact_id)
        if current is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="联系人不存在")
        if body.display_name is not None and any(
            item.id != contact_id
            and item.display_name.casefold() == body.display_name.strip().casefold()
            for item in bridge_store.list_contacts(include_disabled=True)
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="微信备注名必须唯一")
        reason = ""
        if current.enabled and body.enabled is False:
            reason = consume_confirmation(
                request,
                owner=owner,
                action="contact.disable",
                scope="contact",
                resource_id=contact_id,
            )
        try:
            updated = current
            if body.display_name is not None or body.enabled is not None:
                updated = bridge_store.update_contact(
                    contact_id,
                    display_name=body.display_name,
                    enabled=body.enabled,
                )
            if any(value is not None for value in (
                body.proactive_enabled,
                body.proactive_max_daily,
                body.proactive_cooldown_hours,
                body.proactive_active_start_hour,
                body.proactive_active_end_hour,
                body.proactive_check_in_after_hours,
            )):
                bridge_store.update_contact_delivery_policy(
                    contact_id,
                    proactive_enabled=body.proactive_enabled,
                    max_daily=body.proactive_max_daily,
                    cooldown_hours=body.proactive_cooldown_hours,
                    active_start_hour=body.proactive_active_start_hour,
                    active_end_hour=body.proactive_active_end_hour,
                    check_in_after_hours=body.proactive_check_in_after_hours,
                )
        except (UnknownContact, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="contact.update",
            scope_type="contact",
            outcome="success",
            resource_id=contact_id,
            reason=reason,
            before=_audit_snapshot(current),
            after=_audit_snapshot(updated),
        )
        return _view(updated, store=bridge_store)

    @router.put("/{contact_id}/pwa-binding", response_model=ContactView)
    def bind_pwa_contact(
        contact_id: str,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> ContactView:
        contact = bridge_store.get_contact(contact_id)
        if contact is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="联系人不存在")
        if not contact.enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="停用的联系人不能接续 PWA")
        context = context_from_request(request)
        before = get_pwa_contact_binding(context.character_id)
        binding = set_pwa_contact_binding(
            context.character_id,
            contact.id,
            updated_by=owner,
        )
        audit(
            request,
            actor=owner,
            action="pwa.contact.bind",
            scope_type="pwa_contact",
            outcome="success",
            resource_id=contact.id,
            character_id=context.character_id,
            before={"contact_id": before.contact_id} if before else {},
            after={"contact_id": binding.contact_id},
        )
        return _view(contact, store=bridge_store, pwa_bound=True)

    @router.get("/{contact_id}/runtime")
    def contact_runtime(
        contact_id: str,
        owner: str = Depends(read_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> dict[str, Any]:
        del owner
        contact = bridge_store.get_contact(contact_id)
        if contact is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="联系人不存在")
        runtime_store = ContactRuntimeStore(bridge_store.database_path)
        relationship = runtime_store.get_relationship(contact_id, include_disabled=True)
        return {
            "contact": _view(contact, store=bridge_store).model_dump(),
            "relationship": {
                "stage": relationship.stage.value,
                "trust": relationship.trust,
                "intimacy": relationship.intimacy,
                "affection": relationship.affection,
                "comfort": relationship.comfort,
                "resentment": relationship.resentment,
                "last_chat_at": relationship.last_chat_at.isoformat() if relationship.last_chat_at else None,
                "version": relationship.version,
            },
            "profile": runtime_store.get_profile(contact_id, include_disabled=True),
            "memories": runtime_store.list_memories(contact_id, include_disabled=True),
            "timeline": runtime_store.get_timeline(contact_id, include_disabled=True),
            "conversation_checkpoint": runtime_store.get_conversation_checkpoint(contact_id),
            "memory_associations": runtime_store.list_memory_associations(contact_id),
            "pending_topic": runtime_store.get_pending_topic(contact_id),
        }

    @router.patch("/{contact_id}/memories/{memory_id}")
    def review_memory(
        contact_id: str,
        memory_id: str,
        body: MemoryReview,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> dict:
        reason = ""
        if body.status == "archived":
            reason = consume_confirmation(
                request,
                owner=owner,
                action="contact.memory.archive",
                scope="contact_memory",
                resource_id=memory_id,
            )
        runtime_store = ContactRuntimeStore(bridge_store.database_path)
        try:
            updated = runtime_store.review_memory(
                contact_id=contact_id,
                memory_id=memory_id,
                content=body.content,
                status=body.status,
            )
        except UnknownContact as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="联系人不存在或已停用") from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="联系人记忆不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="contact.memory.review",
            scope_type="contact_memory",
            outcome="success",
            resource_id=memory_id,
            reason=reason,
            after={"contact_id": contact_id, "status": updated["status"], "content_changed": body.content is not None},
        )
        return updated

    return router
