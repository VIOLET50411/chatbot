"""角色档案的上下文绑定仓储与无损迁移。

职责边界：
- 素材、候选结论、证据与快照都写入当前 CharacterContext 的独立 SQLite。
- 旧 KV 人设与语料仅复制为 legacy 记录，永不在迁移中删除或覆盖。
- 新导入和审核流程可复用这里的低层写入能力；本模块不调用 LLM。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from core.character_context import require_character_context
from persona.dossier_schema import (
    DOSSIER_SCHEMA,
    PERSONA_DOSSIER_MIGRATION,
    PERSONA_SNAPSHOT_LIFECYCLE_MIGRATION,
)
from persona.style_aggregator import merge_styles


_NAMESPACE = uuid.UUID("7466f547-47ee-460f-9b4a-fc539284e6b1")


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    """为每个档案操作提供事务，并在 Windows 上确定释放 SQLite 文件句柄。"""
    context = require_character_context()
    connection = sqlite3.connect(context.workspace_dir / "soul_mate.db", timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def _character_id() -> str:
    return require_character_context().character_id


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _stable_id(*parts: object) -> str:
    return str(uuid.uuid5(_NAMESPACE, ":".join(str(part) for part in parts)))


def _facet_for_legacy_key(key: str) -> str:
    lowered = key.lower()
    if any(token in lowered for token in ("style", "language", "speaking", "expression")):
        return "communication"
    if any(token in lowered for token in ("boundary", "rule", "attitude")):
        return "boundary"
    if any(token in lowered for token in ("habit", "sleep", "moments")):
        return "habit"
    if any(token in lowered for token in ("hobby", "interest", "food", "aesthetic", "pet")):
        return "preference"
    if any(token in lowered for token in ("location", "education", "occupation", "life", "family")):
        return "life_context"
    if any(token in lowered for token in ("personality", "value", "social")):
        return "value"
    return "identity"


def _legacy_source_type(source_type: str) -> str:
    return {
        "douyin": "douyin_reference",
        "moments": "moments",
        "chat": "chat",
        "manual": "manual",
    }.get(source_type, "legacy")


def _audit(
    connection: sqlite3.Connection,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    actor: str = "system",
    payload: Mapping[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO persona_audit_log
            (id, character_id, action, entity_type, entity_id, actor, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            _character_id(),
            action,
            entity_type,
            entity_id,
            actor,
            _json(dict(payload or {})),
        ),
    )


def ensure_dossier_schema() -> bool:
    """创建档案结构、无损迁移旧记录并补齐快照生命周期标记。"""
    with _connection() as connection:
        connection.executescript(DOSSIER_SCHEMA)
        _ensure_dossier_columns(connection)
        applied = connection.execute(
            "SELECT 1 FROM persona_schema_migration WHERE name=?",
            (PERSONA_DOSSIER_MIGRATION,),
        ).fetchone()
        migrated = False
        if not applied:
            _migrate_legacy_records(connection)
            connection.execute(
                "INSERT INTO persona_schema_migration (name, detail_json) VALUES (?, ?)",
                (
                    PERSONA_DOSSIER_MIGRATION,
                    _json({"strategy": "copy_only", "legacy_tables": ["persona", "corpus", "aggregated_style"]}),
                ),
            )
            _audit(
                connection,
                action="migrate",
                entity_type="persona_dossier",
                entity_id=PERSONA_DOSSIER_MIGRATION,
                payload={"strategy": "copy_only"},
            )
            migrated = True
        _migrate_snapshot_lifecycle(connection)
    return migrated


def _ensure_dossier_columns(connection: sqlite3.Connection) -> None:
    claim_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(persona_claim)").fetchall()
    }
    if "review_reason" not in claim_columns:
        connection.execute(
            "ALTER TABLE persona_claim ADD COLUMN review_reason TEXT NOT NULL DEFAULT ''"
        )

    snapshot_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(persona_snapshot)").fetchall()
    }
    if "is_published" not in snapshot_columns:
        connection.execute(
            "ALTER TABLE persona_snapshot ADD COLUMN is_published INTEGER NOT NULL DEFAULT 0"
        )
    if "published_at" not in snapshot_columns:
        connection.execute("ALTER TABLE persona_snapshot ADD COLUMN published_at TEXT")


def _migrate_snapshot_lifecycle(connection: sqlite3.Connection) -> None:
    """将已存在的不可变版本一次性归档为已发布、只读快照。"""
    applied = connection.execute(
        "SELECT 1 FROM persona_schema_migration WHERE name=?",
        (PERSONA_SNAPSHOT_LIFECYCLE_MIGRATION,),
    ).fetchone()
    if applied:
        return

    migrated_count = connection.execute(
        """
        UPDATE persona_snapshot
        SET is_published=1,
            is_read_only=1,
            published_at=COALESCE(published_at, activated_at, created_at)
        WHERE is_published=0 OR is_read_only=0 OR published_at IS NULL
        """
    ).rowcount
    connection.execute(
        "INSERT INTO persona_schema_migration (name, detail_json) VALUES (?, ?)",
        (
            PERSONA_SNAPSHOT_LIFECYCLE_MIGRATION,
            _json({"strategy": "mark_existing_snapshots_published_read_only", "count": migrated_count}),
        ),
    )
    _audit(
        connection,
        action="migrate",
        entity_type="persona_snapshot",
        entity_id=PERSONA_SNAPSHOT_LIFECYCLE_MIGRATION,
        payload={"count": migrated_count},
    )


def _migrate_legacy_records(connection: sqlite3.Connection) -> None:
    character_id = _character_id()
    corpus_rows = connection.execute(
        "SELECT id, content, source_type, style_json, created_at FROM corpus ORDER BY id"
    ).fetchall()
    for row in corpus_rows:
        material_id = _stable_id(character_id, "legacy-corpus", row["id"])
        source_type = _legacy_source_type(row["source_type"] or "legacy")
        content = row["content"] or ""
        connection.execute(
            """
            INSERT OR IGNORE INTO source_material
                (id, character_id, source_type, content, source_time, import_meta_json,
                 reliability, scope, legacy_corpus_id, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                material_id,
                character_id,
                source_type,
                content,
                row["created_at"],
                _json({
                    "legacy_table": "corpus",
                    "legacy_id": row["id"],
                    "style_json": _parse_json(row["style_json"], {}),
                }),
                0.5,
                "persona",
                row["id"],
                _content_hash(content),
            ),
        )

    persona_rows = connection.execute(
        "SELECT key, value, updated_at FROM persona ORDER BY key"
    ).fetchall()
    legacy_claim_ids: list[str] = []
    for row in persona_rows:
        key = row["key"]
        value = row["value"] or ""
        material_id = _stable_id(character_id, "legacy-persona-source", key)
        claim_id = _stable_id(character_id, "legacy-persona-claim", key)
        material_content = f"{key}: {value}"
        connection.execute(
            """
            INSERT OR IGNORE INTO source_material
                (id, character_id, source_type, content, source_time, import_meta_json,
                 reliability, scope, content_hash)
            VALUES (?, ?, 'canonical_setup', ?, ?, ?, ?, 'persona', ?)
            """,
            (
                material_id,
                character_id,
                material_content,
                row["updated_at"],
                _json({"legacy_table": "persona", "legacy_key": key}),
                0.5,
                _content_hash(material_content),
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO persona_claim
                (id, character_id, facet, claim_key, claim_value, context_json,
                 polarity, confidence, status, origin)
            VALUES (?, ?, ?, ?, ?, ?, 'uncertain', ?, 'pending', 'legacy_migration')
            """,
            (
                claim_id,
                character_id,
                _facet_for_legacy_key(key),
                key,
                value,
                _json({"legacy_key": key}),
                0.5,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO claim_evidence
                (id, claim_id, source_material_id, quote_span_json, support_kind, evidence_weight)
            VALUES (?, ?, ?, ?, 'context', ?)
            """,
            (
                _stable_id(character_id, "legacy-persona-evidence", key),
                claim_id,
                material_id,
                _json({"field": key}),
                0.5,
            ),
        )
        legacy_claim_ids.append(claim_id)

    existing_snapshot = connection.execute(
        "SELECT 1 FROM persona_snapshot WHERE character_id=? LIMIT 1",
        (character_id,),
    ).fetchone()
    if existing_snapshot:
        return

    legacy_persona = {row["key"]: row["value"] for row in persona_rows}
    style_row = connection.execute(
        "SELECT style_json FROM aggregated_style WHERE id=1"
    ).fetchone()
    legacy_style = _parse_json(style_row["style_json"] if style_row else None, {})
    channel_styles = {
        "wechat_chat": {
            "source": "legacy_migration",
            "style": legacy_style,
            "legacy_notes": [
                legacy_persona[key]
                for key in ("speaking_style", "language_style_chat")
                if legacy_persona.get(key)
            ],
        },
        "moments": {
            "source": "legacy_migration",
            "legacy_notes": [legacy_persona["language_style_moments"]]
            if legacy_persona.get("language_style_moments")
            else [],
        },
        "reference": {"source": "legacy_migration"},
    }
    snapshot_payload = {
        "stable_profile": legacy_persona,
        "channel_styles": channel_styles,
        "claim_ids": legacy_claim_ids,
    }
    snapshot_id = _stable_id(character_id, "legacy-migration-snapshot")
    connection.execute(
        """
        INSERT INTO persona_snapshot
            (id, character_id, version, snapshot_kind, approved_claims_hash,
             stable_profile_json, channel_styles_json, claim_ids_json,
             note, is_active, is_read_only, is_published, published_at, activated_at)
        VALUES (?, ?, 1, 'legacy_migration', ?, ?, ?, ?, ?, 1, 1, 1, datetime('now'), datetime('now'))
        """,
        (
            snapshot_id,
            character_id,
            _content_hash(_json(snapshot_payload)),
            _json(legacy_persona),
            _json(channel_styles),
            _json(legacy_claim_ids),
            "由旧 persona、corpus 与 aggregated_style 无损复制；未自动批准任何候选结论。",
        ),
    )


def add_source_material(
    *,
    content: str,
    source_type: str,
    source_time: str | None = None,
    import_meta: Mapping[str, Any] | None = None,
    reliability: float = 0.5,
    scope: str = "persona",
) -> dict[str, Any]:
    ensure_dossier_schema()
    material_id = str(uuid.uuid4())
    character_id = _character_id()
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO source_material
                (id, character_id, source_type, content, source_time, import_meta_json,
                 reliability, scope, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                material_id,
                character_id,
                source_type,
                content,
                source_time,
                _json(dict(import_meta or {})),
                reliability,
                scope,
                _content_hash(content),
            ),
        )
        _audit(
            connection,
            action="create",
            entity_type="source_material",
            entity_id=material_id,
            payload={"source_type": source_type, "scope": scope},
        )
    return {"id": material_id, "character_id": character_id}


