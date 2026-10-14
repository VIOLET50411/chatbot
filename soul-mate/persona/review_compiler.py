"""把 Owner 的素材审核结果编译为可预览、需确认后才激活的人格基线。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from bridge.v1_store import BridgeStore
from core.character_context import CharacterContextNotBoundError, require_character_context
from persona.dossier import compile_claim_payload
from scripts.character_registry import get_single_character


class BaselineCompileError(ValueError):
    """审核结果不完整、已变化或无法安全编译。"""


@dataclass(frozen=True, slots=True)
class BaselineResolution:
    contact_id: str
    addressing_style: str
    punctuation_style: str
    relationship_baseline: str
    known_partner_status: str
    education_goal: str
    education_disclosure: str
    moments_excluded_are_style_evidence: bool
    pet_status: str
    last_shared_context: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BaselineResolution":
        text_fields = (
            "contact_id",
            "addressing_style",
            "punctuation_style",
            "relationship_baseline",
            "known_partner_status",
            "education_goal",
            "education_disclosure",
            "pet_status",
            "last_shared_context",
        )
        normalized = {field: str(value.get(field) or "").strip() for field in text_fields}
        missing = [field for field, content in normalized.items() if not content]
        if missing:
            raise BaselineCompileError(f"人格澄清缺少字段：{', '.join(missing)}")
        for field, content in normalized.items():
            if len(content) > 2_000:
                raise BaselineCompileError(f"人格澄清字段过长：{field}")
        return cls(
            **normalized,
            moments_excluded_are_style_evidence=bool(
                value.get("moments_excluded_are_style_evidence", False)
            ),
        )


_NAMESPACE = uuid.UUID("5079f083-331a-4df5-b72e-b565c614fe6c")
_CONTACT_SCOPED_KEYS = {
    "current_contact_status",
    "relationship_to_user",
    "relationship_with_user",
    "nickname_for_user",
}
_REBUILT_KEYS = {
    "academic",
    "behavior_rules",
    "chat_style_profile",
    "language_style_chat",
    "language_style_moments",
    "moments_style_profile",
    "personality",
    "pet",
    "speaking_style",
}
_OWNER_CONFIRMATION_REVIEW_NOTE = "Owner 对审核缺口的人工确认"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json(value: object, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _stable_id(*parts: object) -> str:
    return str(uuid.uuid5(_NAMESPACE, "|".join(str(part) for part in parts)))


def _field_content(content: object) -> tuple[str, str]:
    text = str(content or "").strip()
    if ":" not in text:
        return "", text
    key, value = text.split(":", 1)
    normalized_key = key.strip()
    if not normalized_key or not normalized_key.replace("_", "").isalnum():
        return "", text
    return normalized_key, value.strip()


def _mapping_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"key": key, "before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]


class PersonaReviewCompiler:
    """审核表是唯一输入；预览不改变当前活动快照或联系人画像。"""

    def __init__(self, store: BridgeStore | None = None) -> None:
        self.store = store or BridgeStore.from_environment()
        self.store.initialize_schema()

    @property
    def character_id(self) -> str:
        try:
            return require_character_context().character_id
        except CharacterContextNotBoundError:
            return str(get_single_character().get("id") or "default")

    def create_draft(
        self,
        resolution: BaselineResolution | Mapping[str, Any],
        *,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        resolved = (
            resolution
            if isinstance(resolution, BaselineResolution)
            else BaselineResolution.from_mapping(resolution)
        )
        normalized_note = str(note or "").strip()
        if len(normalized_note) > 500:
            raise BaselineCompileError("基线说明不能超过 500 个字符")
        actor_name = str(actor or "admin").strip()[:128] or "admin"
        now_text = _iso(_now())
        draft_id = str(uuid.uuid4())

        with self.store._connection(immediate=True) as connection:
            self._assert_contact(connection, resolved.contact_id)
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM material_reviews WHERE decision='pending'"
                ).fetchone()[0]
            )
            if pending:
                raise BaselineCompileError(f"仍有 {pending} 份素材待审核，不能生成基线预览")
            active = self._active_snapshot(connection)
            fingerprint = self._review_fingerprint(connection)
            selected, derived, supersede = self._compile_claim_selection(
                connection,
                resolved,
                draft_id=draft_id,
            )
            all_claims = [*selected, *derived]
            stable_profile, channel_styles, _ = compile_claim_payload(all_claims)
            selected_ids = [str(claim["id"]) for claim in selected]
            contact_rows = connection.execute(
                "SELECT key, value FROM contact_profiles WHERE contact_id=? ORDER BY key",
                (resolved.contact_id,),
            ).fetchall()
            contact_delete_keys = [str(row["key"]) for row in contact_rows]
            contact_profile = self._contact_profile(resolved)

            connection.execute(
                """
                UPDATE persona_baseline_drafts
                SET status='stale', updated_at=?
                WHERE character_id=? AND status='draft'
                """,
                (now_text, self.character_id),
            )
            connection.execute(
                """
                INSERT INTO persona_baseline_drafts(
                    id, character_id, baseline_snapshot_id, review_fingerprint,
                    resolution_json, stable_profile_json, channel_styles_json,
                    selected_claim_ids_json, derived_claims_json,
                    supersede_claim_ids_json, contact_id,
                    contact_delete_keys_json, contact_profile_json, status,
                    note, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    self.character_id,
                    str(active["id"]),
                    fingerprint,
                    _json(asdict(resolved)),
                    _json(stable_profile),
                    _json(channel_styles),
                    _json(selected_ids),
                    _json(derived),
                    _json(sorted(supersede)),
                    resolved.contact_id,
                    _json(contact_delete_keys),
                    _json(contact_profile),
                    normalized_note,
                    actor_name,
                    now_text,
                    now_text,
                ),
            )

        return self.get_state()

    def get_state(self) -> dict[str, Any]:
        with self.store._connection() as connection:
            active = self._active_snapshot(connection)
            summary = {
                str(row["decision"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT decision, COUNT(*) AS count
                    FROM material_reviews
                    WHERE COALESCE(review_note, '') <> ?
                    GROUP BY decision
                    """,
                    (_OWNER_CONFIRMATION_REVIEW_NOTE,),
                ).fetchall()
            }
            owner_confirmation_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM material_reviews WHERE review_note=?",
                    (_OWNER_CONFIRMATION_REVIEW_NOTE,),
                ).fetchone()[0]
            )
            row = connection.execute(
                """
                SELECT * FROM persona_baseline_drafts
                WHERE character_id=?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (self.character_id,),
            ).fetchone()
            draft = self._draft_view(connection, row, active) if row else None
        return {
            "review_summary": summary,
            "owner_confirmation_count": owner_confirmation_count,
            "active_snapshot": self._snapshot_reference(active),
            "draft": draft,
        }

    def activate_draft(self, draft_id: str, *, actor: str) -> dict[str, Any]:
        normalized_id = str(draft_id or "").strip()
        actor_name = str(actor or "admin").strip()[:128] or "admin"
        if not normalized_id:
            raise KeyError("未找到人格基线草稿")
        now_text = _iso(_now())

        with self.store._connection(immediate=True) as connection:
            draft = connection.execute(
                "SELECT * FROM persona_baseline_drafts WHERE id=? AND character_id=?",
                (normalized_id, self.character_id),
            ).fetchone()
            if draft is None:
                raise KeyError("未找到人格基线草稿")
            if str(draft["status"]) != "draft":
                raise BaselineCompileError("只能激活仍处于预览状态的人格基线")
            active = self._active_snapshot(connection)
            if str(active["id"]) != str(draft["baseline_snapshot_id"]):
                self._mark_stale(connection, normalized_id, now_text)
                raise BaselineCompileError("活动人格已变化，请重新生成差异预览")
            if self._review_fingerprint(connection) != str(draft["review_fingerprint"]):
                self._mark_stale(connection, normalized_id, now_text)
                raise BaselineCompileError("素材审核结果已变化，请重新生成差异预览")

            selected_ids = [str(item) for item in _parse_json(draft["selected_claim_ids_json"], [])]
            derived = _parse_json(draft["derived_claims_json"], [])
            supersede_ids = [str(item) for item in _parse_json(draft["supersede_claim_ids_json"], [])]
            if not isinstance(derived, list):
                raise BaselineCompileError("人格基线草稿中的派生结论无效")
            for claim in derived:
                self._materialize_derived_claim(
                    connection,
                    claim,
                    draft_id=normalized_id,
                    actor=actor_name,
                    now_text=now_text,
                )
            if supersede_ids:
                placeholders = ",".join("?" for _ in supersede_ids)
                connection.execute(
                    f"""
                    UPDATE persona_claim
                    SET status='superseded', reviewed_at=?, reviewed_by=?,
                        review_reason='superseded_by_owner_review_baseline'
                    WHERE id IN ({placeholders}) AND character_id=? AND status='approved'
                    """,
                    (now_text, actor_name, *supersede_ids, self.character_id),
                )

            stable_profile = _parse_json(draft["stable_profile_json"], {})
            channel_styles = _parse_json(draft["channel_styles_json"], {})
            derived_ids = [str(item.get("id") or "") for item in derived if isinstance(item, dict)]
            claim_ids = [*selected_ids, *[item for item in derived_ids if item]]
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM persona_snapshot WHERE character_id=?",
                    (self.character_id,),
                ).fetchone()[0]
            )
            snapshot_id = str(uuid.uuid4())
            payload_hash = _hash(
                {
                    "stable_profile": stable_profile,
                    "channel_styles": channel_styles,
                    "claim_ids": claim_ids,
                }
            )
            connection.execute(
                "UPDATE persona_snapshot SET is_active=0 WHERE character_id=? AND is_active=1",
                (self.character_id,),
            )
            connection.execute(
                """
                INSERT INTO persona_snapshot(
                    id, character_id, version, snapshot_kind, approved_claims_hash,
                    stable_profile_json, channel_styles_json, claim_ids_json, note,
                    is_active, is_read_only, is_published, created_at,
                    published_at, activated_at
                ) VALUES (?, ?, ?, 'reviewed', ?, ?, ?, ?, ?, 1, 1, 1, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    self.character_id,
                    version,
                    payload_hash,
                    _json(stable_profile),
                    _json(channel_styles),
                    _json(claim_ids),
                    str(draft["note"] or "Owner 素材审核后生成的人格基线")[:500],
                    now_text,
                    now_text,
                    now_text,
                ),
            )
            self._apply_contact_profile(connection, draft, now_text=now_text)
            connection.execute(
                """
                UPDATE persona_baseline_drafts
                SET status='activated', result_snapshot_id=?, updated_at=?, activated_at=?
                WHERE id=?
                """,
                (snapshot_id, now_text, now_text, normalized_id),
            )
            self._audit(
                connection,
                action="persona_baseline.activate",
                entity_id=normalized_id,
                actor=actor_name,
                payload={
                    "from_snapshot_id": str(active["id"]),
                    "to_snapshot_id": snapshot_id,
                    "version": version,
                },
            )
        return self.get_state()

    def _compile_claim_selection(
        self,
        connection: sqlite3.Connection,
        resolution: BaselineResolution,
        *,
        draft_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        rows = connection.execute(
            """
            SELECT * FROM persona_claim
            WHERE character_id=? AND status='approved' AND polarity='supports'
            ORDER BY facet, claim_key, id
            """,
            (self.character_id,),
        ).fetchall()
        evidence = connection.execute(
            """
            SELECT e.claim_id, e.source_material_id, review.decision,
                   review.corrected_content, review.split_contents_json,
                   source.source_type, source.content
            FROM claim_evidence AS e
            JOIN source_material AS source ON source.id=e.source_material_id
            LEFT JOIN material_reviews AS review ON review.source_material_id=source.id
            JOIN persona_claim AS claim ON claim.id=e.claim_id
            WHERE claim.character_id=? AND source.is_deleted=0
            ORDER BY e.claim_id, e.created_at, e.id
            """,
            (self.character_id,),
        ).fetchall()
        evidence_by_claim: dict[str, list[sqlite3.Row]] = {}
        for item in evidence:
            evidence_by_claim.setdefault(str(item["claim_id"]), []).append(item)

        selected: list[dict[str, Any]] = []
        supersede: set[str] = set()
        approved_by_key: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            approved_by_key.setdefault(str(row["claim_key"]), []).append(row)
            claim_id = str(row["id"])
            key = str(row["claim_key"])
            related = evidence_by_claim.get(claim_id, [])
            decisions = {str(item["decision"] or "") for item in related}
            is_contact_scoped = key in _CONTACT_SCOPED_KEYS or key.startswith("recent_chat_")
            if is_contact_scoped or key in _REBUILT_KEYS:
                supersede.add(claim_id)
                continue
            if "keep" not in decisions:
                supersede.add(claim_id)
                continue
            selected.append(self._claim_record(row))

        corrected_rows = connection.execute(
            """
            SELECT review.id, review.source_material_id, review.corrected_content,
                   source.content
            FROM material_reviews AS review
            JOIN source_material AS source ON source.id=review.source_material_id
            WHERE review.decision='corrected'
            ORDER BY review.id
            """
        ).fetchall()
        corrected: dict[str, sqlite3.Row] = {}
        for item in corrected_rows:
            key, _ = _field_content(item["content"])
            if key:
                corrected[key] = item

        personality_source = corrected.get("personality")
        if personality_source is None:
            raise BaselineCompileError("缺少已修正的 personality 素材")
        _, personality_value = _field_content(personality_source["corrected_content"])
        if not personality_value:
            raise BaselineCompileError("已修正的 personality 内容为空")

        chat_style = self._style_resolution(
            approved_by_key.get("chat_style_profile", []),
            channel="wechat_chat",
        )
        chat_style["common_words"] = [
            item for item in chat_style.get("common_words", []) if str(item) != "老师"
        ]
        chat_style["sentence_style"] = (
            "轻松随意，短句连发，叠字表达情绪；"
            f"{resolution.punctuation_style}；{resolution.addressing_style}。"
        )
        chat_style["punctuation_style"] = resolution.punctuation_style
        chat_style["addressing_style"] = resolution.addressing_style

        moments_style = self._style_resolution(
            approved_by_key.get("moments_style_profile", []),
            channel="moments",
        )
        if not resolution.moments_excluded_are_style_evidence:
            raise BaselineCompileError("排除的朋友圈不能作为风格证据时，需要重新提取朋友圈风格")

        chat_evidence = self._material_ids(connection, channel="chat", include_excluded=False)
        moments_evidence = self._material_ids(
            connection,
            channel="moments",
            include_excluded=resolution.moments_excluded_are_style_evidence,
        )
        behavior_rules = (
            "角色有自主性和自己的生活，不围着任何联系人转；态度由每个联系人的独立关系状态决定，"
            "不会因几句话改变核心人格；只有关系真实推进后才改变语气，不刻意制造温情或暧昧。"
        )
        academic = (
            f"大二时获得国家奖学金，正在准备保研；{resolution.education_goal}。"
            f"披露边界：{resolution.education_disclosure}。"
        )
        derived = [
            self._derived_claim(
                draft_id,
                key="personality",
                facet="value",
                value=personality_value,
                origin="material_review_correction",
                evidence_source_ids=[str(personality_source["source_material_id"])],
                material_content=str(personality_source["corrected_content"]),
            ),
            self._derived_claim(
                draft_id,
                key="style_profile",
                facet="communication",
                value=_json(chat_style),
                context={"channel": "wechat_chat", "source": "owner_review"},
                origin="owner_confirmation",
                evidence_source_ids=chat_evidence,
                material_content=(
                    f"微信表达确认：{resolution.punctuation_style}；{resolution.addressing_style}"
                ),
            ),
            self._derived_claim(
                draft_id,
                key="style_profile",
                facet="communication",
                value=_json(moments_style),
                context={"channel": "moments", "source": "owner_review"},
                origin="owner_confirmation",
                evidence_source_ids=moments_evidence,
                material_content="朋友圈审核确认：排除的朋友圈内容真实，可参与聚合风格，但不作为独立人设事实。",
            ),
            self._derived_claim(
                draft_id,
                key="academic",
                facet="identity",
                value=academic,
                origin="owner_confirmation",
                material_content=f"academic: {academic}",
            ),
            self._derived_claim(
                draft_id,
                key="pet",
                facet="preference",
                value=resolution.pet_status,
                origin="owner_confirmation",
                material_content=f"pet: {resolution.pet_status}",
            ),
            self._derived_claim(
                draft_id,
                key="behavior_rules",
                facet="boundary",
                value=behavior_rules,
                origin="owner_confirmation",
                material_content=f"behavior_rules: {behavior_rules}",
            ),
        ]
        handled_corrections = {"language_style_chat", "personality"}
        for key, item in corrected.items():
            if key in handled_corrections or key in _REBUILT_KEYS or key in _CONTACT_SCOPED_KEYS:
                continue
            _, corrected_value = _field_content(item["corrected_content"])
            template = self._claim_template_for_source(connection, str(item["source_material_id"]), key)
            if not corrected_value or template is None:
                raise BaselineCompileError(f"修正素材 {key} 无法映射到原人格结论")
            derived.append(
                self._derived_claim(
                    draft_id,
                    key=key,
                    facet=str(template["facet"]),
                    value=corrected_value,
                    context=_parse_json(template["context_json"], {}),
                    origin="material_review_correction",
                    evidence_source_ids=[str(item["source_material_id"])],
                    material_content=str(item["corrected_content"]),
                    discriminator=str(item["id"]),
                )
            )

        split_rows = connection.execute(
            """
            SELECT review.id, review.source_material_id, review.split_contents_json
            FROM material_reviews AS review
            WHERE review.decision='split'
            ORDER BY review.id
            """
        ).fetchall()
        for split_review in split_rows:
            parts = _parse_json(split_review["split_contents_json"], [])
            if not isinstance(parts, list) or len(parts) < 2:
                raise BaselineCompileError("已拆分素材缺少至少两个有效片段")
            for index, part in enumerate(parts):
                key, value = _field_content(part)
                if not key or not value:
                    raise BaselineCompileError("拆分后的人设片段必须使用 field: value 格式")
                if key in _CONTACT_SCOPED_KEYS or key.startswith("recent_chat_"):
                    continue
                template = self._claim_template_for_source(
                    connection,
                    str(split_review["source_material_id"]),
                    key,
                )
                if template is None:
                    raise BaselineCompileError(f"拆分素材 {key} 无法映射到原人格结论")
                derived.append(
                    self._derived_claim(
                        draft_id,
                        key=key,
                        facet=str(template["facet"]),
                        value=value,
                        context=_parse_json(template["context_json"], {}),
                        origin="material_review_split",
                        evidence_source_ids=[str(split_review["source_material_id"])],
                        material_content=str(part),
                        discriminator=f"{split_review['id']}:{index}",
                    )
                )
        return selected, derived, supersede

    @staticmethod
    def _claim_template_for_source(
        connection: sqlite3.Connection,
        source_material_id: str,
        claim_key: str,
    ) -> sqlite3.Row | None:
        exact = connection.execute(
            """
            SELECT claim.facet, claim.context_json
            FROM claim_evidence AS evidence
            JOIN persona_claim AS claim ON claim.id=evidence.claim_id
            WHERE evidence.source_material_id=? AND claim.claim_key=?
            ORDER BY CASE claim.status WHEN 'approved' THEN 0 ELSE 1 END, claim.created_at DESC
            LIMIT 1
            """,
            (source_material_id, claim_key),
        ).fetchone()
        if exact is not None:
            return exact
        return connection.execute(
            """
            SELECT claim.facet, claim.context_json
            FROM claim_evidence AS evidence
            JOIN persona_claim AS claim ON claim.id=evidence.claim_id
            WHERE evidence.source_material_id=?
            ORDER BY CASE claim.status WHEN 'approved' THEN 0 ELSE 1 END, claim.created_at DESC
            LIMIT 1
            """,
            (source_material_id,),
        ).fetchone()

    def _style_resolution(
        self,
        rows: list[sqlite3.Row],
        *,
        channel: str,
    ) -> dict[str, Any]:
        for row in rows:
            context = _parse_json(row["context_json"], {})
            if context.get("channel") != channel:
                continue
            parsed = _parse_json(row["claim_value"], None)
            if isinstance(parsed, dict):
                return dict(parsed)
        raise BaselineCompileError(f"缺少 {channel} 的已审核聚合风格")

    def _derived_claim(
        self,
        draft_id: str,
        *,
        key: str,
        facet: str,
        value: str,
        context: Mapping[str, Any] | None = None,
        origin: str,
        evidence_source_ids: list[str] | None = None,
        material_content: str = "",
        discriminator: str = "",
    ) -> dict[str, Any]:
        channel = str((context or {}).get("channel") or "stable")
        claim_id = _stable_id(draft_id, "derived-claim", channel, key, discriminator)
        return {
            "id": claim_id,
            "facet": facet,
            "claim_key": key,
            "claim_value": value,
            "context": dict(context or {}),
            "polarity": "supports",
            "confidence": 1.0,
            "status": "approved",
            "origin": origin,
            "evidence_source_ids": list(dict.fromkeys(evidence_source_ids or [])),
            "material_content": str(material_content or "").strip(),
        }

    @staticmethod
    def _claim_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "facet": str(row["facet"]),
            "claim_key": str(row["claim_key"]),
            "claim_value": str(row["claim_value"]),
            "context": _parse_json(row["context_json"], {}),
            "polarity": str(row["polarity"]),
            "confidence": float(row["confidence"]),
            "status": str(row["status"]),
            "origin": str(row["origin"]),
        }

    @staticmethod
    def _contact_profile(resolution: BaselineResolution) -> dict[str, str]:
        return {
            "称呼习惯": resolution.addressing_style,
            "当前关系基线": resolution.relationship_baseline,
            "共同经历": resolution.last_shared_context,
            "已知感情状态": resolution.known_partner_status,
        }

    def _material_ids(
        self,
        connection: sqlite3.Connection,
        *,
        channel: str,
        include_excluded: bool,
    ) -> list[str]:
        decisions = ("keep", "exclude") if include_excluded else ("keep",)
        placeholders = ",".join("?" for _ in decisions)
        rows = connection.execute(
            f"""
            SELECT source_material_id FROM material_reviews
            WHERE source_channel=? AND decision IN ({placeholders})
            ORDER BY source_material_id
            """,
            (channel, *decisions),
        ).fetchall()
        return [str(row["source_material_id"]) for row in rows]

    def _materialize_derived_claim(
        self,
        connection: sqlite3.Connection,
        claim: object,
        *,
        draft_id: str,
        actor: str,
        now_text: str,
    ) -> None:
        if not isinstance(claim, dict):
            raise BaselineCompileError("人格基线草稿包含无效结论")
        claim_id = str(claim.get("id") or "")
        if not claim_id:
            raise BaselineCompileError("人格基线草稿结论缺少 ID")
        material_ids = [
            str(item) for item in claim.get("evidence_source_ids", []) if str(item)
        ]
        material_content = str(claim.get("material_content") or "").strip()
        if material_content:
            material_id = _stable_id(draft_id, "owner-material", claim_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO source_material(
                    id, character_id, source_type, content, source_time,
                    import_meta_json, reliability, scope, content_hash,
                    is_deleted, created_at
                ) VALUES (?, ?, 'manual', ?, ?, ?, 1.0, 'persona', ?, 0, ?)
                """,
                (
                    material_id,
                    self.character_id,
                    material_content,
                    now_text,
                    _json({"origin": "owner_confirmation", "baseline_draft_id": draft_id}),
                    hashlib.sha256(material_content.encode("utf-8")).hexdigest(),
                    now_text,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO material_reviews(
                    id, source_material_id, source_channel, decision,
                    corrected_content, evidence_json, reviewed_by, reviewed_at,
                    created_at, updated_at, review_note, split_contents_json
                ) VALUES (?, ?, 'identity', 'keep', NULL, '[]', ?, ?, ?, ?, ?, '[]')
                """,
                (
                    f"review:{material_id}",
                    material_id,
                    actor,
                    now_text,
                    now_text,
                    now_text,
                    _OWNER_CONFIRMATION_REVIEW_NOTE,
                ),
            )
            material_ids.append(material_id)

        connection.execute(
            """
            INSERT INTO persona_claim(
                id, character_id, facet, claim_key, claim_value, context_json,
                polarity, confidence, status, origin, reviewed_at,
                reviewed_by, review_reason
            ) VALUES (?, ?, ?, ?, ?, ?, 'supports', ?, 'approved', ?, ?, ?, ?)
            """,
            (
                claim_id,
                self.character_id,
                str(claim.get("facet") or "identity"),
                str(claim.get("claim_key") or ""),
                str(claim.get("claim_value") or ""),
                _json(claim.get("context") if isinstance(claim.get("context"), dict) else {}),
                float(claim.get("confidence") or 1.0),
                str(claim.get("origin") or "owner_confirmation"),
                now_text,
                actor,
                "approved_in_owner_review_baseline",
            ),
        )
        for source_id in dict.fromkeys(material_ids):
            exists = connection.execute(
                "SELECT 1 FROM source_material WHERE id=? AND character_id=? AND is_deleted=0",
                (source_id, self.character_id),
            ).fetchone()
            if exists is None:
                raise BaselineCompileError("人格基线结论引用了不存在的素材")
            connection.execute(
                """
                INSERT OR IGNORE INTO claim_evidence(
                    id, claim_id, source_material_id, quote_span_json,
                    support_kind, evidence_weight, created_at
                ) VALUES (?, ?, ?, ?, 'supports', 1.0, ?)
                """,
                (
                    _stable_id(claim_id, source_id, "owner-review-evidence"),
                    claim_id,
                    source_id,
                    _json({"kind": "owner_review_baseline"}),
                    now_text,
                ),
            )
        self._audit(
            connection,
            action="persona_claim.approve_from_material_review",
            entity_id=claim_id,
            actor=actor,
            payload={"baseline_draft_id": draft_id, "source_ids": material_ids},
        )

    def _apply_contact_profile(
        self,
        connection: sqlite3.Connection,
        draft: sqlite3.Row,
        *,
        now_text: str,
    ) -> None:
        contact_id = str(draft["contact_id"] or "")
        if not contact_id:
            return
        delete_keys = [str(item) for item in _parse_json(draft["contact_delete_keys_json"], [])]
        if delete_keys:
            placeholders = ",".join("?" for _ in delete_keys)
            connection.execute(
                f"DELETE FROM contact_profiles WHERE contact_id=? AND key IN ({placeholders})",
                (contact_id, *delete_keys),
            )
        profile = _parse_json(draft["contact_profile_json"], {})
        if not isinstance(profile, dict):
            raise BaselineCompileError("联系人画像预览无效")
        for key, value in profile.items():
            connection.execute(
                """
                INSERT INTO contact_profiles(contact_id, key, value, confirmed, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(contact_id, key) DO UPDATE SET
                    value=excluded.value, confirmed=1, updated_at=excluded.updated_at
                """,
                (contact_id, str(key), str(value), now_text),
            )

    def _draft_view(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        active: sqlite3.Row,
    ) -> dict[str, Any]:
        baseline = connection.execute(
            "SELECT * FROM persona_snapshot WHERE id=? AND character_id=?",
            (str(row["baseline_snapshot_id"]), self.character_id),
        ).fetchone()
        comparison_snapshot = baseline or active
        before_profile = _parse_json(comparison_snapshot["stable_profile_json"], {})
        before_styles = _parse_json(comparison_snapshot["channel_styles_json"], {})
        after_profile = _parse_json(row["stable_profile_json"], {})
        after_styles = _parse_json(row["channel_styles_json"], {})
        contact_id = str(row["contact_id"] or "")
        after_contact_profile = _parse_json(row["contact_profile_json"], {})
        deleted_contact_keys = {
            str(item) for item in _parse_json(row["contact_delete_keys_json"], [])
        }
        if str(row["status"]) == "activated":
            current_contact_profile = {
                key: "（旧版混合画像字段，将移除；内容不在差异页展开）"
                for key in deleted_contact_keys
            }
        else:
            current_contact_profile = {
                str(item["key"]): str(item["value"])
                for item in connection.execute(
                    "SELECT key, value FROM contact_profiles WHERE contact_id=? ORDER BY key",
                    (contact_id,),
                ).fetchall()
            } if contact_id else {}
        before_style_content = {
            str(channel): value.get("style", {}) if isinstance(value, dict) else {}
            for channel, value in before_styles.items()
        } if isinstance(before_styles, dict) else {}
        after_style_content = {
            str(channel): value.get("style", {}) if isinstance(value, dict) else {}
            for channel, value in after_styles.items()
        } if isinstance(after_styles, dict) else {}
        contact_diff = _mapping_diff(current_contact_profile, after_contact_profile)
        for item in contact_diff:
            if item["key"] in deleted_contact_keys and item["key"] not in after_contact_profile:
                item["before"] = "（旧版混合画像字段，将移除；内容不在差异页展开）"
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "baseline_snapshot_id": str(row["baseline_snapshot_id"]),
            "result_snapshot_id": str(row["result_snapshot_id"] or ""),
            "note": str(row["note"] or ""),
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "activated_at": str(row["activated_at"] or ""),
            "profile_diff": _mapping_diff(before_profile, after_profile),
            "style_diff": _mapping_diff(before_style_content, after_style_content),
            "contact_profile_diff": contact_diff,
            "selected_claim_count": len(_parse_json(row["selected_claim_ids_json"], [])),
            "derived_claim_count": len(_parse_json(row["derived_claims_json"], [])),
            "superseded_claim_count": len(_parse_json(row["supersede_claim_ids_json"], [])),
        }

    @staticmethod
    def _snapshot_reference(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "version": int(row["version"]),
            "kind": str(row["snapshot_kind"]),
            "note": str(row["note"] or ""),
            "is_active": bool(row["is_active"]),
        }

    def _active_snapshot(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM persona_snapshot
            WHERE character_id=? AND is_active=1 AND is_published=1 AND is_read_only=1
            ORDER BY version DESC LIMIT 1
            """,
            (self.character_id,),
        ).fetchone()
        if row is None:
            raise BaselineCompileError("没有可作为差异基线的活动人格快照")
        return row

    @staticmethod
    def _assert_contact(connection: sqlite3.Connection, contact_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM contacts WHERE id=? AND enabled=1",
            (contact_id,),
        ).fetchone()
        if row is None:
            raise BaselineCompileError("澄清内容必须绑定一个启用的白名单联系人")

    @staticmethod
    def _review_fingerprint(connection: sqlite3.Connection) -> str:
        rows = [
            {
                "id": str(row["id"]),
                "decision": str(row["decision"]),
                "corrected_content": str(row["corrected_content"] or ""),
                "split_contents_json": str(row["split_contents_json"] or "[]"),
                "updated_at": str(row["updated_at"]),
            }
            for row in connection.execute(
                """
                SELECT id, decision, corrected_content, split_contents_json, updated_at
                FROM material_reviews ORDER BY id
                """
            ).fetchall()
        ]
        return _hash(rows)

    @staticmethod
    def _mark_stale(connection: sqlite3.Connection, draft_id: str, now_text: str) -> None:
        connection.execute(
            "UPDATE persona_baseline_drafts SET status='stale', updated_at=? WHERE id=?",
            (now_text, draft_id),
        )

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        entity_id: str,
        actor: str,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO persona_audit_log(
                id, character_id, action, entity_type, entity_id, actor,
                payload_json, created_at
            ) VALUES (?, ?, ?, 'persona_baseline', ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                self.character_id,
                action,
                entity_id,
                actor,
                _json(dict(payload)),
                _iso(_now()),
            ),
        )
