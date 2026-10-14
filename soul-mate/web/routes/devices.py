"""Owner 手机快捷指令接入与隐私控制。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from core.device_context import CONSENT_KEYS, get_device_context_service


class DevicePush(BaseModel):
    device_token: str = Field(min_length=24, max_length=256)
    timestamp: str | None = Field(default=None, max_length=64)
    captured_at: str | None = Field(default=None, max_length=64)
    device: str | None = Field(default=None, max_length=80)
    battery: float | None = None
    charging: bool | None = None
    low_power: bool | None = None
    network: str | None = Field(default=None, max_length=32)
    focus_mode: str | None = Field(default=None, max_length=80)
    app_name: str | None = Field(default=None, max_length=120)
    app: str | None = Field(default=None, max_length=120)
    screen_state: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=80)
    location_accuracy_m: float | None = None
    health: dict[str, Any] = Field(default_factory=dict)
    steps: float | None = None
    active_energy_kcal: float | None = None
    sleep_minutes: float | None = None
    heart_rate_avg: float | None = None
    heart_rate_max: float | None = None


class DeviceCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    retention_days: int = Field(default=7, ge=1, le=90)
    consent: dict[str, bool] | None = None


class DeviceUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    retention_days: int | None = Field(default=None, ge=1, le=90)
    consent: dict[str, bool] | None = None

    @model_validator(mode="after")
    def validate_change(self) -> "DeviceUpdate":
        if all(value is None for value in (self.display_name, self.enabled, self.retention_days, self.consent)):
            raise ValueError("至少需要修改一项设备设置")
        if self.consent is not None and any(key not in CONSENT_KEYS for key in self.consent):
            raise ValueError("包含未知的数据授权项")
        return self


def create_devices_router(
    *,
    read_auth: Callable[..., str],
    write_auth: Callable[..., str],
    audit: Callable[..., None],
) -> APIRouter:
    router = APIRouter(tags=["owner-devices"])

    @router.post("/api/device/v1/push")
    def push(body: DevicePush) -> dict[str, Any]:
        payload = body.model_dump(exclude={"device_token"}, exclude_none=True)
        try:
            return get_device_context_service().ingest(token=body.device_token, payload=payload)
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    @router.get("/api/devices")
    def list_devices(owner: str = Depends(read_auth)) -> dict[str, Any]:
        del owner
        return get_device_context_service().status()

    @router.post("/api/devices", status_code=status.HTTP_201_CREATED)
    def create_device(body: DeviceCreate, request: Request, owner: str = Depends(write_auth)) -> dict[str, Any]:
        issued = get_device_context_service().create_device(
            display_name=body.display_name,
            consent=body.consent,
            retention_days=body.retention_days,
        )
        audit(
            request,
            actor=owner,
            action="device.create",
            scope_type="owner_device",
            outcome="success",
            resource_id=issued.device["id"],
            after={"display_name": issued.device["display_name"], "retention_days": issued.device["retention_days"]},
        )
        return {**issued.device, "device_token": issued.token}

    @router.patch("/api/devices/{device_id}")
    def update_device(
        device_id: str,
        body: DeviceUpdate,
        request: Request,
        owner: str = Depends(write_auth),
    ) -> dict[str, Any]:
        try:
            updated = get_device_context_service().update_device(
                device_id,
                display_name=body.display_name,
                enabled=body.enabled,
                consent=body.consent,
                retention_days=body.retention_days,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="device.update",
            scope_type="owner_device",
            outcome="success",
            resource_id=device_id,
            after={"enabled": updated["enabled"], "consent": updated["consent"], "retention_days": updated["retention_days"]},
        )
        return updated

    @router.post("/api/devices/{device_id}/rotate-token")
    def rotate_device_token(device_id: str, request: Request, owner: str = Depends(write_auth)) -> dict[str, str]:
        try:
            token = get_device_context_service().rotate_token(device_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        audit(
            request,
            actor=owner,
            action="device.token.rotate",
            scope_type="owner_device",
            outcome="success",
            resource_id=device_id,
        )
        return {"device_token": token}

    return router
