"""后处理 — 拆条/延迟/清理AI痕迹/小游戏提取"""
import re
import uuid
import os
import random
from memory.models import InnerState

_MAX_SAFE_CHUNKS = 8

# 小游戏 HTML 文件存放目录（由 FastAPI StaticFiles 直接提供）
_GAMES_DIR = os.path.join(os.path.dirname(__file__), "..", "web", "static", "games")


def extract_game(raw: str) -> tuple[str, str | None]:
    """
    检测 LLM 输出中的 HTML 游戏代码块（```html ... ```）。
    若存在：保存为静态文件，返回 (去掉代码块的文本, 游戏 URL)。
    若不存在：返回 (原文, None)。

    游戏 URL 格式：http://{VPS_HOST}:{PORT}/static/games/{id}.html
    由调用方（engine）在 chunks 末尾追加一条消息。
    """
    pattern = re.compile(r"```html\s*(<!DOCTYPE html>.*?```)", re.DOTALL | re.IGNORECASE)
    m = pattern.search(raw)
    if not m:
        return raw, None

    html_code = m.group(1).rstrip("`").strip()
    clean_text = raw[:m.start()].strip()

    # 确保目录存在
    os.makedirs(_GAMES_DIR, exist_ok=True)
    game_id = str(uuid.uuid4())[:8]
    file_path = os.path.join(_GAMES_DIR, f"{game_id}.html")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html_code)

    host = os.getenv("VPS_HOST", "localhost")
    port = os.getenv("WEB_PORT", "4600")
    game_url = f"http://{host}:{port}/static/games/{game_id}.html"
    return clean_text, game_url


def humanize_reply(raw: str, state: InnerState) -> list[str]:
    """把 LLM 原始输出处理成像人类打字的多条消息"""
    # 小游戏提取：若有 HTML 代码块，存文件并追加链接
    raw, game_url = extract_game(raw)
    if game_url:
        raw = raw + f"\n给你，点开玩：{game_url}"

    text = raw.strip()

    # 清理 AI 痕迹
    text = re.sub(r'[（(][^）)]*?[笑哈嘿嗯]{1}[^）)]*?[)）]', '', text)
    text = re.sub(r'^(好的[，,]?|当然[，,]?|没问题[，,]?)', '', text)
    text = text.replace('~', '～')
    text = text.strip()

    # 拆条
    chunks = _smart_split(text)

    # 状态修正
    if state.energy < 3:
        chunks = _merge_short(chunks, max_chunks=2)

    return [c for c in chunks if c.strip()]


def _smart_split(text: str) -> list[str]:
    """按微信聊天习惯智能拆分"""
    explicit_parts = [part.strip() for part in re.split(r'\n+', text) if part.strip()]
    if len(explicit_parts) > 1:
        chunks = explicit_parts
        while len(chunks) > _MAX_SAFE_CHUNKS:
            chunks[-2] = chunks[-2] + ' ' + chunks[-1]
            chunks.pop()
        return chunks

    if len(text) < 25:
        return [text]

    chunks = []
    remaining = text

    # 优先拆分点：换行 > 问号 > 多感叹号 > 句号（仅长句）
    split_patterns = [
        r'\n+',
        r'([？?！!]{1,2})\s+(?=[^\s])',
        r'([。])\s+(?=[^\s])',
    ]

    for pattern in split_patterns:
        if len(remaining) <= 30:
            break
        parts = re.split(pattern, remaining, maxsplit=1)
        # re.split 带括号会保留分隔符，过滤掉单独的标点
        parts = [p for p in parts if p and not re.fullmatch(r'[？?！!。\s]+', p)]
        if len(parts) >= 2 and len(parts[0].strip()) >= 4:
            chunks.append(parts[0].strip())
            remaining = ''.join(parts[1:]).strip()

    if remaining:
        chunks.append(remaining.strip())

    if not chunks:
        chunks = [text]

    # 微信出站最多三条；合并而不是截断，避免丢失回复尾部。
    while len(chunks) > _MAX_SAFE_CHUNKS:
        # 合并最短的相邻两条
        min_i = min(range(len(chunks) - 1), key=lambda i: len(chunks[i]) + len(chunks[i+1]))
        chunks[min_i] = chunks[min_i] + ' ' + chunks[min_i + 1]
        chunks.pop(min_i + 1)

    return chunks


def _merge_short(chunks: list[str], max_chunks: int = 2) -> list[str]:
    """合并成少量消息（累了/懒得多说）"""
    while len(chunks) > max_chunks:
        chunks[0] = chunks[0] + ' ' + chunks[1]
        chunks.pop(1)
    return chunks


def calculate_chunk_delay(chunk: str, is_first: bool) -> float:
    """计算每条消息发送前的延迟（模拟打字时间）"""
    if is_first:
        return 0.0
    char_count = len(chunk)
    typing_time = char_count * random.uniform(0.05, 0.10)
    pause = random.uniform(0.8, 2.0)
    return min(typing_time + pause, 7.0)
