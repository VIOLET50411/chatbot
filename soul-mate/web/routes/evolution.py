"""人格慢速演变的 Owner 状态、暂停、发布与回滚 API。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from persona.evolution import EvolutionPolicyError, PersonaEvolutionService
from web.character_access import store_from_request


class EvolutionControlUpdate(BaseModel):
    paused: bool
    reason: str = Field(min_length=3, max_length=500)


class EvolutionState(BaseModel):
    control: dict[str, Any]
    events: list[dict[str, Any]]


def create_evolution_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    consume_confirmation: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/persona/evolution", tags=["owner-persona-evolution"])

    def service(request: Request) -> PersonaEvolutionService:
        return PersonaEvolutionService(store_from_request(request))

    @router.get("", response_model=EvolutionState)
    def state(
        owner: str = Depends(read_auth),
        evolution: PersonaEvolutionService = Depends(service),
    ) -> EvolutionState:
        del owner
        return EvolutionState(control=evolution.get_control(), events=evolution.list_events())

    @router.patch("/control", response_model=dict[str, Any])
    def update_control(
        body: EvolutionControlUpdate,
        request: Request,
        owner: str = Depends(write_auth),
        evolution: PersonaEvolutionService = Depends(service),
    ) -> dict[str, Any]:
        try:
            control = evolution.set_paused(body.paused, reason=body.reason, actor=owner)
        except EvolutionPolicyError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="persona.evolution.pause" if body.paused else "persona.evolution.resume",
            scope_type="persona_evolution",
            outcome="success",
            reason=body.reason,
            after={"paused": body.paused},
        )
        return control

    @router.post("/{event_id}/publish", response_model=dict[str, Any])
    def publish(
        event_id: str,
        request: Request,
        owner: str = Depends(write_auth),
        evolution: PersonaEvolutionService = Depends(service),
    ) -> dict[str, Any]:
        reason = consume_confirmation(
            request,
            owner=owner,
            action="persona.evolution.publish",
            scope="persona_evolution",
            resource_id=event_id,
        )
        try:
            event = evolution.publish_candidate(event_id, actor=owner)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except EvolutionPolicyError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="persona.evolution.publish",
            scope_type="persona_evolution",
            outcome="success",
            resource_id=event_id,
            reason=reason,
            after={"status": event["status"], "result_snapshot_id": event["result_snapshot_id"]},
        )
        return event

    @router.post("/{event_id}/rollback", response_model=dict[str, Any])
    def rollback(
        event_id: str,
        request: Request,
        owner: str = Depends(write_auth),
        evolution: PersonaEvolutionService = Depends(service),
    ) -> dict[str, Any]:
        reason = consume_confirmation(
            request,
            owner=owner,
            action="persona.evolution.rollback",
            scope="persona_evolution",
            resource_id=event_id,
        )
        try:
            event = evolution.rollback(event_id, actor=owner)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except EvolutionPolicyError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="persona.evolution.rollback",
            scope_type="persona_evolution",
            outcome="success",
            resource_id=event_id,
            reason=reason,
            after={"status": event["status"], "baseline_snapshot_id": event["baseline_snapshot_id"]},
        )
        return event

    return router
