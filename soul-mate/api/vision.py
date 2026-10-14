"""
视觉理解管道 — 将图片 URL 转换为文字描述，供 DeepSeek 生成回复时参考。

默认使用 Qwen3.7-Flash（阿里云百炼，接口兼容 OpenAI vision 格式）。
可通过 .env 替换为任意支持 vision 的 OpenAI-compatible 接口。

环境变量：
    VISION_API_KEY   — DashScope API key（或其他 vision 服务的 key）
    VISION_BASE_URL  — 默认 https://dashscope.aliyuncs.com/compatible-mode/v1
    VISION_MODEL     — 默认 qwen3.7-flash

若未配置 VISION_API_KEY，describe_image() 返回空字符串，
上层按纯文本消息处理（不崩溃，只是不识图）。
"""
import base64
import io
import logging
import os
from pathlib import Path
from typing import Optional
from openai import AsyncOpenAI
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError
from core.runtime_settings import runtime_value

_vision_client: Optional[AsyncOpenAI] = None
_vision_signature: tuple[str, str] | None = None
logger = logging.getLogger(__name__)


def _description_instruction() -> str:
    return (
        "请为后续微信对话准确理解这张图片。先判断图片类型和主要场景；"
        "如果是聊天截图、笔记、题目清单、课程表或其他文字密集图片，优先 OCR 标题、栏目和最关键文字，"
        "再概括它表达的数量、结构和用途。看不清的文字明确说看不清，不要把连续数字擅自猜成乐谱、密码或其他类别。"
        "只描述图片中能看到的内容，不评价人物，不编造出处；用自然简体中文，控制在 300 字以内。"
    )


def _prepare_inline_image(content: bytes, mime_type: str) -> tuple[bytes, str]:
    """放大小尺寸截图，帮助多模态模型读取微信压缩后的细字。"""
    try:
        with Image.open(io.BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source)
            width, height = image.size
            if width <= 0 or height <= 0:
                return content, mime_type
            scale = min(4.0, max(1.0, 720 / min(width, height)))
            if scale <= 1.0:
                return content, mime_type
            target = (max(1, round(width * scale)), max(1, round(height * scale)))
            image = image.resize(target, Image.Resampling.LANCZOS)
            image = image.filter(ImageFilter.UnsharpMask(radius=1.2, percent=130, threshold=2))
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            prepared = output.getvalue()
            # 避免极端图片因预处理反而突破接口大小限制。
            if len(prepared) <= 8 * 1024 * 1024:
                return prepared, "image/png"
    except (OSError, UnidentifiedImageError, ValueError):
        return content, mime_type
    return content, mime_type


def _get_client() -> Optional[AsyncOpenAI]:
    global _vision_client, _vision_signature
    api_key = runtime_value("VISION_API_KEY", "").strip() or os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        return None
    base_url = runtime_value(
        "VISION_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).strip()
    signature = (api_key, base_url)
    if _vision_client is None or _vision_signature != signature:
        _vision_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        _vision_signature = signature
    return _vision_client


async def _describe_source(image_url: str) -> str:
    """
    返回用于连续对话的简洁中文描述（≤300字）。
    失败或未配置时返回空字符串。

    调用方将返回值注入 prompt：
        f"[图片内容：{description}]"
    DeepSeek 不直接看图，靠这段描述理解图片再回复。
    """
    client = _get_client()
    if not client:
        return ""

    model = runtime_value("VISION_MODEL", "qwen3.7-flash").strip() or "qwen3.7-flash"
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    },
                    {
                        "type": "text",
                        "text": (
                            _description_instruction()
                        ),
                    },
                ],
            }],
            max_tokens=500,
        )
        content = resp.choices[0].message.content
        return str(content or "").strip()[:1200]
    except Exception as exc:
        logger.warning("视觉理解调用失败 type=%s", type(exc).__name__)
        return ""


async def describe_image(image_url: str) -> str:
    """理解远程图片；未配置或调用失败时返回空字符串。"""
    return await _describe_source(image_url)


async def describe_image_file(path: Path, mime_type: str) -> str:
    """把桥接保存的本地图片以内联 data URI 交给视觉模型。"""
    maximum = min(
        max(int(os.getenv("VISION_MAX_IMAGE_BYTES", 20 * 1024 * 1024)), 1),
        50 * 1024 * 1024,
    )
    try:
        if not path.is_file() or path.stat().st_size > maximum:
            return ""
        content = path.read_bytes()
    except OSError as exc:
        logger.warning("读取视觉附件失败 type=%s", type(exc).__name__)
        return ""
    normalized_mime = str(mime_type or "").strip().lower()
    if not normalized_mime.startswith("image/"):
        return ""
    content, normalized_mime = _prepare_inline_image(content, normalized_mime)
    encoded = base64.b64encode(content).decode("ascii")
    return await _describe_source(f"data:{normalized_mime};base64,{encoded}")
