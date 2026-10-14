"""人格候选场景盲评 API。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from persona.review_cases import PersonaReviewStore
from web.character_access import store_from_request


class PersonaRatingUpdate(BaseModel):
    rating: Literal["realistic", "acceptable", "artificial"]
    reasons: list[str] = Field(default_factory=list, max_length=12)
    note: str = Field(default="", max_length=2_000)


def create_persona_reviews_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/persona/reviews", tags=["owner-persona-reviews"])

    def review_store(request: Request) -> PersonaReviewStore:
        return PersonaReviewStore(store_from_request(request).database_path)

    @router.get("/{candidate_snapshot_id}")
    def review_cases(
        candidate_snapshot_id: str,
        owner: str = Depends(read_auth),
        store: PersonaReviewStore = Depends(review_store),
    ) -> dict[str, Any]:
        del owner
        return store.list_cases(candidate_snapshot_id)

    @router.put("/cases/{case_id}/rating")
    def rate_case(
        case_id: str,
        body: PersonaRatingUpdate,
        request: Request,
        owner: str = Depends(write_auth),
        store: PersonaReviewStore = Depends(review_store),
    ) -> dict[str, Any]:
        try:
            result = store.rate_case(
                case_id,
                rating=body.rating,
                reasons=body.reasons,
                note=body.note,
                actor=owner,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="persona.review.rate",
            scope_type="persona_review_case",
            outcome="success",
            resource_id=case_id,
            after={"rating": body.rating, "reason_count": len(body.reasons)},
            character_id=request.state.character_context.character_id,
        )
        return result

    return router
