"""根据 Owner 对纪雨 v13 的最后一条否定评分生成 v14 候选，不自动激活。"""
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
from scripts.compile_ji_review_round5 import _cases as round5_cases


NOTE = "纪雨 真人感第六轮候选：只重写 v13 最后一条人机感回复，未激活"


def _cases() -> list[dict[str, str]]:
    cases = copy.deepcopy(round5_cases())
    for case in cases:
        if case["user_message"] == "我现在又累又饿还不想动":
            case["candidate_reply"] = "那先点个吃的\n吃完再说"
            case["context_note"] = "两条都是完整意思；直接解决饿的问题，不添加生硬推论"
    return cases


def main() -> int:
    record = get_character("ji-yu")
    if record is None:
        raise RuntimeError("纪雨人物不存在")
    context = CharacterContext.from_record(record)
    with character_scope(context):
        snapshots = list_snapshots()
        source = next((item for item in snapshots if int(item.get("version") or 0) == 13), None)
        if source is None:
            raise RuntimeError("未找到纪雨 v13 候选")
        candidate = next((item for item in snapshots if item.get("note") == NOTE), None)
        if candidate is None:
            profile = dict(source["stable_profile"])
            profile["humanlikeness_round6_correction"] = (
                "处理普通生活问题时直接接住眼前事实；不为了显得聪明补充生硬因果，"
                "也不把完整意思切成依赖后句的碎片。"
            )
            candidate = create_edited_snapshot(
                str(source["id"]),
                stable_profile=profile,
                channel_styles=source.get("channel_styles") or {},
                note=NOTE,
                actor="owner_ji_round6_compiler",
            )
        store = PersonaReviewStore(context.workspace_dir / "soul_mate.db")
        cases = _cases()
        store.seed_cases(
            candidate_snapshot_id=str(candidate["id"]),
            character_id="ji-yu",
            cases=cases,
        )
        store.seed_evidence(
            str(candidate["id"]),
            [
                {
                    "item_key": "round5_last_failure",
                    "conclusion": "只重写 v13 中仍被 Owner 标记为人机感的一条日常回复",
                    "evidence": "v13 已完成 46 条评价，其中 1 条仍为人机感明显",
                    "state": "accepted",
                }
            ],
        )
        copied = store.carry_forward_exact_ratings(
            source_snapshot_id=str(source["id"]),
            target_snapshot_id=str(candidate["id"]),
        )
        summary = store.list_cases(str(candidate["id"]))["summary"]
    print(
        json.dumps(
            {
                "snapshot_id": str(candidate["id"]),
                "version": int(candidate["version"]),
                "active": bool(candidate.get("is_active")),
                "source_snapshot_id": str(source["id"]),
                "review_cases": len(cases),
                "ratings_carried_now": copied,
                "review_summary": summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
