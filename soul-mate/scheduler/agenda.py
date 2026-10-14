"""每日议程生成 — 每天早 7 点由 APScheduler 调用"""
import json
import logging
from datetime import datetime

from memory import manager
from memory.models import DailyAgenda
from api import llm_client
from core.inner_world import describe_mood
from core.character_context import CharacterContext, character_scope
from memory.interaction_service import get_legacy_inner_state
from persona.dossier import get_active_snapshot


logger = logging.getLogger(__name__)


_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


async def generate_daily_agenda(context: CharacterContext) -> None:
    with character_scope(context):
        await _generate_daily_agenda(context)


async def _generate_daily_agenda(context: CharacterContext) -> None:
    snapshot = get_active_snapshot()
    profile = snapshot.get("stable_profile") if snapshot else None
    if not snapshot or not snapshot.get("is_active") or not isinstance(profile, dict):
        logger.warning("缺少活跃人格快照，跳过议程生成 character=%s", context.character_id)
        return

    # 已生成过就跳过（重启时保护）
    if await manager.get_today_agenda():
        return

    state = await get_legacy_inner_state(context)

    name = str(profile.get("name") or context.display_name)
    mood_str = describe_mood(state)
    weekday  = _WEEKDAYS[datetime.now().weekday()]

    raw = await llm_client.lightweight_chat(f"""
你是 {name}，今天{weekday}，当前状态：{mood_str}。

为今天生成一份自然、真实的日常安排。
输出 JSON（字段均为字符串或字符串列表）：
{{
  "morning":        "上午做什么（一句话）",
  "afternoon":      "下午做什么（一句话）",
  "evening":        "晚上做什么（一句话）",
  "mood_note":      "今天整体心情基调（一句话）",
  "possible_share": ["可能想分享的事1", "可能想分享的事2"]
}}

要求：生活化、有点懒散随意，不要写成完美规划。
""", max_tokens=400)

    try:
        data = json.loads(raw)
    except Exception:
        data = {
            "morning":  "睡懒觉",
            "afternoon": "随便逛逛",
            "evening":  "刷手机",
            "mood_note": "今天也没什么特别的",
            "possible_share":  [],
        }

    agenda = DailyAgenda(
        date=datetime.now().strftime("%Y-%m-%d"),
        morning=data.get("morning",        ""),
        afternoon=data.get("afternoon",    ""),
        evening=data.get("evening",        ""),
        mood_note=data.get("mood_note",    ""),
        possible_share=data.get("possible_share",  []),
    )

    await manager.save_daily_agenda(agenda)
    logger.info("每日议程已生成 date=%s mood=%s", agenda.date, agenda.mood_note)
