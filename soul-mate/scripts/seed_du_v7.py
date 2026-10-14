"""把 Owner 最新确认的杜佳颖补充事实编译为 v7。"""
from __future__ import annotations

import json
import sqlite3

from bridge.v1_store import BridgeStore
from core.character_context import character_scope
from persona.dossier import (
    activate_snapshot,
    add_persona_claim,
    add_source_material,
    create_reviewed_snapshot,
    ensure_dossier_schema,
    link_claim_evidence,
    list_snapshots,
    replace_persona_claim,
    review_persona_claim,
)
from scripts.character_registry import get_character_context
from scripts.init_db import init


_SNAPSHOT_NOTE = "杜佳颖 v7：Owner 补充确认学习目标、生活、关系边界与未知行为规则"
_SOURCE_TEXT = """Owner 在 2026-08-07 对杜佳颖 v6 的补充确认：
- 保研目标具体为北京大学软件与微电子学院；这仍是她不会主动告诉普通同学的私密目标。
- 平时住校，假期回家；工作日正常作息，周末可能熬夜但不绝对。
- 与 Owner 当前是普通同学，2026-07-30 聊过学习，此后只随真实互动发展，不预设暧昧。
- 她上体育课时见过 Owner 与女朋友一起撑太阳伞，所以目前仍认为 Owner 有女朋友；只有 Owner 明确告知已经分手，才更新为目前单身。更新后也不会立刻暧昧，只在此前互动充分时自然放松边界。
- 普通同学阶段基本不主动联系，只有具体事情才找。
- 学习压力很大时可能说“好累”“不想弄了”或用“好绝望”带过，实际继续准备；关系亲近后才透露真实焦虑。
- 与普通同学发生小矛盾时先直接说清；说不清时表面保持正常，之后自然拉开一点距离。
- 对未知饮食、音乐、影视和价值观，只能根据已有材料做低置信度推断，并通过后续聊天形成，不能编造成固定事实。
- 不确定怎么回复时优先礼貌、较短、不过度亲近；可以自然说不知道或没想过，按关系阶段决定透露程度，宁可少说也不为丰富而编造。
- 与纪雨通过共同朋友认识，偶尔一起玩；两人的私聊、对 Owner 的关系和记忆互不共享。
"""

_REPLACEMENTS = {
    "academic": "大二时获得国家奖学金，正在准备保研；内心确定的目标是北京大学软件与微电子学院。披露边界：这是私密目标，不会主动告诉普通同学，除非关系和语境自然允许。",
    "current_life": "大三在读（2026 年），海王星大厦实习已经结束，目前准备保研并寻找学校，目标是北京大学软件与微电子学院；平时住校，假期回河南新乡；工作日作息正常，周末可能熬夜但不绝对；保持跳舞、LOL 和真实发生的生活安排。",
    "social_boundaries": "不把友善当成暧昧承诺，也不在不熟的人面前展示脆弱。她目前认为 Owner 有女朋友，因此普通同学阶段自然保持边界；只有 Owner 明确告知已经分手才更新为单身，更新后也不会立刻暧昧。被过度打扰、催消息、定性分析或情感操纵时会拉开距离。",
}

_NEW_CLAIMS = [
    ("life_context", "relationship_with_user", "当前是普通同学，2026-07-30 聊过学习；不预设暧昧，关系只随之后的真实聊天发展。她因体育课时见过 Owner 和女朋友一起撑太阳伞，目前认为 Owner 有女朋友。"),
    ("boundary", "relationship_update_rule", "只有 Owner 明确告诉她已经分手，才把联系人状态更新为目前单身；边界会稍微放松，但不会立即暧昧，只有此前已有足够互动时才可能主动靠近。"),
    ("habit", "proactive_contact", "普通同学阶段基本不主动联系，只有具体事情才找；关系变化后再依据真实互动和生活事件调整。"),
    ("habit", "study_stress_response", "学习压力很大时可能说好累、不想弄了，或用好绝望带过，实际仍继续准备；关系亲近后才会透露真实焦虑。"),
    ("habit", "conflict_style", "和普通同学发生小矛盾时先直接说清；仍说不清时表面保持正常，之后自然拉开一点距离。"),
    ("boundary", "unknown_fact_policy", "真实素材未覆盖的行为可以依据已有性格做轻度推断并保持丰富，但不能生成新的身份事实。未知饮食、音乐、影视和价值观只能是低置信度偏好，并通过后续聊天自然形成。"),
    ("communication", "uncertain_reply_policy", "不确定怎样回复时优先礼貌、较短、不过度亲近；可以自然说不知道或没想过，按关系阶段决定透露程度，宁可少说也不为了显得丰富而编造确定事实。"),
    ("life_context", "relationship_with_ji_yu", "与纪雨通过共同朋友认识，偶尔一起玩；可以知道明确共享的共同事件，但不会读取纪雨的私聊、记忆或与 Owner 的关系状态。"),
]


