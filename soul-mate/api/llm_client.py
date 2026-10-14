"""DeepSeek 文本生成与本地 BGE-M3 Embedding。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import threading
from pathlib import Path
from typing import Any, Optional

from openai import AsyncOpenAI
from core.runtime_settings import runtime_value


logger = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None
_client_signature: tuple[str, str] | None = None
_semantic_model: Any = None
_semantic_model_lock = threading.Lock()
_embedding_health: dict[str, str] = {
    "provider": "not-loaded",
    "model": os.getenv("LOCAL_EMBEDDING_MODEL", "BAAI/bge-m3"),
    "error": "",
}


def get_client() -> AsyncOpenAI:
    global _client, _client_signature
    api_key = runtime_value("DEEPSEEK_API_KEY", "").strip()
    base_url = runtime_value("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip()
    signature = (api_key, base_url)
    if _client is None or _client_signature != signature:
        _client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        _client_signature = signature
    return _client


def _model_for(role: str) -> str:
    shared = runtime_value("DEEPSEEK_MODEL", "deepseek-v4-pro").strip() or "deepseek-v4-pro"
    if role == "planner":
        return runtime_value("DEEPSEEK_PLANNER_MODEL", "").strip() or shared
    if role == "renderer":
        return runtime_value("DEEPSEEK_RENDERER_MODEL", "").strip() or shared
    return shared


async def _completion(
    messages: list[dict],
    *,
    role: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    require_complete: bool = False,
) -> str:
    kwargs: dict[str, Any] = {
        "model": _model_for(role),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    last_error: BaseException | None = None
    for attempt in range(1, 4):
        try:
            response = await get_client().chat.completions.create(**kwargs)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "模型请求失败 role=%s attempt=%s type=%s",
                role,
                attempt,
                type(exc).__name__,
            )
            if attempt < 3:
                await asyncio.sleep(attempt)
                continue
            raise
        raw = response.choices[0].message.content or ""
        finish_reason = response.choices[0].finish_reason
        logger.info(
            "模型请求完成 role=%s attempt=%s finish=%s chars=%s",
            role,
            attempt,
            finish_reason,
            len(raw),
        )
        if require_complete and finish_reason != "stop":
            last_error = RuntimeError(f"模型输出未完整结束：{finish_reason or 'unknown'}")
            if attempt < 3:
                await asyncio.sleep(attempt)
                continue
            raise last_error
        if raw.strip():
            return raw.strip()
        last_error = RuntimeError("模型返回空内容")
        if attempt < 3:
            await asyncio.sleep(attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError("模型调用失败")


async def structured_chat(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int = 700,
) -> dict[str, Any]:
    """低随机度隐藏决策，只返回 JSON 对象。"""
    current_messages = [dict(item) for item in messages]
    last_error: ValueError | None = None
    for parse_attempt in range(1, 3):
        raw = await _completion(
            current_messages,
            role="planner",
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("隐藏决策器返回值必须是对象")
            return parsed
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = ValueError("隐藏决策器返回了无效 JSON")
            logger.warning("隐藏决策 JSON 解析失败 parse_attempt=%s", parse_attempt)
            if parse_attempt >= 2:
                break
            retry_note = (
                "上一次输出无法解析。只返回一个语法严格的 JSON 对象："
                "所有属性名和字符串必须使用双引号，不得出现注释、尾随逗号、Markdown 或 JSON 之外的文字。"
            )
            if current_messages and current_messages[0].get("role") == "system":
                current_messages[0] = {
                    **current_messages[0],
                    "content": f"{current_messages[0].get('content', '')}\n\n## JSON 格式重试\n{retry_note}",
                }
            else:
                current_messages.insert(
                    0,
                    {"role": "system", "content": f"## JSON 格式重试\n{retry_note}"},
                )
    if last_error is not None:
        raise last_error
    raise ValueError("隐藏决策器返回了无效 JSON")


async def text_chat(
    messages: list[dict],
    *,
    temperature: float = 0.85,
    max_tokens: int = 1000,
) -> str:
    """自然语言渲染器，只返回可发送的纯文本。"""
    return await _completion(
        messages,
        role="renderer",
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=False,
    )


async def chat(messages: list[dict], expect_json: bool = False) -> dict:
    """旧调用兼容层；新对话核心应分别调用 structured_chat/text_chat。"""
    if expect_json:
        parsed = await structured_chat(messages)
        reply = str(parsed.get("reply") or "").strip()
        return {"reply": reply, "remember": parsed.get("remember", [])}
    return {"reply": await text_chat(messages), "remember": []}


def _semantic_local_embedding(text: str) -> list[float]:
    global _semantic_model
    if _semantic_model is None:
        with _semantic_model_lock:
            if _semantic_model is None:
                from sentence_transformers import SentenceTransformer

                model_name = os.getenv("LOCAL_EMBEDDING_MODEL", "BAAI/bge-m3").strip() or "BAAI/bge-m3"
                cache_dir = os.getenv("LOCAL_EMBEDDING_CACHE", "").strip() or None
                _semantic_model = SentenceTransformer(model_name, cache_folder=cache_dir)
    encoded = _semantic_model.encode(
        [text or ""],
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )[0]
    return [float(value) for value in encoded.tolist()]


async def embed(text: str) -> list[float]:
    """只在本机计算向量；本地模型不可用时显式降级，不调用远程 Embedding。"""
    global _embedding_health
    model_name = os.getenv("LOCAL_EMBEDDING_MODEL", "BAAI/bge-m3").strip() or "BAAI/bge-m3"
    try:
        vector = await asyncio.to_thread(_semantic_local_embedding, text)
    except Exception as exc:
        summary = f"{type(exc).__name__}: {str(exc)[:240]}"
        _embedding_health = {
            "provider": "hash-fallback",
            "model": model_name,
            "error": summary,
        }
        logger.warning("本地向量模型不可用，使用哈希降级 type=%s", type(exc).__name__)
        return _local_embedding(text)
    _embedding_health = {
        "provider": "sentence-transformers",
        "model": model_name,
        "error": "",
    }
    return vector


def embedding_health() -> dict[str, str]:
    return dict(_embedding_health)


def _local_embedding(text: str, dim: int | None = None) -> list[float]:
    """确定性字符 n-gram 降级向量；维度与 BGE-M3 dense 输出保持一致。"""
    dimension = dim or max(64, int(os.getenv("LOCAL_EMBEDDING_DIM", "1024")))
    vector = [0.0] * dimension
    lowered = (text or "").lower()
    tokens: list[str] = []
    for size in (1, 2, 3, 4):
        for index in range(len(lowered) - size + 1):
            tokens.append(lowered[index:index + size])
    if not tokens:
        tokens = ["<empty>"]
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "little") % dimension
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


async def lightweight_chat(
    prompt: str,
    max_tokens: int = 300,
    *,
    require_complete: bool = False,
) -> str:
    """后台结构化小任务；复用 planner 模型但不写入提示词或原始输出日志。"""
    return await _completion(
        [{"role": "user", "content": prompt}],
        role="planner",
        temperature=0.6,
        max_tokens=max_tokens,
        json_mode=True,
        require_complete=require_complete,
    )
