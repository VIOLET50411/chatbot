"""单人设素材审核 API；审核前不改变当前运行人格。"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, model_validator

from bridge.v1_store import BridgeStore
from web.character_access import store_from_request


ReviewDecision = Literal["pending", "keep", "exclude", "split", "corrected"]


class MaterialConclusion(BaseModel):
    claim_id: str
    facet: str
    key: str
    value: str
    confidence: float
    status: str
    support_kind: str
    quote_span: dict[str, Any]


class MaterialReviewView(BaseModel):
    id: str
    source_material_id: str
    source_channel: str
    source_type: str
    content: str
    source_time: str | None
    reliability: float
    decision: ReviewDecision
    corrected_content: str | None
    split_contents: list[str]
    review_note: str
    reviewed_by: str | None
    reviewed_at: str | None
    conclusions: list[MaterialConclusion]


class MaterialReviewList(BaseModel):
    items: list[MaterialReviewView]
    total: int
    limit: int
    offset: int
    summary: dict[str, int]


class MaterialReviewUpdate(BaseModel):
    decision: ReviewDecision
    corrected_content: str | None = Field(default=None, max_length=20_000)
    split_contents: list[str] = Field(default_factory=list, max_length=20)
    review_note: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def validate_decision_payload(self) -> "MaterialReviewUpdate":
        self.corrected_content = self.corrected_content.strip() if self.corrected_content else None
        self.split_contents = [item.strip() for item in self.split_contents if item.strip()]
        self.review_note = self.review_note.strip()
        if self.decision == "corrected" and not self.corrected_content:
            raise ValueError("修正素材必须填写修正文案")
        if self.decision == "split" and len(self.split_contents) < 2:
            raise ValueError("拆分素材至少需要两段内容")
        if any(len(item) > 20_000 for item in self.split_contents):
            raise ValueError("拆分后的单段素材过长")
        if self.decision != "corrected":
            self.corrected_content = None
        if self.decision != "split":
            self.split_contents = []
        return self


class MaterialBatchUpdate(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    decision: Literal["pending", "keep", "exclude"]
    review_note: str = Field(default="", max_length=2_000)


class MaterialBatchResult(BaseModel):
    updated: int


def _parse_json_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_strings(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed if str(item).strip()] if isinstance(parsed, list) else []


def create_materials_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/materials", tags=["owner-materials"])

    def store(request: Request) -> BridgeStore:
        return store_from_request(request)

    def _conclusions(connection: sqlite3.Connection, source_material_id: str) -> list[MaterialConclusion]:
        claim_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='persona_claim'"
        ).fetchone()
        evidence_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claim_evidence'"
        ).fetchone()
        if claim_table is None or evidence_table is None:
            return []
        rows = connection.execute(
            """
            SELECT claim.id, claim.facet, claim.claim_key, claim.claim_value,
                   claim.confidence, claim.status, evidence.support_kind,
                   evidence.quote_span_json
            FROM claim_evidence AS evidence
            JOIN persona_claim AS claim ON claim.id=evidence.claim_id
            WHERE evidence.source_material_id=?
            ORDER BY claim.confidence DESC, claim.created_at, claim.id
            """,
            (source_material_id,),
        ).fetchall()
        return [
            MaterialConclusion(
                claim_id=str(row["id"]),
                facet=str(row["facet"]),
                key=str(row["claim_key"]),
                value=str(row["claim_value"]),
                confidence=float(row["confidence"]),
                status=str(row["status"]),
                support_kind=str(row["support_kind"]),
                quote_span=_parse_json_object(row["quote_span_json"]),
            )
            for row in rows
        ]

    def _view(connection: sqlite3.Connection, row: sqlite3.Row) -> MaterialReviewView:
        return MaterialReviewView(
            id=str(row["id"]),
            source_material_id=str(row["source_material_id"]),
            source_channel=str(row["source_channel"]),
            source_type=str(row["source_type"]),
            content=str(row["content"]),
            source_time=str(row["source_time"]) if row["source_time"] else None,
            reliability=float(row["reliability"]),
            decision=str(row["decision"]),
            corrected_content=str(row["corrected_content"]) if row["corrected_content"] else None,
            split_contents=_parse_json_strings(row["split_contents_json"]),
            review_note=str(row["review_note"] or ""),
            reviewed_by=str(row["reviewed_by"]) if row["reviewed_by"] else None,
            reviewed_at=str(row["reviewed_at"]) if row["reviewed_at"] else None,
            conclusions=_conclusions(connection, str(row["source_material_id"])),
        )

    @router.get("", response_model=MaterialReviewList)
    def list_materials(
        decision: ReviewDecision | None = None,
        source_channel: str | None = Query(default=None, max_length=64),
        query: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        owner: str = Depends(read_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> MaterialReviewList:
        del owner
        where = ["COALESCE(source.is_deleted, 0)=0"]
        params: list[object] = []
        if decision:
            where.append("review.decision=?")
            params.append(decision)
        if source_channel:
            where.append("review.source_channel=?")
            params.append(source_channel.strip())
        if query:
            where.append("source.content LIKE ? ESCAPE '\\'")
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        where_sql = " AND ".join(where)
        connection = sqlite3.connect(bridge_store.database_path)
        connection.row_factory = sqlite3.Row
        try:
            source_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_material'"
            ).fetchone()
            if source_table is None:
                return MaterialReviewList(items=[], total=0, limit=limit, offset=offset, summary={})
            total = int(connection.execute(
                f"""
                SELECT COUNT(*) FROM material_reviews AS review
                JOIN source_material AS source ON source.id=review.source_material_id
                WHERE {where_sql}
                """,
                tuple(params),
            ).fetchone()[0])
            rows = connection.execute(
                f"""
                SELECT review.*, source.source_type, source.content,
                       source.source_time, source.reliability
                FROM material_reviews AS review
                JOIN source_material AS source ON source.id=review.source_material_id
                WHERE {where_sql}
                ORDER BY CASE review.decision WHEN 'pending' THEN 0 ELSE 1 END,
                         source.created_at, review.id
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            summary = {
                str(row["decision"]): int(row["count"])
                for row in connection.execute(
                    "SELECT decision, COUNT(*) AS count FROM material_reviews GROUP BY decision"
                ).fetchall()
            }
            return MaterialReviewList(
                items=[_view(connection, row) for row in rows],
                total=total,
                limit=limit,
                offset=offset,
                summary=summary,
            )
        finally:
            connection.close()

    @router.patch("/{review_id}", response_model=MaterialReviewView)
    def update_material(
        review_id: str,
        body: MaterialReviewUpdate,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> MaterialReviewView:
        now = datetime.now(timezone.utc).isoformat()
        connection = sqlite3.connect(bridge_store.database_path)
        connection.row_factory = sqlite3.Row
        try:
            current = connection.execute(
                "SELECT decision FROM material_reviews WHERE id=?", (review_id,)
            ).fetchone()
            if current is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="素材审核记录不存在")
            connection.execute(
                """
                UPDATE material_reviews
                SET decision=?, corrected_content=?, split_contents_json=?, review_note=?,
                    reviewed_by=?, reviewed_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    body.decision,
                    body.corrected_content,
                    json.dumps(body.split_contents, ensure_ascii=False),
                    body.review_note,
                    owner if body.decision != "pending" else None,
                    now if body.decision != "pending" else None,
                    now,
                    review_id,
                ),
            )
            row = connection.execute(
                """
                SELECT review.*, source.source_type, source.content,
                       source.source_time, source.reliability
                FROM material_reviews AS review
                JOIN source_material AS source ON source.id=review.source_material_id
                WHERE review.id=?
                """,
                (review_id,),
            ).fetchone()
            connection.commit()
            result = _view(connection, row)
        finally:
            connection.close()
        audit(
            request,
            actor=owner,
            action="material.review",
            scope_type="persona_material",
            outcome="success",
            resource_id=review_id,
            before={"decision": str(current["decision"])},
            after={"decision": body.decision},
        )
        return result

    @router.post("/batch", response_model=MaterialBatchResult)
    def batch_update_materials(
        body: MaterialBatchUpdate,
        request: Request,
        owner: str = Depends(write_auth),
        bridge_store: BridgeStore = Depends(store),
    ) -> MaterialBatchResult:
        ids = list(dict.fromkeys(item.strip() for item in body.ids if item.strip()))
        placeholders = ",".join("?" for _ in ids)
        now = datetime.now(timezone.utc).isoformat()
        connection = sqlite3.connect(bridge_store.database_path)
        try:
            cursor = connection.execute(
                f"""
                UPDATE material_reviews
                SET decision=?, corrected_content=NULL, split_contents_json='[]',
                    review_note=?, reviewed_by=?, reviewed_at=?, updated_at=?
                WHERE id IN ({placeholders})
                """,
                (
                    body.decision,
                    body.review_note.strip(),
                    owner if body.decision != "pending" else None,
                    now if body.decision != "pending" else None,
                    now,
                    *ids,
                ),
            )
            updated = int(cursor.rowcount)
            connection.commit()
        finally:
            connection.close()
        audit(
            request,
            actor=owner,
            action="material.review_batch",
            scope_type="persona_material",
            outcome="success",
            after={"decision": body.decision, "updated": updated},
        )
        return MaterialBatchResult(updated=updated)

    return router