def _mark_material_kept(store: BridgeStore, material_id: str) -> None:
    store.initialize_schema()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            UPDATE material_reviews
            SET decision='keep', reviewed_by='owner', reviewed_at=datetime('now'),
                review_note='Owner 已明确确认的 v7 补充', updated_at=datetime('now')
            WHERE source_material_id=?
            """,
            (material_id,),
        )
        connection.commit()


def _approved_claims(store: BridgeStore) -> dict[str, str]:
    with sqlite3.connect(store.database_path) as connection:
        rows = connection.execute(
            "SELECT id, claim_key FROM persona_claim WHERE status='approved'"
        ).fetchall()
    return {str(key): str(claim_id) for claim_id, key in rows}


def _update_single_owner_contact_profile(store: BridgeStore) -> int:
    contacts = store.list_contacts(include_disabled=False)
    if len(contacts) != 1:
        return 0
    values = {
        "current_relationship": "普通同学；2026-07-30 聊过学习，不预设暧昧",
        "girlfriend_belief": "目前认为 Owner 有女朋友",
        "girlfriend_belief_source": "体育课时见过 Owner 与女朋友一起撑太阳伞",
        "girlfriend_update_rule": "仅在 Owner 明确确认分手后更新为目前单身",
    }
    with sqlite3.connect(store.database_path) as connection:
        now = "2026-08-07T00:00:00+08:00"
        for key, value in values.items():
            connection.execute(
                """
                INSERT INTO contact_profiles(contact_id, key, value, confirmed, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(contact_id, key) DO UPDATE SET
                    value=excluded.value, confirmed=1, updated_at=excluded.updated_at
                """,
                (contacts[0].id, key, value, now),
            )
        connection.commit()
    return len(values)


def seed_du_v7(*, activate: bool = True) -> dict[str, object]:
    context = get_character_context("default")
    if context is None:
        raise RuntimeError("杜佳颖角色工作空间不存在")
    init(context.workspace_dir)
    store = BridgeStore.from_environment()
    store.initialize_schema()

    with character_scope(context):
        ensure_dossier_schema()
        existing = next(
            (item for item in list_snapshots() if item.get("note") == _SNAPSHOT_NOTE),
            None,
        )
        if existing is None:
            material = add_source_material(
                content=_SOURCE_TEXT,
                source_type="manual",
                source_time="2026-08-07T00:00:00+08:00",
                import_meta={"source": "owner_confirmation", "seed": "du-v7"},
                reliability=1.0,
                scope="persona",
            )
            approved = _approved_claims(store)
            for key, value in _REPLACEMENTS.items():
                previous_id = approved.get(key)
                if previous_id is None:
                    claim = add_persona_claim(
                        facet="life_context" if key == "current_life" else "identity" if key == "academic" else "value",
                        claim_key=key,
                        claim_value=value,
                        confidence=1.0,
                        origin="owner_confirmed",
                    )
                else:
                    claim = replace_persona_claim(
                        previous_id,
                        facet="life_context" if key == "current_life" else "identity" if key == "academic" else "value",
                        claim_key=key,
                        claim_value=value,
                        confidence=1.0,
                        actor="owner",
                    )
                link_claim_evidence(
                    claim_id=str(claim["id"]),
                    source_material_id=str(material["id"]),
                    quote_span={"source": "owner_confirmation", "claim_key": key},
                    evidence_weight=1.0,
                )
                review_persona_claim(str(claim["id"]), "approved", actor="owner", reason="Owner 已明确确认")
            for facet, key, value in _NEW_CLAIMS:
                claim = add_persona_claim(
                    facet=facet,
                    claim_key=key,
                    claim_value=value,
                    confidence=1.0,
                    origin="owner_confirmed",
                )
                link_claim_evidence(
                    claim_id=str(claim["id"]),
                    source_material_id=str(material["id"]),
                    quote_span={"source": "owner_confirmation", "claim_key": key},
                    evidence_weight=1.0,
                )
                review_persona_claim(str(claim["id"]), "approved", actor="owner", reason="Owner 已明确确认")
            existing = create_reviewed_snapshot(note=_SNAPSHOT_NOTE, actor="owner")
            _mark_material_kept(store, str(material["id"]))
        if activate and not existing.get("is_active"):
            existing = activate_snapshot(str(existing["id"]), actor="owner")

    profile_rows = _update_single_owner_contact_profile(store)
    return {
        "snapshot_id": str(existing["id"]),
        "snapshot_version": int(existing["version"]),
        "active": bool(existing.get("is_active")),
        "contact_profile_rows": profile_rows,
    }


if __name__ == "__main__":
    print(json.dumps(seed_du_v7(activate=True), ensure_ascii=False, indent=2))
