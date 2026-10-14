"""宿主机只读社交浏览器代理：鉴权后仅转发明确允许的读取动作。"""
from __future__ import annotations

import hmac
import os
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, model_validator


class ReadAction(BaseModel):
    action: Literal["feed", "search", "profile", "read"]
    n: int = Field(default=10, ge=1, le=30)
    query: str | None = Field(default=None, max_length=100)
    user: str | None = Field(default=None, max_length=100)
    url: str | None = Field(default=None, max_length=2000)
    latest: bool | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> "ReadAction":
        if self.action == "search" and not self.query:
            raise ValueError("搜索必须提供关键词")
        if self.action == "read" and not self.url:
            raise ValueError("读取详情必须提供链接")
        return self


def _authorize(header: str | None) -> None:
    expected = os.getenv("SOCIAL_BROWSER_PROXY_TOKEN", "").strip()
    provided = str(header or "")
    if not expected or not provided.startswith("Bearer ") or not hmac.compare_digest(provided[7:], expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="社交浏览器代理鉴权失败")


def _upstream(platform: str) -> str:
    default = "http://127.0.0.1:8272/twitter" if platform == "twitter" else "http://127.0.0.1:8273/xiaohongshu"
    key = "SOCIAL_BROWSER_X_UPSTREAM" if platform == "twitter" else "SOCIAL_BROWSER_XHS_UPSTREAM"
    value = os.getenv(key, default).strip()
    if not value.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="上游必须只监听宿主机回环地址")
    return value


app = FastAPI(title="Soul Mate Social Browser Read-only Proxy", docs_url=None, redoc_url=None)


@app.get("/healthz")
def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _authorize(authorization)
    return {"status": "ok", "mode": "read_only"}


async def _forward(platform: str, body: ReadAction, authorization: str | None) -> Any:
    _authorize(authorization)
    if platform == "twitter" and body.action == "search":
        payload = body.model_dump(exclude_none=True)
        payload["action"] = "feed"
    elif platform == "xiaohongshu" and body.action not in {"feed", "search", "profile", "read"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不支持的读取动作")
    else:
        payload = body.model_dump(exclude_none=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=5.0), follow_redirects=False) as client:
        response = await client.post(_upstream(platform), json=payload)
    try:
        content = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="社交浏览器返回了无效内容") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="社交浏览器读取失败")
    return content


@app.post("/twitter")
async def twitter(body: ReadAction, authorization: str | None = Header(default=None)) -> Any:
    return await _forward("twitter", body, authorization)


@app.post("/xiaohongshu")
async def xiaohongshu(body: ReadAction, authorization: str | None = Header(default=None)) -> Any:
    return await _forward("xiaohongshu", body, authorization)
