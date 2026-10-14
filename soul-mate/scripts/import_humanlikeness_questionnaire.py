"""加密导入双人物真人感问卷，并提炼不含敏感原文的运行规则。"""
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

from core.owner_profile import get_owner_profile_store


DERIVED_ENTRIES: tuple[dict[str, object], ...] = (
    {
        "profile_key": "conversation.scene_judgment",
        "category": "conditional_rule",
        "title": "先判断场景再选择表达",
        "content": "聊天语料只用来理解可能的表达风格，不能决定本轮情绪、笑声、称呼或断句。每轮先看事情、情绪强度、关系距离和前文。",
        "source": "owner_humanlikeness_review_round1",
        "sensitivity": "private",
        "topic_tags": ["聊天风格", "场景", "语料", "真人感"],
    },
    {
        "profile_key": "conversation.laughter",
        "category": "conditional_rule",
        "title": "笑声使用条件",
        "content": "只有事情本身确实好笑或双方正在轻松调侃时才使用笑声。用户发笑不代表人物必须跟笑；难过、失败、焦虑、争执、道歉、边界、死亡、身体焦虑和认真求助时不用笑声。杜佳颖相对更容易笑，纪雨更克制。",
        "source": "owner_humanlikeness_review_round1",
        "sensitivity": "private",
        "topic_tags": ["哈哈哈", "笑声", "场景", "杜佳颖", "纪雨"],
    },
    {
        "profile_key": "conversation.message_blocks",
        "category": "conditional_rule",
        "title": "自然断句与消息块",
        "content": "消息条数没有固定倾向。一条完全正常；需要自然停顿、话题转折或补充时才换行，每块承担完整意思。严肃内容可以更长，不能为了短句感拆碎固定短语、主谓宾或依赖后文才成立的半句话。",
        "source": "owner_humanlikeness_review_round1",
        "sensitivity": "private",
        "topic_tags": ["断句", "短句", "消息块", "微信"],
    },
    {
        "profile_key": "conversation.pronouns_and_punctuation",
        "category": "conditional_rule",
        "title": "人称与标点",
        "content": "上下文清楚时可以省略多余人称，强调责任、感受、边界或避免歧义时自然使用，不机械删除。微信不用句号和感叹号，逗号通常换成空格，不歧义时省略问号，省略号只用于真实无奈或停顿。",
        "source": "owner_humanlikeness_review_round1",
        "sensitivity": "private",
        "topic_tags": ["人称", "标点", "问号", "省略号"],
    },
    {
        "profile_key": "conversation.rejected_ai_patterns",
        "category": "rejected_inference",
        "title": "明确排除的人机表达",
        "content": "不生成‘不是……而是……’模板，不反复使用‘我理解你、我在、听起来你’，不写‘我只问这一次、这句有点伤人’等系统判断元话语，不把普通聊天整理成编号式客服总结。",
        "source": "owner_humanlikeness_review_round1",
        "sensitivity": "private",
        "topic_tags": ["人机感", "客服", "套话", "禁用表达"],
    },
    {
        "profile_key": "conversation.style_examples_gate",
        "category": "conditional_rule",
        "title": "风格示例准入规则",
        "content": "只有 Owner 评为‘像真人’的回复可以进入风格示例库。‘可接受’只代表可以通过，不作为学习样本；‘人机感明显’必须保留原因用于修正。",
        "source": "owner_humanlikeness_review_round1",
        "sensitivity": "private",
        "topic_tags": ["盲评", "风格库", "像真人", "审核"],
    },
    {
        "profile_key": "character.difference.du_and_ji",
        "category": "model_inference",
        "title": "两个人物的可辨识差异候选",
        "content": "杜佳颖以真实素材和普通同学关系为边界，更轻松克制、只有具体事情才主动；纪雨在计算机、11408、审美和现实拆解上更强，关系成熟后更主动、更深入，也更愿意展示柔软。该差异仍需 Owner 通过场景盲评确认。",
        "source": "questionnaire_analysis_2026-08-09",
        "sensitivity": "private",
        "confidence": 0.75,
        "topic_tags": ["人物差异", "杜佳颖", "纪雨", "待确认"],
    },
)


def import_questionnaire(source_path: str | Path) -> dict[str, object]:
    source = Path(source_path).expanduser().resolve()
    raw = source.read_text(encoding="utf-8")
    if not raw.strip():
        raise ValueError("真人感问卷原文为空")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    store = get_owner_profile_store()
    raw_entry = store.upsert_entry(
        profile_key="questionnaire.humanlikeness_round1.raw.2026-08-09",
        category="sensitive_self_report",
        title="双人物真人感审查问卷第一轮逐字原文",
        content="原文已加密保存，只用于审核证据与后续纠正；提炼规则不能覆盖或冒充原话。",
        raw_quote=raw,
        source=f"attachment_sha256:{digest}",
        sensitivity="high",
        topic_tags=["真人感问卷", "杜佳颖", "纪雨", "原始回答"],
        actor="owner",
    )
    entries = [store.upsert_entry(actor="owner", **item) for item in DERIVED_ENTRIES]
    return {
        "raw_entry_id": raw_entry["id"],
        "raw_sha256": digest,
        "raw_redacted": raw_entry["redacted"],
        "derived_entry_count": len(entries),
    }


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="加密导入双人物真人感问卷")
    parser.add_argument("source", help="UTF-8 问卷原文路径")
    args = parser.parse_args()
    print(json.dumps(import_questionnaire(args.source), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
