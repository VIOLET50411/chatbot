"""默认角色人设/语料迁移脚本。

把 `scripts/init_persona.py` 中用户已确认保留的默认人设、历史朋友圈/聊天/抖音语料，
以及新增的 2026-07-30 对话语料，导入到可审计的角色档案链路：
source_material -> persona_claim -> evidence -> reviewed snapshot。

默认会审核并发布快照，因为迁移内容由用户显式确认；不会绕过审计记录。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.character_context import character_scope
from persona.dossier import (
    activate_snapshot,
    add_persona_claim,
    add_source_material,
    create_reviewed_snapshot,
    ensure_dossier_schema,
    link_claim_evidence,
    replace_persona_claim,
    review_persona_claim,
)
from scripts.character_registry import get_character_context
from scripts.init_db import init
from scripts.init_persona import (
    CHAT_CORPUS,
    DOUYIN_CORPUS,
    MOMENTS_CORPUS,
    PERSONA,
)


NEW_CHAT_FILE = PROJECT_ROOT / "data" / "corpus" / "chat_2026-07-30.txt"
ACTOR = "owner"
REVIEW_REASON = "用户已确认保留默认角色人设/语料，并通过审核链路迁移"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _connect(context) -> sqlite3.Connection:
    connection = sqlite3.connect(context.workspace_dir / "soul_mate.db")
    connection.row_factory = sqlite3.Row
    return connection


def _source_material_id(context, content_hash: str) -> str | None:
    connection = _connect(context)
    try:
        row = connection.execute(
            """
            SELECT id FROM source_material
            WHERE character_id=? AND content_hash=? LIMIT 1
            """,
            (context.character_id, content_hash),
        ).fetchone()
        return str(row["id"]) if row else None
    finally:
        connection.close()


def _claim_id(context, claim_key: str) -> str | None:
    connection = _connect(context)
    try:
        row = connection.execute(
            """
            SELECT id FROM persona_claim
            WHERE character_id=? AND claim_key=? AND status != 'superseded'
            ORDER BY created_at DESC LIMIT 1
            """,
            (context.character_id, claim_key),
        ).fetchone()
        return str(row["id"]) if row else None
    finally:
        connection.close()


def _evidence_exists(context, claim_id: str, material_id: str) -> bool:
    connection = _connect(context)
    try:
        row = connection.execute(
            """
            SELECT 1 FROM claim_evidence
            WHERE claim_id=? AND source_material_id=? LIMIT 1
            """,
            (claim_id, material_id),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _add_material(
    context,
    *,
    content: str,
    source_type: str,
    source_time: str | None = None,
    reliability: float = 1.0,
    import_meta: Mapping[str, Any] | None = None,
) -> tuple[str, bool]:
    digest = _content_hash(content)
    existing = _source_material_id(context, digest)
    if existing:
        return existing, False
    material = add_source_material(
        content=content,
        source_type=source_type,
        source_time=source_time,
        reliability=reliability,
        import_meta=dict(import_meta or {}),
        scope="persona",
    )
    return str(material["id"]), True


def _add_claim(
    context,
    *,
    facet: str,
    claim_key: str,
    claim_value: str,
    context_payload: Mapping[str, Any] | None = None,
    origin: str = "user_approved_migration",
) -> tuple[str, bool]:
    existing = _claim_id(context, claim_key)
    if existing:
        connection = _connect(context)
        try:
            row = connection.execute(
                """
                SELECT claim_value, status FROM persona_claim
                WHERE id=? AND character_id=?
                """,
                (existing, context.character_id),
            ).fetchone()
            current_value = str(row["claim_value"]) if row else ""
            current_status = str(row["status"]) if row else ""
        finally:
            connection.close()
        if current_value == claim_value and current_status in {"pending", "approved"}:
            return existing, False
        replacement = replace_persona_claim(
            existing,
            facet=facet,
            claim_key=claim_key,
            claim_value=claim_value,
            context=dict(context_payload or {}),
            polarity="supports",
            confidence=1.0,
            actor=ACTOR,
        )
        return str(replacement["id"]), True
    claim = add_persona_claim(
        facet=facet,
        claim_key=claim_key,
        claim_value=claim_value,
        context=dict(context_payload or {}),
        polarity="supports",
        confidence=1.0,
        origin=origin,
    )
    return str(claim["id"]), True


def _link_evidence(
    context,
    *,
    claim_id: str,
    material_id: str,
    evidence_weight: float = 1.0,
    quote_span: Mapping[str, Any] | None = None,
) -> bool:
    if _evidence_exists(context, claim_id, material_id):
        return False
    link_claim_evidence(
        claim_id=claim_id,
        source_material_id=material_id,
        quote_span=dict(quote_span or {}),
        support_kind="supports",
        evidence_weight=evidence_weight,
    )
    return True


def _approve_claim(context, claim_id: str, *, approve: bool = True) -> bool:
    connection = _connect(context)
    try:
        row = connection.execute(
            "SELECT status, polarity FROM persona_claim WHERE id=? AND character_id=?",
            (claim_id, context.character_id),
        ).fetchone()
        status = row["status"] if row else ""
        polarity = row["polarity"] if row else ""
    finally:
        connection.close()
    if status == "approved":
        if polarity == "supports":
            return False
        connection = _connect(context)
        try:
            connection.execute(
                """
                UPDATE persona_claim
                SET polarity='supports', confidence=1.0
                WHERE id=? AND character_id=?
                """,
                (claim_id, context.character_id),
            )
            connection.commit()
        finally:
            connection.close()
        return True
    if status != "pending":
        return False
    if not approve:
        return False
    connection = _connect(context)
    try:
        connection.execute(
            """
            UPDATE persona_claim
            SET polarity='supports', confidence=1.0
            WHERE id=? AND character_id=?
            """,
            (claim_id, context.character_id),
        )
        connection.commit()
    finally:
        connection.close()
    review_persona_claim(
        claim_id,
        "approved",
        actor=ACTOR,
        reason=REVIEW_REASON,
    )
    return True


def _user_profile_value(context, key: str) -> str:
    connection = _connect(context)
    try:
        row = connection.execute(
            "SELECT value FROM user_profile WHERE key=?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row else ""
    finally:
        connection.close()


def _write_legacy_persona(context) -> None:
    connection = _connect(context)
    try:
        for key, value in PERSONA.items():
            if key == "user_profile":
                continue
            connection.execute(
                """
                INSERT OR REPLACE INTO persona (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                """,
                (key, str(value)),
            )
        connection.commit()
    finally:
        connection.close()


def _facet_for_key(key: str) -> str:
    lowered = key.lower()
    if any(token in lowered for token in (
        "style", "language", "speaking", "communication", "chat"
    )):
        return "communication"
    if any(token in lowered for token in (
        "boundary", "rule", "attitude", "social_boundaries"
    )):
        return "boundary"
    if any(token in lowered for token in (
        "habit", "sleep", "moments_habit", "sensory"
    )):
        return "habit"
    if any(token in lowered for token in (
        "hobby", "interest", "food", "aesthetic", "pet", "fan", "lol"
    )):
        return "preference"
    if any(token in lowered for token in (
        "location", "education", "occupation", "life", "family",
        "academic", "current", "relationship_with_user", "contact"
    )):
        return "life_context"
    if any(token in lowered for token in (
        "personality", "value", "social", "behavior", "emotional",
        "appearance", "close_friends", "vulnerability"
    )):
        return "value"
    return "identity"


def _chat_style_claim_value() -> str:
    style = {
        "sentence_style": str(PERSONA.get("language_style_chat", "")).replace("\n", " "),
        "tone": "轻松随意，短句连发，礼貌但不过分亲近",
        "common_words": ["老师", "哈哈哈", "加油", "好羡慕"],
        "emoji_habits": ["哈哈哈", "^ ^", "👍"],
        "special_expressions": ["应试", "勾心斗角", "默默许愿", "都有光明的未来"],
    }
    return json.dumps(style, ensure_ascii=False, sort_keys=True)


def _moments_style_claim_value() -> str:
    style = {
        "sentence_style": str(PERSONA.get("language_style_moments", "")).replace("\n", " "),
        "tone": "极简留白，短句收尾，不刻意表达情绪",
        "common_words": ["最近", "绝望", "重庆", "累"],
        "emoji_habits": ["……", "。。。", "^ ^"],
        "special_expressions": ["好绝望", "别太羡慕", "神"],
    }
    return json.dumps(style, ensure_ascii=False, sort_keys=True)


def _import_default_persona(context, *, approve: bool = True) -> bool:
    changed = False
    for key, value in PERSONA.items():
        if key == "user_profile":
            continue
        content = f"{key}: {value}"
        material_id, material_created = _add_material(
            context,
            content=content,
            source_type="canonical_setup",
            reliability=1.0,
            import_meta={"source": "default_persona", "source_key": key},
        )
        claim_id, claim_created = _add_claim(
            context,
            facet=_facet_for_key(key),
            claim_key=key,
            claim_value=str(value),
            context_payload={"source": "default_persona", "source_key": key},
        )
        evidence_created = _link_evidence(
            context,
            claim_id=claim_id,
            material_id=material_id,
            evidence_weight=1.0,
            quote_span={"kind": "whole_material", "field": key},
        )
        approved = _approve_claim(context, claim_id, approve=approve)
        changed = changed or material_created or claim_created or evidence_created or approved

    extra_claims = {
        "occupation": {
            "facet": "life_context",
            "value": "西南大学软件工程在读学生，准备保研",
        },
        "nickname_for_user": {
            "facet": "communication",
            "value": "老师",
        },
        "speaking_style": {
            "facet": "communication",
            "value": str(PERSONA.get("language_style_chat", "")).replace("\n", " "),
        },
    }
    for key, claim in extra_claims.items():
        value = str(claim["value"])
        content = f"{key}: {value}"
        material_id, material_created = _add_material(
            context,
            content=content,
            source_type="canonical_setup",
            reliability=1.0,
            import_meta={"source": "default_persona", "source_key": key},
        )
        claim_id, claim_created = _add_claim(
            context,
            facet=claim["facet"],
            claim_key=key,
            claim_value=value,
            context_payload={"source": "default_persona", "source_key": key},
        )
        evidence_created = _link_evidence(
            context,
            claim_id=claim_id,
            material_id=material_id,
            evidence_weight=1.0,
            quote_span={"kind": "whole_material", "field": key},
        )
        approved = _approve_claim(context, claim_id, approve=approve)
        changed = changed or material_created or claim_created or evidence_created or approved

    user_profile = str(PERSONA.get("user_profile", "")).strip()
    if user_profile and _user_profile_value(context, "VIOLET") != user_profile:
        connection = _connect(context)
        try:
            connection.execute(
                """
                INSERT OR REPLACE INTO user_profile
                (key, value, confirmed, updated_at)
                VALUES (?, ?, 1, datetime('now'))
                """,
                ("VIOLET", user_profile),
            )
            connection.commit()
        finally:
            connection.close()
        changed = True
    return changed


def _import_corpus(context, *, approve: bool = True) -> bool:
    changed = False
    chat_material_ids: list[str] = []
    moments_material_ids: list[str] = []

    for index, item in enumerate(CHAT_CORPUS):
        content = item if isinstance(item, str) else "\n".join(item)
        material_id, created = _add_material(
            context,
            content=content,
            source_type="chat",
            source_time=None,
            reliability=1.0,
            import_meta={"source": "default_persona", "corpus_group": "chat", "index": index},
        )
        chat_material_ids.append(material_id)
        changed = changed or created

    new_chat = NEW_CHAT_FILE.read_text(encoding="utf-8").strip()
    if new_chat:
        material_id, created = _add_material(
            context,
            content=new_chat,
            source_type="chat",
            source_time="2026-07-30 18:17:00",
            reliability=1.0,
            import_meta={
                "source": "user_provided",
                "conversation_date": "2026-07-30",
                "roles": ["VIOLET", "杜佳颖"],
            },
        )
        chat_material_ids.append(material_id)
        changed = changed or created

    for index, item in enumerate(MOMENTS_CORPUS):
        material_id, created = _add_material(
            context,
            content=str(item),
            source_type="moments",
            source_time=None,
            reliability=1.0,
            import_meta={"source": "default_persona", "corpus_group": "moments", "index": index},
        )
        moments_material_ids.append(material_id)
        changed = changed or created

    for index, item in enumerate(DOUYIN_CORPUS):
        material_id, created = _add_material(
            context,
            content=str(item),
            source_type="douyin_reference",
            source_time=None,
            reliability=0.8,
            import_meta={"source": "default_persona", "corpus_group": "douyin", "index": index},
        )
        changed = changed or created

    if chat_material_ids:
        claim_id, claim_created = _add_claim(
            context,
            facet="communication",
            claim_key="chat_style_profile",
            claim_value=_chat_style_claim_value(),
            context_payload={
                "channel": "wechat_chat",
                "source_type": "chat",
                "source": "default_persona",
            },
        )
        changed = changed or claim_created
        for material_id in chat_material_ids:
            linked = _link_evidence(
                context,
                claim_id=claim_id,
                material_id=material_id,
                evidence_weight=1.0,
                quote_span={"kind": "whole_material"},
            )
            changed = changed or linked
        changed = _approve_claim(context, claim_id, approve=approve) or changed

    if moments_material_ids:
        claim_id, claim_created = _add_claim(
            context,
            facet="communication",
            claim_key="moments_style_profile",
            claim_value=_moments_style_claim_value(),
            context_payload={
                "channel": "moments",
                "source_type": "moments",
                "source": "default_persona",
            },
        )
        changed = changed or claim_created
        for material_id in moments_material_ids:
            linked = _link_evidence(
                context,
                claim_id=claim_id,
                material_id=material_id,
                evidence_weight=1.0,
                quote_span={"kind": "whole_material"},
            )
            changed = changed or linked
        changed = _approve_claim(context, claim_id, approve=approve) or changed

    if new_chat:
        summary = (
            "2026年7月30日，VIOLET向杜佳颖请教高数和考研准备，"
            "杜佳颖在成都地铁上回复，提到自己期末靠整理材料和给别人讲题，"
            "也表示保研压力大、有梦校但先不说；最后双方互道加油，"
            "VIOLET感谢并提出回校后请奶茶。"
        )
        claim_id, claim_created = _add_claim(
            context,
            facet="life_context",
            claim_key="recent_chat_2026_07_30",
            claim_value=summary,
            context_payload={
                "source": "user_provided",
                "conversation_date": "2026-07-30",
            },
        )
        changed = changed or claim_created
        for material_id in chat_material_ids:
            linked = _link_evidence(
                context,
                claim_id=claim_id,
                material_id=material_id,
                evidence_weight=1.0,
                quote_span={"kind": "whole_material"},
            )
            changed = changed or linked
        changed = _approve_claim(context, claim_id, approve=approve) or changed

    return changed


def _publish_snapshot(context, *, changed: bool) -> bool:
    if not changed:
        return False
    try:
        snapshot = create_reviewed_snapshot(
            note="默认角色迁移：杜佳颖/LocinJ 人设、语料与 2026-07-30 对话，已由用户确认。",
            actor=ACTOR,
        )
        activate_snapshot(str(snapshot["id"]), actor=ACTOR)
    except ValueError:
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移默认角色的人设和语料到审核链路")
    parser.add_argument("--approve", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    context = get_character_context("default")
    if context is None:
        raise RuntimeError("默认角色不存在，请先初始化角色注册表")

    with character_scope(context):
        init(data_dir=context.workspace_dir)
        _write_legacy_persona(context)
        ensure_dossier_schema()
        persona_changed = _import_default_persona(context, approve=args.approve)
        corpus_changed = _import_corpus(context, approve=args.approve)
        snapshot_published = _publish_snapshot(
            context,
            changed=persona_changed or corpus_changed,
        )

    print("默认角色迁移完成")
    print(f"  persona_changed={persona_changed}")
    print(f"  corpus_changed={corpus_changed}")
    print(f"  snapshot_published={snapshot_published}")


if __name__ == "__main__":
    main()
