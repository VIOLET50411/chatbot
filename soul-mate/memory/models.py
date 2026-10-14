"""数据模型定义"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class MemoryType(str, Enum):
    IDENTITY   = "identity"      # L5 身份档案
    PREFERENCE = "preference"    # L4 偏好
    RELATION   = "relationship"  # L3 关系图谱
    EVENT      = "event"         # L2 事件
    EMOTION    = "emotion"       # L1 情绪印象


class RelationshipStage(str, Enum):
    STRANGER  = "初识"
    PROBING   = "试探"
    FAMILIAR  = "熟悉朋友"
    CLOSE     = "亲密好友"
    AMBIGUOUS = "暧昧"
    LOVER     = "恋人"


@dataclass
class Memory:
    id: str
    type: MemoryType
    content: str
    importance: int          # 1-5
    initial_strength: float  # 创建时的强度，通常为 1.0
    recall_count: int = 0    # 被检索次数（用于加固计算）
    created_at: datetime = field(default_factory=datetime.now)
    source: str = "conversation"  # conversation / corpus / manual
    # Russell 情感坐标
    valence: float = 0.0     # 情感效价 -1(负面) ~ 1(正面)
    arousal: float = 0.5     # 情绪强度 0(平静) ~ 1(激烈)
    # 记忆管理标记
    pinned: bool = False     # True → 永不衰减，importance 锁定最高
    resolved: bool = False   # True → 已解决，降权至 5%，沉底等唤醒
    character_id: str = ""  # 向量元数据中的第二层角色边界
    fact_id: str = ""       # 角色工作空间内的可审计事实主键
    source_trace_id: str = ""  # 可追溯的事实或管理操作标识
    status: str = "published"
    created_by: str = "legacy"
    index_token: str = ""  # 仅索引阶段使用的临时凭据，不能作为持久化向量元数据


@dataclass(slots=True)
class InnerState:
    # 情绪面板（0-10）
    happiness:    float = 6.0
    energy:       float = 7.0
    irritability: float = 2.0
    affection:    float = 5.0
    loneliness:   float = 3.0

    # 关系状态
    stage:     RelationshipStage = RelationshipStage.STRANGER
    trust:     float = 20.0
    intimacy:  float = 10.0
    resentment: float = 0.0  # 积怨

    # 当前公开活动仅用于回复节奏与语境，不承载完整日程或隐藏内容。
    current_activity: str = "没什么特别的"

    # 关系上下文标签（用于判断亲密度变化的语境，驱动次日回落逻辑）
    # "late_night" | "shared_interest" | "deep_talk" | "normal"
    intimacy_context: str = "normal"

    # 元数据
    last_chat_time: Optional[datetime] = None
    last_tick_time: Optional[datetime] = None


@dataclass(slots=True)
class DailyAgenda:
    date: str               # YYYY-MM-DD
    morning: str
    afternoon: str
    evening: str
    mood_note: str
    possible_share: list  # 可供主动意图引用的公开分享事项
