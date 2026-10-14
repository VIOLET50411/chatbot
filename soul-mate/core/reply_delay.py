"""
回复时机计算 — 模拟真人延迟行为

决策层（decision.py）决定"回不回"，本模块决定"什么时候回"。

延迟由三个因素加权：
  1. 亲密度（intimacy 0-100）：决定基础延迟区间，越亲密区间越短
  2. 忙碌程度（current_activity 关键词匹配）：加大延迟乘数
  3. 时间段（凌晨 / 早晨）：额外加乘

亲密度对忙碌乘数有抑制效果：越亲密越愿意挤时间回，忙碌带来的
额外延迟会按比例折减；≥75 时还有硬上限（再忙不超过10分钟）。
"""
import os
import random
import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memory.models import InnerState


# ── 亲密度 → 基础延迟区间（秒）──────────────────────────────────────────
# 聚合已经等待 4–8 秒，这里只增加微信端可感知的自然回复节奏，统一限制在 3–90 秒。
_BASE_BY_INTIMACY: list[tuple[float, tuple[int, int]]] = [
    (80, (3, 18)),
    (60, (5, 30)),
    (40, (8, 45)),
    (20, (12, 65)),
    (0, (18, 90)),
]

# ── 活动关键词 → 忙碌乘数 ────────────────────────────────────────────────
_BUSY_MULTIPLIERS: list[tuple[float, list[str]]] = [
    (3.5, ["上课中", "考试中", "面试中"]),
    (2.5, ["上课", "考试", "面试", "开会", "实验课"]),
    (2.0, ["写作业", "复习", "上班", "加班", "睡觉", "睡了", "睡着"]),
    (1.5, ["吃饭", "健身", "运动", "洗澡", "出门", "逛街", "上课"]),
    (0.6, ["没什么", "闲着", "刷手机", "摸鱼", "躺着", "在宿舍", "休息"]),
]


def _busyness_mult(activity: str) -> float:
    if not activity:
        return 1.0
    for mult, keywords in _BUSY_MULTIPLIERS:
        if any(kw in activity for kw in keywords):
            return mult
    return 1.0


def _time_mult(hour: int) -> float:
    """深夜/凌晨回复概率低，早晨稍慢。"""
    if 0 <= hour < 7:
        return 2.5
    if 7 <= hour < 9:
        return 1.3
    return 1.0


def calculate_reply_delay(
    state: "InnerState",
    _message: str = "",
    *,
    context=None,
) -> int:
    """
    返回应延迟的秒数。0 = 立即回复（< 30s 视为立即，不值得调度）。

    调用时机：engine 已完成 LLM 生成、后处理完毕，即将发送之前。
    """
    intimacy: float = getattr(state, "intimacy", 10.0)
    activity: str   = getattr(state, "current_activity", "") or ""
    hour = datetime.now().hour
    message = str(_message or "").strip()

    # 1. 基础区间
    base_lo, base_hi = next(
        (lo_hi for threshold, lo_hi in _BASE_BY_INTIMACY if intimacy >= threshold),
        (18, 90),
    )
    base = random.uniform(base_lo, base_hi)

    # 2. 忙碌乘数（亲密度高时折减忙碌影响）
    raw_busy = _busyness_mult(activity)
    if intimacy >= 70:
        # 很亲密：忙碌影响只保留 30%
        busy_mult = 1.0 + (raw_busy - 1.0) * 0.3
    elif intimacy >= 50:
        # 较亲密：保留 60%
        busy_mult = 1.0 + (raw_busy - 1.0) * 0.6
    else:
        busy_mult = raw_busy

    # 3. 时间乘数
    time_mult = _time_mult(hour)

    delay = base * busy_mult * time_mult

    # 角色级配置优先；未配置时读取全局环境变量，避免初识阶段拖太久。
    configured = getattr(context, "reply_delay_max_seconds", None)
    if configured is None:
        try:
            configured = int(os.getenv("SOUL_MATE_MAX_REPLY_DELAY_SECONDS", "120"))
        except ValueError:
            configured = 120
    max_delay = min(90, max(0, int(configured)))
    if max_delay == 0:
        return 0
    delay = min(delay, max_delay)

    # 确认是否在线的短问候，不应在模型已经生成后再额外等待近一分钟。
    compact_ping = re.sub(r"[\s？?。！!]", "", message)
    if compact_ping in {"在吗", "有空吗", "睡了吗", "忙吗", "方便吗"}:
        delay = min(delay, 18)
    elif compact_ping and len(compact_ping) <= 4:
        delay = min(delay, 30)

    return max(3, int(delay))