def add_persona_claim(
    *,
    facet: str,
    claim_key: str,
    claim_value: str,
    context: Mapping[str, Any] | None = None,
    polarity: str = "supports",
    confidence: float = 0.5,
    origin: str = "inference",
) -> dict[str, Any]:
    ensure_dossier_schema()
    claim_id = str(uuid.uuid4())
    character_id = _character_id()
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO persona_claim
                (id, character_id, facet, claim_key, claim_value, context_json,
                 polarity, confidence, status, origin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                claim_id,
                character_id,
                facet,
                claim_key,
                claim_value,
                _json(dict(context or {})),
                polarity,
                confidence,
                origin,
            ),
        )
        _audit(
            connection,
            action="create",
            entity_type="persona_claim",
            entity_id=claim_id,
            payload={"facet": facet, "claim_key": claim_key, "origin": origin},
        )
    return {"id": claim_id, "character_id": character_id, "status": "pending"}


def link_claim_evidence(
    *,
    claim_id: str,
    source_material_id: str,
    quote_span: Mapping[str, Any] | None = None,
    support_kind: str = "supports",
    evidence_weight: float = 0.5,
) -> str:
    ensure_dossier_schema()
    if support_kind not in {"supports", "contradicts", "context"}:
        raise ValueError("证据关系必须是 supports、contradicts 或 context")

    evidence_id = str(uuid.uuid4())
    character_id = _character_id()
    with _connection() as connection:
        claim = connection.execute(
            "SELECT character_id FROM persona_claim WHERE id=?", (claim_id,)
        ).fetchone()
        material = connection.execute(
            "SELECT character_id, is_deleted FROM source_material WHERE id=?",
            (source_material_id,),
        ).fetchone()
        if not claim or claim["character_id"] != character_id:
            raise KeyError("未找到当前角色的候选结论")
        if not material or material["character_id"] != character_id:
            raise KeyError("未找到当前角色的原始素材")
        if material["is_deleted"]:
            raise ValueError("已归档素材不能关联新的证据")

        existing = connection.execute(
            """
            SELECT id FROM claim_evidence
            WHERE claim_id=? AND source_material_id=? AND support_kind=?
            """,
            (claim_id, source_material_id, support_kind),
        ).fetchone()
        if existing:
            return existing["id"]

        connection.execute(
            """
            INSERT INTO claim_evidence
                (id, claim_id, source_material_id, quote_span_json, support_kind, evidence_weight)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                claim_id,
                source_material_id,
                _json(dict(quote_span or {})),
                support_kind,
                evidence_weight,
            ),
        )
        _audit(
            connection,
            action="link",
            entity_type="claim_evidence",
            entity_id=evidence_id,
            payload={"claim_id": claim_id, "source_material_id": source_material_id},
        )
    return evidence_id


def get_active_snapshot() -> dict[str, Any] | None:
    ensure_dossier_schema()
    character_id = _character_id()
    with _connection() as connection:
        row = connection.execute(
            """
            SELECT * FROM persona_snapshot
            WHERE character_id=? AND is_active=1 AND is_published=1 AND is_read_only=1
            ORDER BY version DESC LIMIT 1
            """,
            (character_id,),
        ).fetchone()
    return _snapshot_from_row(row) if row else None


def list_source_materials(
    *,
    source_type: str | None = None,
    include_deleted: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """按当前角色分页读取原始素材；归档记录默认不参与工作队列。"""
    ensure_dossier_schema()
    safe_limit = min(100, max(1, int(limit)))
    safe_offset = max(0, int(offset))
    conditions = ["character_id=?"]
    parameters: list[Any] = [_character_id()]
    if source_type:
        conditions.append("source_type=?")
        parameters.append(source_type)
    if not include_deleted:
        conditions.append("is_deleted=0")
    where = " AND ".join(conditions)

    with _connection() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM source_material
            WHERE {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, safe_limit, safe_offset],
        ).fetchall()
        total = connection.execute(
            f"SELECT COUNT(*) FROM source_material WHERE {where}", parameters
        ).fetchone()[0]
    return {"total": total, "items": [_material_from_row(row) for row in rows]}


def archive_source_material(material_id: str, *, actor: str = "admin") -> bool:
    """软归档素材，保留证据链与审计，禁止物理删除历史来源。"""
    ensure_dossier_schema()
    character_id = _character_id()
    with _connection() as connection:
        row = connection.execute(
            "SELECT is_deleted FROM source_material WHERE id=? AND character_id=?",
            (material_id, character_id),
        ).fetchone()
        if not row:
            raise KeyError("未找到当前角色的原始素材")
        if row["is_deleted"]:
            return False
        connection.execute(
            """
            UPDATE source_material
            SET is_deleted=1, deleted_at=datetime('now')
            WHERE id=? AND character_id=?
            """,
            (material_id, character_id),
        )
        _audit(
            connection,
            action="archive",
            entity_type="source_material",
            entity_id=material_id,
            actor=actor,
        )
    return True


def list_audit_events(
    *,
    limit: int = 100,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_dossier_schema()
    safe_limit = min(200, max(1, int(limit)))
    conditions = ["character_id=?"]
    parameters: list[Any] = [_character_id()]
    if entity_type:
        conditions.append("entity_type=?")
        parameters.append(entity_type)
    if entity_id:
        conditions.append("entity_id=?")
        parameters.append(entity_id)
    where = " AND ".join(conditions)
    with _connection() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM persona_audit_log
            WHERE {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            [*parameters, safe_limit],
        ).fetchall()
    return [_audit_from_row(row) for row in rows]


def _material_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "character_id": row["character_id"],
        "source_type": row["source_type"],
        "content": row["content"],
        "source_time": row["source_time"],
        "import_meta": _parse_json(row["import_meta_json"], {}),
        "reliability": row["reliability"],
        "scope": row["scope"],
        "legacy_corpus_id": row["legacy_corpus_id"],
        "is_deleted": bool(row["is_deleted"]),
        "created_at": row["created_at"],
        "deleted_at": row["deleted_at"],
    }


def _audit_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "action": row["action"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "actor": row["actor"],
        "payload": _parse_json(row["payload_json"], {}),
        "created_at": row["created_at"],
    }


def list_claims(
    *,
    status: str | None = None,
    include_evidence: bool = False,
) -> list[dict[str, Any]]:
    ensure_dossier_schema()
    character_id = _character_id()
    with _connection() as connection:
        if status:
            rows = connection.execute(
                "SELECT * FROM persona_claim WHERE character_id=? AND status=? ORDER BY created_at DESC",
                (character_id, status),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM persona_claim WHERE character_id=? ORDER BY created_at DESC",
                (character_id,),
            ).fetchall()
        claims = [_claim_from_row(row) for row in rows]
        if include_evidence and claims:
            ids = [claim["id"] for claim in claims]
            placeholders = ",".join("?" for _ in ids)
            evidence_rows = connection.execute(
                f"""
                SELECT e.*, s.content, s.source_type, s.is_deleted
                FROM claim_evidence e
                JOIN source_material s ON s.id=e.source_material_id
                WHERE e.claim_id IN ({placeholders})
                ORDER BY e.created_at
                """,
                ids,
            ).fetchall()
        else:
            evidence_rows = []

    evidence_by_claim: dict[str, list[dict[str, Any]]] = {}
    for row in evidence_rows:
        evidence_by_claim.setdefault(row["claim_id"], []).append(
            {
                "id": row["id"],
                "source_material_id": row["source_material_id"],
                "source_type": row["source_type"],
                "source_is_deleted": bool(row["is_deleted"]),
                "content": row["content"],
                "quote_span": _parse_json(row["quote_span_json"], {}),
                "support_kind": row["support_kind"],
                "evidence_weight": row["evidence_weight"],
            }
        )
    for claim in claims:
        claim["evidence"] = evidence_by_claim.get(claim["id"], [])
    return claims


def _claim_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "facet": row["facet"],
        "claim_key": row["claim_key"],
        "claim_value": row["claim_value"],
        "context": _parse_json(row["context_json"], {}),
        "polarity": row["polarity"],
        "confidence": row["confidence"],
        "status": row["status"],
        "origin": row["origin"],
        "supersedes_claim_id": row["supersedes_claim_id"],
        "created_at": row["created_at"],
        "reviewed_at": row["reviewed_at"],
        "reviewed_by": row["reviewed_by"],
        "review_reason": row["review_reason"],
    }


def _snapshot_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "character_id": row["character_id"],
        "version": row["version"],
        "kind": row["snapshot_kind"],
        "approved_claims_hash": row["approved_claims_hash"],
        "stable_profile": _parse_json(row["stable_profile_json"], {}),
        "channel_styles": _parse_json(row["channel_styles_json"], {}),
        "claim_ids": _parse_json(row["claim_ids_json"], []),
        "note": row["note"],
        "is_active": bool(row["is_active"]),
        "is_read_only": bool(row["is_read_only"]),
        "is_published": bool(row["is_published"]),
        "created_at": row["created_at"],
        "published_at": row["published_at"],
        "activated_at": row["activated_at"],
    }


_REVIEW_TRANSITIONS = {
    "pending": {"approved", "rejected", "superseded"},
    "approved": {"superseded"},
    "rejected": set(),
    "superseded": set(),
}


_CHANNEL_ALIASES = {
    "chat": "wechat_chat",
    "wechat": "wechat_chat",
    "wechat_chat": "wechat_chat",
    "moments": "moments",
    "wechat_moments": "moments",
    "reference": "reference",
    "douyin": "reference",
    "douyin_reference": "reference",
}


def review_persona_claim(
    claim_id: str,
    decision: str,
    *,
    actor: str = "admin",
    reason: str = "",
) -> dict[str, Any]:
    """将候选结论推进到审核终态，已发布快照不会被隐式改变。"""
    if decision not in {"approved", "rejected", "superseded"}:
        raise ValueError("审核结论必须是 approved、rejected 或 superseded")
    normalized_reason = str(reason or "").strip()
    if len(normalized_reason) > 500:
        raise ValueError("审核原因不能超过 500 个字符")

    ensure_dossier_schema()
    character_id = _character_id()
    with _connection() as connection:
        row = connection.execute(
            "SELECT * FROM persona_claim WHERE id=? AND character_id=?",
            (claim_id, character_id),
        ).fetchone()
        if not row:
            raise KeyError("未找到当前角色的候选结论")

        previous_status = row["status"]
        if decision not in _REVIEW_TRANSITIONS[previous_status]:
            raise ValueError(f"结论不能从 {previous_status} 变更为 {decision}")
        if decision == "approved":
            evidence = connection.execute(
                """
                SELECT 1
                FROM claim_evidence e
                JOIN source_material s ON s.id=e.source_material_id
                WHERE e.claim_id=? AND s.character_id=? AND s.is_deleted=0
                LIMIT 1
                """,
                (claim_id, character_id),
            ).fetchone()
            if not evidence:
                raise ValueError("批准结论前必须关联至少一条未归档的原始素材")

        connection.execute(
            """
            UPDATE persona_claim
            SET status=?, reviewed_at=datetime('now'), reviewed_by=?, review_reason=?
            WHERE id=? AND character_id=?
            """,
            (decision, actor, normalized_reason, claim_id, character_id),
        )
        _audit(
            connection,
            action="review",
            entity_type="persona_claim",
            entity_id=claim_id,
            actor=actor,
            payload={
                "from_status": previous_status,
                "to_status": decision,
                "reason": normalized_reason,
            },
        )
        updated = connection.execute(
            "SELECT * FROM persona_claim WHERE id=?", (claim_id,)
        ).fetchone()
    return _claim_from_row(updated)


def replace_persona_claim(
    claim_id: str,
    *,
    facet: str,
    claim_key: str,
    claim_value: str,
    context: Mapping[str, Any] | None = None,
    polarity: str = "supports",
    confidence: float = 0.5,
    actor: str = "admin",
) -> dict[str, Any]:
    """以新的待审核结论替代旧结论，证据必须由调用方显式重新关联。"""
    ensure_dossier_schema()
    character_id = _character_id()
    replacement_id = str(uuid.uuid4())
    with _connection() as connection:
        previous = connection.execute(
            "SELECT status FROM persona_claim WHERE id=? AND character_id=?",
            (claim_id, character_id),
        ).fetchone()
        if not previous:
            raise KeyError("未找到当前角色的候选结论")
        if previous["status"] not in {"pending", "approved"}:
            raise ValueError("只能替代待审核或已批准的结论")

        connection.execute(
            """
            UPDATE persona_claim
            SET status='superseded', reviewed_at=datetime('now'), reviewed_by=?,
                review_reason='replaced_by_new_candidate'
            WHERE id=? AND character_id=?
            """,
            (actor, claim_id, character_id),
        )
        connection.execute(
            """
            INSERT INTO persona_claim
                (id, character_id, facet, claim_key, claim_value, context_json,
                 polarity, confidence, status, origin, supersedes_claim_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'manual_replacement', ?)
            """,
            (
                replacement_id,
                character_id,
                facet,
                claim_key,
                claim_value,
                _json(dict(context or {})),
                polarity,
                confidence,
                claim_id,
            ),
        )
        _audit(
            connection,
            action="supersede",
            entity_type="persona_claim",
            entity_id=claim_id,
            actor=actor,
            payload={"replacement_claim_id": replacement_id},
        )
        _audit(
            connection,
            action="create",
            entity_type="persona_claim",
            entity_id=replacement_id,
            actor=actor,
            payload={"origin": "manual_replacement", "supersedes_claim_id": claim_id},
        )
        replacement = connection.execute(
            "SELECT * FROM persona_claim WHERE id=?", (replacement_id,)
        ).fetchone()
    return _claim_from_row(replacement)


def create_reviewed_snapshot(
    *,
    note: str = "",
    actor: str = "admin",
) -> dict[str, Any]:
    """冻结已批准结论为已发布、只读但尚未启用的新快照。"""
    normalized_note = str(note or "").strip()
    if len(normalized_note) > 500:
        raise ValueError("快照说明不能超过 500 个字符")

    ensure_dossier_schema()
    character_id = _character_id()
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM persona_claim
            WHERE character_id=? AND status='approved' AND polarity='supports'
            ORDER BY facet, claim_key, id
            """,
            (character_id,),
        ).fetchall()
        if not rows:
            raise ValueError("至少需要一条已批准且支持运行时的结论才能发布快照")

        stable_profile, channel_styles, claim_ids = _compile_snapshot_payload(rows)
        snapshot_payload = {
            "stable_profile": stable_profile,
            "channel_styles": channel_styles,
            "claim_ids": claim_ids,
        }
        version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM persona_snapshot WHERE character_id=?",
            (character_id,),
        ).fetchone()[0]
        snapshot_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO persona_snapshot
                (id, character_id, version, snapshot_kind, approved_claims_hash,
                 stable_profile_json, channel_styles_json, claim_ids_json, note,
                 is_active, is_read_only, is_published, published_at)
            VALUES (?, ?, ?, 'reviewed', ?, ?, ?, ?, ?, 0, 1, 1, datetime('now'))
            """,
            (
                snapshot_id,
                character_id,
                version,
                _content_hash(_json(snapshot_payload)),
                _json(stable_profile),
                _json(channel_styles),
                _json(claim_ids),
                normalized_note,
            ),
        )
        _audit(
            connection,
            action="publish",
            entity_type="persona_snapshot",
            entity_id=snapshot_id,
            actor=actor,
            payload={"version": version, "claim_ids": claim_ids, "is_active": False},
        )
        snapshot = connection.execute(
            "SELECT * FROM persona_snapshot WHERE id=?", (snapshot_id,)
        ).fetchone()
    return _snapshot_from_row(snapshot)


def create_edited_snapshot(
    base_snapshot_id: str,
    *,
    stable_profile: Mapping[str, str],
    channel_styles: Mapping[str, Any] | None = None,
    note: str = "",
    actor: str = "admin",
) -> dict[str, Any]:
    """基于任一历史版本创建新的只读版本，不原地改写历史。"""
    normalized_note = str(note or "").strip()
    if not normalized_note:
        raise ValueError("请填写本次人设修改说明")
    if len(normalized_note) > 500:
        raise ValueError("快照说明不能超过 500 个字符")
    normalized_profile = {
        str(key).strip(): str(value).strip()
        for key, value in stable_profile.items()
        if str(key).strip() and str(value).strip()
    }
    if not normalized_profile:
        raise ValueError("人格事实不能为空")

    ensure_dossier_schema()
    character_id = _character_id()
    with _connection() as connection:
        base = connection.execute(
            "SELECT * FROM persona_snapshot WHERE id=? AND character_id=?",
            (base_snapshot_id, character_id),
        ).fetchone()
        if not base:
            raise KeyError("未找到当前角色的历史人格版本")
        if not base["is_published"] or not base["is_read_only"]:
            raise ValueError("只能基于已经发布的历史人格版本编辑")

        normalized_styles = (
            dict(channel_styles)
            if channel_styles is not None
            else _parse_json(base["channel_styles_json"], {})
        )
        claim_ids = _parse_json(base["claim_ids_json"], [])
        snapshot_payload = {
            "stable_profile": normalized_profile,
            "channel_styles": normalized_styles,
            "claim_ids": claim_ids,
            "manual_edit_base": base_snapshot_id,
        }
        version = int(connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM persona_snapshot WHERE character_id=?",
            (character_id,),
        ).fetchone()[0])
        snapshot_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO persona_snapshot
                (id, character_id, version, snapshot_kind, approved_claims_hash,
                 stable_profile_json, channel_styles_json, claim_ids_json, note,
                 is_active, is_read_only, is_published, published_at)
            VALUES (?, ?, ?, 'reviewed', ?, ?, ?, ?, ?, 0, 1, 1, datetime('now'))
            """,
            (
                snapshot_id,
                character_id,
                version,
                _content_hash(_json(snapshot_payload)),
                _json(normalized_profile),
                _json(normalized_styles),
                _json(claim_ids),
                normalized_note,
            ),
        )
        _audit(
            connection,
            action="manual_edit_publish",
            entity_type="persona_snapshot",
            entity_id=snapshot_id,
            actor=actor,
            payload={
                "version": version,
                "base_snapshot_id": base_snapshot_id,
                "changed_profile_keys": sorted(
                    key
                    for key in set(_parse_json(base["stable_profile_json"], {})) | set(normalized_profile)
                    if _parse_json(base["stable_profile_json"], {}).get(key) != normalized_profile.get(key)
                ),
                "is_active": False,
            },
        )
        snapshot = connection.execute(
            "SELECT * FROM persona_snapshot WHERE id=?", (snapshot_id,)
        ).fetchone()
    return _snapshot_from_row(snapshot)


