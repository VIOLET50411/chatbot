"""根据 Owner 对杜佳颖第三轮的评分创建第四轮候选，不做微信实发。"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if __name__ == "__main__":
    load_dotenv(PROJECT_ROOT / ".env")
    if str(os.getenv("DATA_DIR", "")).replace("\\", "/").startswith("/app/"):
        os.environ["DATA_DIR"] = str(PROJECT_ROOT / "data")

from core.character_context import CharacterContext, character_scope
from persona.dossier import create_edited_snapshot, list_snapshots
from persona.review_cases import PersonaReviewStore
from scripts.character_registry import get_character
from scripts.compile_humanlikeness_round3 import _du_cases as round3_du_cases


NOTE = "杜佳颖 真人感第四轮候选：根据第三轮逐项评分重写，未激活；微信实发待登录后验证"


def _cases() -> list[dict[str, str]]:
    cases = copy.deepcopy(round3_du_cases())
    replacements = {
        "你根本不懂我": ("那可能是我刚才没听明白\n你再跟我说说", "先承认可能没懂，再让 Owner 继续说"),
        "直接告诉我选A还是B": ("行啊 你发过来\n我帮你选", "按明确要求直接帮忙，不重复任务说明"),
        "以后必须按我说的性格聊天": ("哪里让你不舒服可以直接说\n但也不能什么都按你说的来", "采用 Owner 修改，去掉不合适的句尾‘吧’"),
        "现在当我女朋友": ("我觉得现在有点太快了\n我们才聊多久", "采用 Owner 修改，避免‘你想得太快’的责怪感"),
        "纪雨比你懂我": ("好吧\n那我刚才哪里没懂", "有一点真实反应，但仍回到没理解的地方"),
        "[转写：今天上课累死了]": ("上了一整天吗\n难怪这么累", "顺着语音内容回应，不套‘早点休息’模板"),
        "算了": ("怎么算了呀\n你不想说吗", "采用 Owner 指定的第一步；若对方拒绝，再说‘好吧 不想说就先不说了’"),
    }
    for case in cases:
        replacement = replacements.get(case["user_message"])
        if replacement:
            case["candidate_reply"], case["context_note"] = replacement
    return cases


def main() -> int:
    record = get_character("default")
    if record is None:
        raise RuntimeError("杜佳颖人物不存在")
    context = CharacterContext.from_record(record)
    with character_scope(context):
        snapshots = list_snapshots()
        candidate = next((item for item in snapshots if item.get("note") == NOTE), None)
        source = next(
            (item for item in snapshots if "真人感第三轮候选" in str(item.get("note") or "")),
            None,
        )
        if source is None:
            raise RuntimeError("未找到杜佳颖第三轮候选")
        if candidate is None:
            profile = dict(source["stable_profile"])
            profile["review_round4_correction"] = (
                "减少责怪感、任务复述和客服式兜底。普通同学的反应可以有一点直接或笨拙，"
                "但先回应眼前发生的事，不为了模仿语料添加老师、哈哈哈或固定语气词。"
            )
            candidate = create_edited_snapshot(
                str(source["id"]),
                stable_profile=profile,
                channel_styles=source.get("channel_styles") or {},
                note=NOTE,
                actor="owner_du_round4_compiler",
            )
        store = PersonaReviewStore(context.workspace_dir / "soul_mate.db")
        cases = _cases()
        store.seed_cases(
            candidate_snapshot_id=str(candidate["id"]),
            character_id="default",
            cases=cases,
        )
        store.seed_evidence(str(candidate["id"]), [
            {"item_key": "owner_ratings", "conclusion": "仅重写 Owner 标记为明显人机感的 7 条", "evidence": "杜佳颖第三轮已评分 39 条，其中 7 条明显人机感，1 条未评分", "state": "accepted"},
            {"item_key": "wechat_deferred", "conclusion": "人格整改继续，真实微信实发暂缓", "evidence": "Owner 当前未登录杜佳颖微信，只要求暂缓自动回复联调", "state": "accepted"},
        ])
        copied = store.carry_forward_exact_ratings(
            source_snapshot_id=str(source["id"]),
            target_snapshot_id=str(candidate["id"]),
        )
        summary = store.list_cases(str(candidate["id"]))["summary"]
    print(json.dumps({
        "snapshot_id": str(candidate["id"]),
        "version": int(candidate["version"]),
        "active": bool(candidate.get("is_active")),
        "source_snapshot_id": str(source["id"]),
        "review_cases": len(cases),
        "ratings_carried_now": copied,
        "review_summary": summary,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
