"""
打字节奏模拟 — 控制"对方正在输入..."气泡的时机和时长

职责：根据消息内容、情绪状态、亲密度，生成每条 chunk 发送前的打字动作序列。
Plus 薄代理依据序列控制分块间隔；本模块只做计算，不接触微信或网络。

人类打字行为模型：
  ┌─ 简单/确定  ──→ 一段连续打字 → 发出
  ├─ 纠结/情感  ──→ 打一段 → 消失（删了）→ 停顿 → 再打 → 发出
  ├─ 长消息     ──→ 打一段 → 短停顿 → 继续打 → 发出
  └─ 快回       ──→ 极短打字 or 不显示（< 8字的肯定回复）

额外影响因子（超出用户描述，自动补充）：
  - 深夜：打字变慢（疲惫）
  - 多条 chunk：后续条比第一条快（对话已在流动）
  - 低能量 / 高烦躁：打字慢，停顿多
  - 高亲密度：不需要字斟句酌，速度更快
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memory.models import InnerState


@dataclass
class TypingStep:
    action: str      # "typing" | "pause"
    duration: float  # 秒


# ─── 纠结检测 ─────────────────────────────────────────────────────────────────

# 打了又删的信号词：出现 ≥ 2 个则判定为"纠结"
_HESITANT_SIGNALS = [
    "其实", "说实话", "不知道怎么", "怎么说呢", "emmm",
    "但是", "不过", "只是", "有点", "可能", "感觉",
    "也许", "算了", "不是", "有些", "有时候",
]

# 情感敏感词：出现 ≥ 1 个就让第一条打字带点犹豫
_EMOTIONAL_SIGNALS = [
    "喜欢", "讨厌", "难过", "委屈", "心疼", "生气",
    "害怕", "想你", "在意", "失望", "开心",
]


def _hesitancy_level(text: str) -> str:
    """返回 'none' | 'mild' | 'strong'"""
    hesitant_count = sum(1 for w in _HESITANT_SIGNALS if w in text)
    emotional_count = sum(1 for w in _EMOTIONAL_SIGNALS if w in text)
    if hesitant_count >= 2:
        return "strong"
    if hesitant_count == 1 or emotional_count >= 1:
        return "mild"
    return "none"


# ─── 状态修正因子 ──────────────────────────────────────────────────────────────

def _speed_factor(state: InnerState) -> float:
    """综合亲密度、精力、时间段计算打字速度系数（越小越快）"""
    intimacy  = getattr(state, "intimacy",    10.0)
    energy    = getattr(state, "energy",       7.0)
    irritability = getattr(state, "irritability", 2.0)
    hour = datetime.now().hour

    # 亲密度：越高越不字斟句酌
    if intimacy >= 75:
        factor = 0.55
    elif intimacy >= 55:
        factor = 0.70
    elif intimacy >= 35:
        factor = 0.85
    else:
        factor = 1.0

    # 精力低：打字慢
    if energy < 3:
        factor *= 1.35
    elif energy < 5:
        factor *= 1.15

    # 烦躁高：打字草率（反而快一点，短促）
    if irritability > 6:
        factor *= 0.80

    # 深夜 (23–7 点)：疲惫，稍慢
    if 0 <= hour < 7 or hour >= 23:
        factor *= 1.25

    return factor


# ─── 单条 chunk 的打字序列 ─────────────────────────────────────────────────────

def _sequence_for_chunk(
    chunk: str,
    hesitancy: str,
    speed: float,
    is_first: bool,
) -> list[TypingStep]:
    char_count = len(chunk)

    # 极短确定性回复（< 8字）：几乎不显示打字中
    if char_count < 8 and hesitancy == "none":
        dur = random.uniform(0.3, 0.8) * speed
        return [TypingStep("typing", dur)]

    # 基础打字时长（每字 0.10 ~ 0.16 秒）
    base = char_count * random.uniform(0.10, 0.16) * speed

    # 非第一条消息已在对话流中，速度快 15%
    if not is_first:
        base *= 0.85

    if hesitancy == "strong":
        # 打了一部分 → 删掉 → 停顿 → 重打
        burst1 = base * random.uniform(0.35, 0.55)
        pause  = random.uniform(1.8, 3.5) * speed
        burst2 = base * random.uniform(0.60, 0.90)
        return [
            TypingStep("typing", burst1),
            TypingStep("pause",  pause),
            TypingStep("typing", burst2),
        ]
    elif hesitancy == "mild":
        # 打到一半短暂停一下
        burst1 = base * random.uniform(0.50, 0.65)
        pause  = random.uniform(0.8, 1.8) * speed
        burst2 = base * random.uniform(0.40, 0.60)
        return [
            TypingStep("typing", burst1),
            TypingStep("pause",  pause),
            TypingStep("typing", burst2),
        ]
    elif char_count > 50:
        # 长消息中途小停
        burst1 = base * 0.60
        pause  = random.uniform(0.4, 0.9)
        burst2 = base * 0.40
        return [
            TypingStep("typing", burst1),
            TypingStep("pause",  pause),
            TypingStep("typing", burst2),
        ]
    else:
        return [TypingStep("typing", base)]


# ─── 公开接口 ──────────────────────────────────────────────────────────────────

def calculate_typing_sequence(
    chunks: list[str],
    state: "InnerState",
) -> list[list[TypingStep]]:
    """
    返回 list[list[TypingStep]]，对应每条 chunk 发送前需要执行的打字序列。

    调用方（sender.send_chunks）依次：
      1. 等 delays[i]（chunk 间隔）
      2. 遍历 typing_seqs[i]，executing each TypingStep
         - "typing" → 调 send_typing_notice(wxid)，sleep duration
         - "pause"  → sleep duration（不发 typing，气泡消失）
      3. send_text(wxid, chunk)
    """
    all_text  = " ".join(chunks)
    hesitancy = _hesitancy_level(all_text)
    speed     = _speed_factor(state)

    return [
        _sequence_for_chunk(chunk, hesitancy, speed, i == 0)
        for i, chunk in enumerate(chunks)
    ]
