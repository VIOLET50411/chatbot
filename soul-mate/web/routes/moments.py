"""朋友圈草稿、确认发布与未知结果核验。"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bridge.v1_store import BridgeStateConflict, BridgeStore, MomentPost
from web.character_access import store_from_request


class MomentPrepare(BaseModel):
    text: str = Field(default="", max_length=1500)
    attachment_refs: list[str] = Field(default_factory=list, max_length=9)
    privacy_tags: list[str] = Field(min_length=1, max_length=50)
    source: Literal["manual", "manual_test"] = "manual_test"


class MomentResolve(BaseModel):
    published: bool
    note: str = Field(min_length=1, max_length=500)


class MomentView(BaseModel):
    id: str
    status: str
    text: str
    attachment_refs: list[str]
    privacy_tags: list[str]
    source: str
    outbound_action_id: str | None
    provider_post_id: str | None
    error_summary: str
    created_at: str
    published_at: str | None


class MomentList(BaseModel):
    items: list[MomentView]


def _view(post: MomentPost) -> MomentView:
    return MomentView(
        id=post.id,
        status=post.status,
        text=post.text,
        attachment_refs=list(post.attachment_refs),
        privacy_tags=list(post.privacy_tags),
        source=post.source,
        outbound_action_id=post.outbound_action_id,
        provider_post_id=post.provider_post_id,
        error_summary=post.error_summary,
        created_at=post.created_at.isoformat(),
        published_at=post.published_at.isoformat() if post.published_at else None,
    )


def create_moments_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    consume_confirmation: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/wechat/moments", tags=["owner-moments"])

    def store(request: Request) -> BridgeStore:
        return store_from_request(request)

    @router.get("", response_model=MomentList)
    def list_moments(
        owner: str = Depends(read_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> MomentList:
        del owner
        return MomentList(items=[_view(item) for item in bridge_store.list_moments()])

    @router.post("", response_model=MomentView, status_code=status.HTTP_201_CREATED)
    def prepare(
        body: MomentPrepare,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> MomentView:
        try:
            post = bridge_store.prepare_moment(
                text=body.text,
                attachment_refs=body.attachment_refs,
                privacy_tags=body.privacy_tags,
                source=body.source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except BridgeStateConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="moment.prepare",
            scope_type="moment",
            outcome="success",
            resource_id=post.id,
            after={"status": post.status, "source": post.source, "tag_count": len(post.privacy_tags)},
        )
        return _view(post)

    @router.post("/{post_id}/dispatch", response_model=MomentView)
    def dispatch(
        post_id: str,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> MomentView:
        reason = consume_confirmation(
            request,
            owner=owner,
            action="moment.dispatch",
            scope="moment",
            resource_id=post_id,
        )
        try:
            post = bridge_store.dispatch_moment(
                post_id,
                scheduled_at=datetime.now(timezone.utc),
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except BridgeStateConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="moment.dispatch",
            scope_type="moment",
            outcome="success",
            resource_id=post.id,
            reason=reason,
            after={"status": post.status, "outbound_action_id": post.outbound_action_id},
        )
        return _view(post)

    @router.post("/{post_id}/resolve", response_model=MomentView)
    def resolve(
        post_id: str,
        body: MomentResolve,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> MomentView:
        reason = consume_confirmation(
            request,
            owner=owner,
            action="moment.resolve",
            scope="moment",
            resource_id=post_id,
        )
        try:
            post = bridge_store.resolve_moment_unknown(
                post_id,
                published=body.published,
                note=body.note,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except (ValueError, BridgeStateConflict) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="moment.resolve",
            scope_type="moment",
            outcome="success",
            resource_id=post.id,
            reason=reason,
            after={"status": post.status},
        )
        return _view(post)

    return router
