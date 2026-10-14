"""把 Owner 问卷原文加密导入版本化资料库；不会打印或复制原文。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.owner_profile import OwnerProfileStore, get_owner_profile_store


DERIVED_ENTRIES: tuple[dict[str, object], ...] = (
    {
        "profile_key": "fact.birth_date",
        "category": "explicit_fact",
        "title": "出生日期",
        "content": "2005-10-12",
        "source": "owner_confirmation_2026-08-08",
        "sensitivity": "private",
        "topic_tags": ["生日", "年龄", "出生"],
    },
    {
        "profile_key": "current.exam.2026-12-19",
        "category": "explicit_fact",
        "title": "当前考研安排",
        "content": "本科在读并准备考研，考试日期是 2026-12-19。具体进度会变化，应以 Owner 后续更新为准。",
        "source": "owner_questionnaire_confirmed",
        "sensitivity": "private",
        "topic_tags": ["考研", "复习", "考试", "学习"],
        "valid_until": "2026-12-20T23:59:59+08:00",
    },
    {
        "profile_key": "relationship.current_partner_status",
        "category": "explicit_fact",
        "title": "现实关系状态",
        "content": "当前有女朋友；这是会变化的实时事实，只有 Owner 的明确更新才能改变。",
        "source": "owner_confirmation_2026-08-08",
        "sensitivity": "private",
        "topic_tags": ["女朋友", "对象", "恋爱", "分手"],
    },
    {
        "profile_key": "support.adaptive_sequence",
        "category": "conditional_rule",
        "title": "陪伴与建议的切换顺序",
        "content": "情绪强烈时先安慰和陪伴，平稳后再分析；Owner 明确说只听我说、直接一点或帮我分析时，按当轮明确要求切换。",
        "source": "owner_questionnaire_confirmed",
        "sensitivity": "private",
        "topic_tags": ["难受", "没事", "算了", "安慰", "分析", "建议", "只听我说"],
    },
    {
        "profile_key": "support.advice_shape",
        "category": "conditional_rule",
        "title": "实质建议的理想形式",
        "content": "需要建议时先问少量关键信息，再给选择、利弊、明确推荐和可以立刻开始的第一步；避免说教、指责和父母式打卡。",
        "source": "owner_questionnaire_confirmed",
        "sensitivity": "private",
        "topic_tags": ["怎么办", "建议", "分析", "选择", "计划"],
    },
    {
        "profile_key": "values.multi_objective",
        "category": "stable_preference",
        "title": "重要价值不是单一排序",
        "content": "自由与选择、爱与连接、体验、成长和外界认可都重要，会按场景权衡，不应强行排成永久不变的唯一顺序。",
        "source": "owner_questionnaire_confirmed",
        "sensitivity": "private",
        "topic_tags": ["自由", "选择", "爱", "成长", "认可", "意义"],
    },
    {
        "profile_key": "relationship.partner_preferences",
        "category": "stable_preference",
        "title": "长期伴侣偏好",
        "content": "现实吸引包含外貌、气质和审美入口；长期关系还重视成熟、理解、智慧、能力、温柔、生活适配、相互欣赏与平等。希望照顾与被照顾、需要与被需要能自然切换。",
        "source": "owner_questionnaire_confirmed",
        "sensitivity": "private",
        "topic_tags": ["喜欢", "伴侣", "理想型", "外貌", "相处", "恋爱"],
    },
    {
        "profile_key": "emotion.death_anxiety",
        "category": "sensitive_self_report",
        "title": "对死亡的具体恐惧",
        "content": "主要担心意识永久终止、不可逆、未知和失去未来体验。人生有意义或幸福不会自动消除这种恐惧，有时会更舍不得结束。",
        "source": "owner_questionnaire_confirmed",
        "sensitivity": "high",
        "topic_tags": ["死亡", "意识", "永远", "意义", "害怕"],
    },
    {
        "profile_key": "identity.body_and_intimacy_exploration",
        "category": "sensitive_self_report",
        "title": "身体表达、性别角色压力与亲密探索",
        "content": "完整具体自述只保存在同来源的加密问卷原文中。它同时涉及审美、自我表达、社会角色压力、身体体验、被欣赏和性好奇；不能据此擅自指定性取向或性别认同。",
        "source": "owner_questionnaire_confirmed_sensitive",
        "sensitivity": "high",
        "topic_tags": ["女装", "身体", "性别", "性取向", "亲密", "探索"],
    },
    {
        "profile_key": "inference.rejected_overconfident_labels",
        "category": "rejected_inference",
        "title": "已经否定的过度推断",
        "content": "不得重新引入：把死亡焦虑归因为人生无意义；把提升自己后再追求喜欢的人一律解释为逃避；认定理想伴侣必须年长；使用焦虑型依恋、行动系统受损、高潜力型等诊断式标签；把学业变化当成身体或亲密探索的单一原因；把多选理解成所有需求必须同时满足。",
        "source": "owner_correction_2026-08-09",
        "sensitivity": "private",
        "topic_tags": ["分析我", "推断", "依恋", "逃避", "死亡", "选择"],
    },
)


def import_questionnaire(source_path: str | Path, *, store: OwnerProfileStore | None = None) -> dict[str, object]:
    target = Path(source_path).expanduser().resolve()
    raw = target.read_text(encoding="utf-8")
    if not raw.strip():
        raise ValueError("问卷原文为空")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    profile_store = store or get_owner_profile_store()
    raw_entry = profile_store.upsert_entry(
        profile_key="questionnaire.raw.2026-08-09",
        category="sensitive_self_report",
        title="完整问卷逐字原文",
        content="原始回答与后续分析分开保存；原文不能被提炼结论覆盖或改写。",
        raw_quote=raw,
        source=f"attachment_sha256:{digest}",
        sensitivity="high",
        topic_tags=["问卷", "原始回答", "自述"],
        actor="owner",
    )
    entries = [profile_store.upsert_entry(actor="owner", **item) for item in DERIVED_ENTRIES]
    by_key = {str(item["profile_key"]): item for item in entries}
    for character_id in ("default", "ji-yu"):
        for key in ("current.exam.2026-12-19", "relationship.current_partner_status"):
            profile_store.set_disclosure(
                character_id=character_id,
                entry_id=str(by_key[key]["id"]),
                state="confirmed_known",
                evidence="人物开局已确认知道该项基本事实，后续变化只以 Owner 更新为准",
                actor="owner",
            )
    goal = profile_store.upsert_goal(
        title="准备 2026 年考研",
        detail="考试日期 2026-12-19；进度与具体复习安排由 Owner 持续更新。",
        due_at="2026-12-19T00:00:00+08:00",
        next_step="等待 Owner 在管理台补充当前复习阶段与下一项可执行任务",
    )
    return {
        "raw_entry_id": raw_entry["id"],
        "raw_sha256": digest,
        "raw_redacted": raw_entry["redacted"],
        "derived_entry_count": len(entries),
        "goal_id": goal["id"],
    }


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="加密导入 Owner 问卷")
    parser.add_argument("source", help="UTF-8 问卷原文路径")
    args = parser.parse_args()
    result = import_questionnaire(args.source)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
