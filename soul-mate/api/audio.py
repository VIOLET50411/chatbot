"""音频转写管道。

使用 OpenAI-compatible `/audio/transcriptions` 接口。未配置时返回空字符串，
上层按未识别语音处理，不崩溃。
"""

from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI
from core.runtime_settings import runtime_value

logger = logging.getLogger(__name__)
_audio_client: Optional[AsyncOpenAI] = None
_audio_signature: tuple[str, str] | None = None


def audio_configured() -> bool:
    return bool(
        runtime_value("AUDIO_API_KEY", "").strip()
        or runtime_value("VISION_API_KEY", "").strip()
    )


def _get_client() -> Optional[AsyncOpenAI]:
    global _audio_client, _audio_signature
    api_key = (
        runtime_value("AUDIO_API_KEY", "").strip()
        or runtime_value("VISION_API_KEY", "").strip()
    )
    if not api_key:
        return None
    base_url = runtime_value(
        "AUDIO_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).strip()
    signature = (api_key, base_url)
    if _audio_client is None or _audio_signature != signature:
        _audio_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        _audio_signature = signature
    return _audio_client


async def transcribe_audio(audio_path: Path) -> str:
    """返回语音文本；未配置或失败时返回空字符串。"""
    client = _get_client()
    if client is None:
        return ""
    model = runtime_value("AUDIO_MODEL", "qwen3-asr-flash").strip() or "qwen3-asr-flash"
    try:
        with audio_path.open("rb") as audio_file:
            response = await client.audio.transcriptions.create(
                model=model,
                file=audio_file,
            )
        return (response.text or "").strip()
    except Exception as exc:
        logger.warning("音频转写失败 type=%s", type(exc).__name__)
        return ""
