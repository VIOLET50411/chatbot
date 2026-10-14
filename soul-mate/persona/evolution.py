"""单人设的慢速自动演变守卫、发布与回滚。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from bridge.v1_store import BridgeStore
from core.character_context import CharacterContextNotBoundError, require_character_context
from scripts.character_registry import get_single_character


MIN_EVIDENCE_COUNT = 3
MIN_EVIDENCE_SPAN = timedelta(days=14)
MIN_CONFIDENCE = 0.85
MIN_PUBLISH_INTERVAL = timedelta(days=7)

_ALLOWED_DIMENSIONS = {
    "preferences",
    "opinions",
    "habits",
    "boundaries",
    "personality_weights",
}
_COMMAND_SOURCE_KINDS = {"user_command", "persona_change_command"}


class EvolutionPolicyError(ValueError):
    """候选或发布动作不满足自动人格演变策略。"""


@dataclass(frozen=True)
class EvolutionEvidence:
    source_id: str
    occurred_at: datetime
    source_kind: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvolutionPolicyError("证据时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value)).astimezone(timezone.utc)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise EvolutionPolicyError("人格演变值必须是可序列化的 JSON") from exc


def _parse_json(value: object, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _hash_snapshot(stable_profile: dict[str, Any], channel_styles: dict[str, Any], claim_ids: list[Any]) -> str:
    payload = _json(
        {
            "stable_profile": stable_profile,
            "channel_styles": channel_styles,
            "claim_ids": claim_ids,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "dimension": str(row["dimension"]),
        "before": _parse_json(row["before_json"], None),
        "after": _parse_json(row["after_json"], None),
        "evidence_ids": _parse_json(row["evidence_ids_json"], []),
        "confidence": float(row["confidence"]),
        "evidence_start_at": str(row["evidence_start_at"]),
        "evidence_end_at": str(row["evidence_end_at"]),
        "status": str(row["status"]),
        "reason": str(row["reason"]),
        "baseline_snapshot_id": str(row["baseline_snapshot_id"] or ""),
        "result_snapshot_id": str(row["result_snapshot_id"] or ""),
        "trigger_kind": str(row["trigger_kind"]),
        "created_at": str(row["created_at"]),
        "published_at": str(row["published_at"] or ""),
        "rolled_back_at": str(row["rolled_back_at"] or ""),
    }


class PersonaEvolutionService:
    """所有自动演变都必须经过此服务；默认状态为暂停。"""

    def __init__(
        self,
        store: BridgeStore | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.store = store or BridgeStore.from_environment()
        self.clock = clock
        self.store.initialize_schema()

    @property
    def character_id(self) -> str:
        try:
            return require_character_context().character_id
        except CharacterContextNotBoundError:
            return str(get_single_character().get("id") or "default")

    def get_control(self) -> dict[str, Any]:
        with self.store._connection() as connection:
            row = connection.execute(
                "SELECT paused, reason, updated_by, updated_at FROM persona_evolution_control WHERE id=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("人格演变控制记录缺失")
        return {
            "paused": bool(row["paused"]),
            "reason": str(row["reason"]),
            "updated_by": str(row["updated_by"]),
            "updated_at": str(row["updated_at"]),
        }

    def set_paused(self, paused: bool, *, reason: str, actor: str) -> dict[str, Any]:
        normalized_reason = str(reason or "").strip()
        if len(normalized_reason) < 3 or len(normalized_reason) > 500:
            raise EvolutionPolicyError("暂停状态变更必须提供 3–500 字原因")
        normalized_actor = str(actor or "system").strip()[:128] or "system"
        with self.store._connection(immediate=True) as connection:
            if not paused:
                pending_materials = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM material_reviews WHERE decision='pending'"
                    ).fetchone()[0]
                )
                if pending_materials:
                    raise EvolutionPolicyError(
                        f"仍有 {pending_materials} 份素材待审核，不能解除人格演变暂停"
                    )
                baseline = connection.execute(
                    """
                    SELECT status, result_snapshot_id
                    FROM persona_baseline_drafts
                    WHERE character_id=?
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (self.character_id,),
                ).fetchone()
                snapshot_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='persona_snapshot'"
                ).fetchone()
                active = (
                    connection.execute(
                        """
                        SELECT id FROM persona_snapshot
                        WHERE character_id=? AND is_active=1 AND is_published=1 AND is_read_only=1
                        ORDER BY version DESC LIMIT 1
                        """,
                        (self.character_id,),
                    ).fetchone()
                    if snapshot_table is not None
                    else None
                )
                if (
                    baseline is None
                    or str(baseline["status"]) != "activated"
                    or active is None
                    or str(active["id"]) != str(baseline["result_snapshot_id"] or "")
                ):
                    raise EvolutionPolicyError("审核后新人格尚未确认激活，不能解除人格演变暂停")
            connection.execute(
                """
                UPDATE persona_evolution_control
                SET paused=?, reason=?, updated_by=?, updated_at=?
                WHERE id=1
                """,
                (int(paused), normalized_reason, normalized_actor, _iso(self.clock())),
            )
        return self.get_control()

    def create_candidate(
        self,
        *,
        dimension: str,
        after: Any,
        evidence: Iterable[EvolutionEvidence],
        confidence: float,
        reason: str,
    ) -> dict[str, Any]:
        if self.get_control()["paused"]:
            raise EvolutionPolicyError("人格自动演变当前已暂停")
        normalized_dimension = str(dimension or "").strip()
        if normalized_dimension not in _ALLOWED_DIMENSIONS:
            raise EvolutionPolicyError("身份事实和未授权维度不可自动演变")
        if float(confidence) < MIN_CONFIDENCE or float(confidence) > 1:
            raise EvolutionPolicyError("人格演变置信度不得低于 0.85")
        normalized_reason = str(reason or "").strip()
        if len(normalized_reason) < 3 or len(normalized_reason) > 500:
            raise EvolutionPolicyError("人格演变必须提供 3–500 字原因")

        evidence_by_id: dict[str, EvolutionEvidence] = {}
        for item in evidence:
            source_id = str(item.source_id or "").strip()
            source_kind = str(item.source_kind or "").strip()
            if not source_id or not source_kind:
                raise EvolutionPolicyError("演变证据必须包含来源 ID 和来源类型")
            _iso(item.occurred_at)
            evidence_by_id[source_id] = item
        items = list(evidence_by_id.values())
        if len(items) < MIN_EVIDENCE_COUNT:
            raise EvolutionPolicyError("人格演变至少需要 3 份独立证据")
        start_at = min(item.occurred_at.astimezone(timezone.utc) for item in items)
        end_at = max(item.occurred_at.astimezone(timezone.utc) for item in items)
        if end_at - start_at < MIN_EVIDENCE_SPAN:
            raise EvolutionPolicyError("人格演变证据跨度不得少于 14 天")
        if all(str(item.source_kind).strip() in _COMMAND_SOURCE_KINDS for item in items):
            raise EvolutionPolicyError("用户命令不能单独触发人格演变")

        serialized_after = _json(after)
        with self.store._connection(immediate=True) as connection:
            snapshot = connection.execute(
                """
                SELECT id, stable_profile_json FROM persona_snapshot
                WHERE character_id=? AND is_active=1 AND is_published=1 AND is_read_only=1
                ORDER BY version DESC LIMIT 1
                """,
                (self.character_id,),
            ).fetchone()
            if snapshot is None:
                raise EvolutionPolicyError("没有可作为演变基线的活动只读人格快照")
            profile = _parse_json(snapshot["stable_profile_json"], {})
            if not isinstance(profile, dict):
                raise EvolutionPolicyError("活动人格快照格式无效")
            before = profile.get(normalized_dimension)
            if _json(before) == serialized_after:
                raise EvolutionPolicyError("候选值与当前人格没有差异")
            event_id = str(uuid.uuid4())
            now = _iso(self.clock())
            connection.execute(
                """
                INSERT INTO persona_evolution_events(
                    id, dimension, before_json, after_json, evidence_ids_json,
                    confidence, evidence_start_at, evidence_end_at, status,
                    reason, created_at, baseline_snapshot_id, trigger_kind
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, 'automatic')
                """,
                (
                    event_id,
                    normalized_dimension,
                    _json(before),
                    serialized_after,
                    _json(sorted(evidence_by_id)),
                    float(confidence),
                    _iso(start_at),
                    _iso(end_at),
                    normalized_reason,
                    now,
                    str(snapshot["id"]),
                ),
            )
            row = connection.execute(
                "SELECT * FROM persona_evolution_events WHERE id=?", (event_id,)
            ).fetchone()
        return _event_from_row(row)

    def publish_candidate(self, event_id: str, *, actor: str = "system") -> dict[str, Any]:
        if self.get_control()["paused"]:
            raise EvolutionPolicyError("人格自动演变当前已暂停")
        now = self.clock().astimezone(timezone.utc)
        with self.store._connection(immediate=True) as connection:
            event = connection.execute(
                "SELECT * FROM persona_evolution_events WHERE id=?", (str(event_id),)
            ).fetchone()
            if event is None:
                raise KeyError("未找到人格演变事件")
            if event["status"] != "candidate":
                raise EvolutionPolicyError("只能发布候选状态的人格演变")
            if str(event["dimension"]) not in _ALLOWED_DIMENSIONS:
                raise EvolutionPolicyError("身份事实和未授权维度不可自动演变")
            if float(event["confidence"]) < MIN_CONFIDENCE:
                raise EvolutionPolicyError("人格演变置信度不得低于 0.85")
            start_at = _parse_time(event["evidence_start_at"])
            end_at = _parse_time(event["evidence_end_at"])
            evidence_ids = _parse_json(event["evidence_ids_json"], [])
            if (
                start_at is None
                or end_at is None
                or end_at - start_at < MIN_EVIDENCE_SPAN
                or not isinstance(evidence_ids, list)
                or len(set(str(item) for item in evidence_ids)) < MIN_EVIDENCE_COUNT
            ):
                raise EvolutionPolicyError("候选事件不再满足证据门槛")
            latest = connection.execute(
                """
                SELECT published_at FROM persona_evolution_events
                WHERE status='published' AND published_at IS NOT NULL
                ORDER BY published_at DESC LIMIT 1
                """
            ).fetchone()
            latest_at = _parse_time(latest["published_at"]) if latest else None
            if latest_at is not None and now - latest_at < MIN_PUBLISH_INTERVAL:
                raise EvolutionPolicyError("人格自动演变最多每 7 天发布一个版本")

            active = connection.execute(
                """
                SELECT * FROM persona_snapshot
                WHERE character_id=? AND is_active=1 AND is_published=1 AND is_read_only=1
                ORDER BY version DESC LIMIT 1
                """,
                (self.character_id,),
            ).fetchone()
            if active is None or str(active["id"]) != str(event["baseline_snapshot_id"]):
                raise EvolutionPolicyError("人格基线已经变化，请重新收集和评估证据")
            profile = _parse_json(active["stable_profile_json"], {})
            channel_styles = _parse_json(active["channel_styles_json"], {})
            claim_ids = _parse_json(active["claim_ids_json"], [])
            if not isinstance(profile, dict) or not isinstance(channel_styles, dict) or not isinstance(claim_ids, list):
                raise EvolutionPolicyError("活动人格快照格式无效")
            profile[str(event["dimension"])] = _parse_json(event["after_json"], None)
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM persona_snapshot WHERE character_id=?",
                    (self.character_id,),
                ).fetchone()[0]
            )
            snapshot_id = str(uuid.uuid4())
            now_text = _iso(now)
            connection.execute(
                "UPDATE persona_snapshot SET is_active=0 WHERE character_id=? AND is_active=1",
                (self.character_id,),
            )
            connection.execute(
                """
                INSERT INTO persona_snapshot(
                    id, character_id, version, snapshot_kind, approved_claims_hash,
                    stable_profile_json, channel_styles_json, claim_ids_json, note,
                    is_active, is_read_only, is_published, created_at, published_at, activated_at
                )
                VALUES (?, ?, ?, 'reviewed', ?, ?, ?, ?, ?, 1, 1, 1, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    self.character_id,
                    version,
                    _hash_snapshot(profile, channel_styles, claim_ids),
                    _json(profile),
                    _json(channel_styles),
                    _json(claim_ids),
                    f"自动演变：{str(event['reason'])}"[:500],
                    now_text,
                    now_text,
                    now_text,
                ),
            )
            connection.execute(
                """
                UPDATE persona_evolution_events
                SET status='published', result_snapshot_id=?, published_at=?
                WHERE id=?
                """,
                (snapshot_id, now_text, str(event_id)),
            )
            self._audit(
                connection,
                action="persona_evolution.publish",
                entity_id=str(event_id),
                actor=actor,
                payload={
                    "dimension": str(event["dimension"]),
                    "from_snapshot_id": str(active["id"]),
                    "to_snapshot_id": snapshot_id,
                    "version": version,
                    "reason": str(event["reason"]),
                },
            )
            row = connection.execute(
                "SELECT * FROM persona_evolution_events WHERE id=?", (str(event_id),)
            ).fetchone()
        return _event_from_row(row)

    def rollback(self, event_id: str, *, actor: str) -> dict[str, Any]:
        with self.store._connection(immediate=True) as connection:
            event = connection.execute(
                "SELECT * FROM persona_evolution_events WHERE id=?", (str(event_id),)
            ).fetchone()
            if event is None:
                raise KeyError("未找到人格演变事件")
            if event["status"] != "published":
                raise EvolutionPolicyError("只能回滚已发布且尚未回滚的人格演变")
            result_id = str(event["result_snapshot_id"] or "")
            baseline_id = str(event["baseline_snapshot_id"] or "")
            result = connection.execute(
                "SELECT is_active FROM persona_snapshot WHERE id=? AND character_id=?",
                (result_id, self.character_id),
            ).fetchone()
            baseline = connection.execute(
                """
                SELECT id FROM persona_snapshot
                WHERE id=? AND character_id=? AND is_published=1 AND is_read_only=1
                """,
                (baseline_id, self.character_id),
            ).fetchone()
            if result is None or not bool(result["is_active"]) or baseline is None:
                raise EvolutionPolicyError("该演变版本已不是当前活动版本，不能直接回滚")
            now = _iso(self.clock())
            connection.execute(
                "UPDATE persona_snapshot SET is_active=0 WHERE character_id=? AND is_active=1",
                (self.character_id,),
            )
            connection.execute(
                "UPDATE persona_snapshot SET is_active=1, activated_at=? WHERE id=?",
                (now, baseline_id),
            )
            connection.execute(
                """
                UPDATE persona_evolution_events
                SET status='rolled_back', rolled_back_at=?
                WHERE id=?
                """,
                (now, str(event_id)),
            )
            self._audit(
                connection,
                action="persona_evolution.rollback",
                entity_id=str(event_id),
                actor=actor,
                payload={"from_snapshot_id": result_id, "to_snapshot_id": baseline_id},
            )
            row = connection.execute(
                "SELECT * FROM persona_evolution_events WHERE id=?", (str(event_id),)
            ).fetchone()
        return _event_from_row(row)

    def list_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM persona_evolution_events ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        entity_id: str,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO persona_audit_log(id, character_id, action, entity_type, entity_id, actor, payload_json, created_at)
            VALUES (?, ?, ?, 'persona_evolution', ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                self.character_id,
                action,
                entity_id,
                str(actor or "system")[:128],
                _json(payload),
                _iso(self.clock()),
            ),
        )
