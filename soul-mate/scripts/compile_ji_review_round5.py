"""根据 Owner 对纪雨 v12 的最终评分生成严格真人感候选，不自动激活。"""
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
from scripts.compile_ji_consultant_revision import _cases as round4_cases


NOTE = "纪雨 真人感第五轮候选：重写 v12 最后 5 条人机感回复，严格零人机感后方可激活"


def _cases() -> list[dict[str, str]]:
    cases = copy.deepcopy(round4_cases())
    replacements = {
        "我现在又累又饿还不想动": (
            "先点个吃的\n吃饱了再想动不动",
            "保留两段完整意思，去掉 Owner 标记生硬的‘只会更不想动’",
        ),
        "帮我排今天复习": (
            "发我看看\n我帮你排",
            "按明确请求直接接任务，不复述流程",
        ),
        "这两天又有点怕死 一想到以后彻底不存在就很难受": (
            "怎么又想到这个了\n现在是不是特别难受",
            "记得这个主题，但不把咨询分析复述给 Owner",
        ),
        "我现在条件不够好 所以想变好一点再追真正喜欢的人": (
            "那就先让自己变得更好一点\n不过现在的你也没有那么差",
            "认可现实选择，用日常语气保留对现在的欣赏",
        ),
        "你知道我那么多不好的地方 会不会嫌弃我": (
            "会心疼啊\n但我不会因为这些就看轻你",
            "采用 Owner 补充的明确主语，避免像抽象判断",
        ),
    }
    for case in cases:
        replacement = replacements.get(case["user_message"])
        if replacement:
            case["candidate_reply"], case["context_note"] = replacement
    return cases


def main() -> int:
    record = get_character("ji-yu")
    if record is None:
        raise RuntimeError("纪雨人物不存在")
    context = CharacterContext.from_record(record)
    with character_scope(context):
        snapshots = list_snapshots()
        source = next((item for item in snapshots if int(item.get("version") or 0) == 12), None)
        if source is None:
            raise RuntimeError("未找到纪雨 v12 候选")
        candidate = next((item for item in snapshots if item.get("note") == NOTE), None)
        if candidate is None:
            profile = dict(source["stable_profile"])
            profile["strict_humanlikeness_gate"] = (
                "候选回复必须全部经过 Owner 评价，且人机感明显为零才允许激活；"
                "宁可继续改写，也不能用总体通过率掩盖单条不自然回复。"
            )
            candidate = create_edited_snapshot(
                str(source["id"]),
                stable_profile=profile,
                channel_styles=source.get("channel_styles") or {},
                note=NOTE,
                actor="owner_ji_round5_compiler",
            )
        store = PersonaReviewStore(context.workspace_dir / "soul_mate.db")
        cases = _cases()
        store.seed_cases(candidate_snapshot_id=str(candidate["id"]), character_id="ji-yu", cases=cases)
        store.seed_evidence(str(candidate["id"]), [
            {
                "item_key": "strict_review_gate",
                "conclusion": "全部场景完成且人机感明显为零才允许激活",
                "evidence": "Owner 于 2026-08-10 明确提高真人感验收门槛",
                "state": "accepted",
            },
            {
                "item_key": "round4_failures",
                "conclusion": "只重写纪雨 v12 中最后 5 条人机感回复",
                "evidence": "v12 已完成 46 条评价，其中 5 条被标记为人机感明显",
                "state": "accepted",
            },
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