def activate_snapshot(snapshot_id: str, *, actor: str = "admin") -> dict[str, Any]:
    """启用当前角色的已发布只读版本；目标低于当前版本即为回滚。"""
    ensure_dossier_schema()
    character_id = _character_id()
    with _connection() as connection:
        row = connection.execute(
            "SELECT * FROM persona_snapshot WHERE id=? AND character_id=?",
            (snapshot_id, character_id),
        ).fetchone()
        if not row:
            raise KeyError("未找到当前角色的人格快照")
        if not row["is_published"] or not row["is_read_only"]:
            raise ValueError("只能启用已发布且只读的人格快照")

        previous = connection.execute(
            """
            SELECT version FROM persona_snapshot
            WHERE character_id=? AND is_active=1
            ORDER BY version DESC LIMIT 1
            """,
            (character_id,),
        ).fetchone()
        previous_version = int(previous["version"]) if previous else None
        connection.execute(
            "UPDATE persona_snapshot SET is_active=0 WHERE character_id=? AND is_active=1",
            (character_id,),
        )
        connection.execute(
            """
            UPDATE persona_snapshot
            SET is_active=1, activated_at=datetime('now')
            WHERE id=? AND character_id=?
            """,
            (snapshot_id, character_id),
        )
        _audit(
            connection,
            action="activate",
            entity_type="persona_snapshot",
            entity_id=snapshot_id,
            actor=actor,
            payload={
                "from_version": previous_version,
                "to_version": row["version"],
                "transition": "rollback" if previous_version and row["version"] < previous_version else "activate",
            },
        )
        activated = connection.execute(
            "SELECT * FROM persona_snapshot WHERE id=?", (snapshot_id,)
        ).fetchone()
    return _snapshot_from_row(activated)


def list_snapshots() -> list[dict[str, Any]]:
    ensure_dossier_schema()
    with _connection() as connection:
        rows = connection.execute(
            "SELECT * FROM persona_snapshot WHERE character_id=? ORDER BY version DESC",
            (_character_id(),),
        ).fetchall()
    return [_snapshot_from_row(row) for row in rows]


def diff_snapshots(from_snapshot_id: str, to_snapshot_id: str) -> dict[str, Any]:
    """比较当前角色的两个不可变快照，不修改活跃快照或候选结论。"""
    ensure_dossier_schema()
    character_id = _character_id()
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM persona_snapshot
            WHERE character_id=? AND id IN (?, ?)
            """,
            (character_id, from_snapshot_id, to_snapshot_id),
        ).fetchall()
    by_id = {str(row["id"]): _snapshot_from_row(row) for row in rows}
    missing = [
        snapshot_id
        for snapshot_id in (from_snapshot_id, to_snapshot_id)
        if snapshot_id not in by_id
    ]
    if missing:
        raise KeyError("未找到当前角色的人格快照")

    before = by_id[from_snapshot_id]
    after = by_id[to_snapshot_id]
    before_profile = before["stable_profile"]
    after_profile = after["stable_profile"]
    before_styles = before["channel_styles"]
    after_styles = after["channel_styles"]
    before_claims = set(str(item) for item in before["claim_ids"])
    after_claims = set(str(item) for item in after["claim_ids"])
    return {
        "from": _snapshot_diff_reference(before),
        "to": _snapshot_diff_reference(after),
        "stable_profile": _mapping_diff(before_profile, after_profile),
        "channel_styles": _mapping_diff(before_styles, after_styles),
        "claim_ids": {
            "added": sorted(after_claims - before_claims),
            "removed": sorted(before_claims - after_claims),
            "unchanged": sorted(before_claims & after_claims),
        },
        "has_changes": bool(
            _mapping_diff(before_profile, after_profile)
            or _mapping_diff(before_styles, after_styles)
            or before_claims != after_claims
        ),
    }


def _snapshot_diff_reference(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": snapshot["id"],
        "version": snapshot["version"],
        "kind": snapshot["kind"],
        "note": snapshot["note"],
        "is_active": snapshot["is_active"],
        "is_published": snapshot["is_published"],
        "is_read_only": snapshot["is_read_only"],
        "created_at": snapshot["created_at"],
        "published_at": snapshot["published_at"],
        "activated_at": snapshot["activated_at"],
    }


def _mapping_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        previous = before.get(key)
        current = after.get(key)
        if previous != current:
            changes.append({"key": key, "before": previous, "after": current})
    return changes


def _compile_snapshot_payload(
    rows: list[sqlite3.Row],
) -> tuple[dict[str, str], dict[str, dict[str, Any]], list[str]]:
    return compile_claim_payload([_claim_from_row(row) for row in rows])


def compile_claim_payload(
    claims: list[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, dict[str, Any]], list[str]]:
    """把明确选择的结论编译为快照载荷；不会读取或修改数据库。"""
    stable_profile: dict[str, str] = {}
    stable_values: dict[str, str] = {}
    style_observations: dict[str, list[dict[str, Any]]] = {}
    style_claim_ids: dict[str, list[str]] = {}
    explicit_styles: dict[str, dict[str, Any]] = {}
    claim_ids: list[str] = []

    for source_claim in claims:
        claim = dict(source_claim)
        context = claim.get("context")
        if not isinstance(context, Mapping):
            context = {}
        channel_name = context.get("channel") if claim["facet"] == "communication" else None
        if channel_name:
            channel = _CHANNEL_ALIASES.get(str(channel_name))
            if not channel:
                raise ValueError(f"不支持的沟通渠道：{channel_name}")
            payload = _style_payload(claim)
            if claim["claim_key"] in {"style", "style_profile", "channel_style"}:
                style_observations.setdefault(channel, []).append(payload)
            else:
                explicit = explicit_styles.setdefault(channel, {})
                for key, value in payload.items():
                    current = explicit.get(key)
                    if current is not None and current != value:
                        raise ValueError(f"渠道 {channel} 的风格字段 {key} 存在冲突，请先替代其中一个结论")
                    explicit[key] = value
            style_claim_ids.setdefault(channel, []).append(claim["id"])
        else:
            previous_value = stable_values.get(claim["claim_key"])
            if previous_value is not None and previous_value != claim["claim_value"]:
                raise ValueError(f"稳定人格字段 {claim['claim_key']} 存在冲突，请先替代其中一个结论")
            stable_values[claim["claim_key"]] = claim["claim_value"]
            stable_profile[claim["claim_key"]] = claim["claim_value"]
        claim_ids.append(claim["id"])

    channel_styles: dict[str, dict[str, Any]] = {}
    for channel in sorted(set(style_observations) | set(explicit_styles)):
        merged = merge_styles(style_observations.get(channel, []))
        for key, value in explicit_styles.get(channel, {}).items():
            current = merged.get(key)
            if current not in (None, "", []) and current != value:
                raise ValueError(f"渠道 {channel} 的风格字段 {key} 存在冲突，请先替代其中一个结论")
            merged[key] = value
        channel_styles[channel] = {
            "source": "reviewed",
            "style": merged,
            "claim_ids": style_claim_ids[channel],
        }

    return stable_profile, channel_styles, claim_ids


def _style_payload(claim: Mapping[str, Any]) -> dict[str, Any]:
    if claim["claim_key"] in {"style", "style_profile", "channel_style"}:
        payload = _parse_json(str(claim["claim_value"]), None)
        if isinstance(payload, dict):
            return payload
        raise ValueError("渠道风格结论必须是 JSON 对象")
    return {str(claim["claim_key"]): claim["claim_value"]}
