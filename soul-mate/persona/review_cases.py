"""人格候选场景盲评、证据与统计。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


RATINGS = frozenset({"realistic", "acceptable", "artificial"})
EVIDENCE_STATES = frozenset({"accepted", "inference", "rejected"})


class PersonaReviewStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS persona_review_cases (
                    id TEXT PRIMARY KEY,
                    candidate_snapshot_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    context_note TEXT NOT NULL,
                    candidate_reply TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(candidate_snapshot_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_persona_review_cases_candidate
                ON persona_review_cases(candidate_snapshot_id, sequence);

                CREATE TABLE IF NOT EXISTS persona_review_ratings (
                    case_id TEXT PRIMARY KEY REFERENCES persona_review_cases(id) ON DELETE CASCADE,
                    rating TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    note TEXT NOT NULL,
                    rated_by TEXT NOT NULL,
                    rated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS persona_candidate_evidence (
                    candidate_snapshot_id TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    conclusion TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    state TEXT NOT NULL,
                    PRIMARY KEY(candidate_snapshot_id, item_key)
                );
                """
            )

    def seed_cases(
        self,
        *,
        candidate_snapshot_id: str,
        character_id: str,
        cases: list[dict[str, str]],
    ) -> int:
        if len(cases) < 40:
            raise ValueError("人格候选至少需要 40 个审核场景")
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            for sequence, case in enumerate(cases, start=1):
                connection.execute(
                    """
                    INSERT INTO persona_review_cases(
                        id, candidate_snapshot_id, character_id, sequence, category,
                        user_message, context_note, candidate_reply, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(candidate_snapshot_id, sequence) DO UPDATE SET
                        category=excluded.category,
                        user_message=excluded.user_message,
                        context_note=excluded.context_note,
                        candidate_reply=excluded.candidate_reply
                    """,
                    (
                        str(uuid.uuid4()),
                        candidate_snapshot_id,
                        character_id,
                        sequence,
                        str(case.get("category") or "日常")[:80],
                        str(case.get("user_message") or "")[:20_000],
                        str(case.get("context_note") or "")[:2_000],
                        str(case.get("candidate_reply") or "")[:20_000],
                        now,
                    ),
                )
        return len(cases)

    def seed_evidence(self, candidate_snapshot_id: str, items: list[dict[str, str]]) -> int:
        with self._connection() as connection:
            for item in items:
                state = str(item.get("state") or "inference").strip().lower()
                if state not in EVIDENCE_STATES:
                    raise ValueError("候选证据状态无效")
                connection.execute(
                    """
                    INSERT INTO persona_candidate_evidence(
                        candidate_snapshot_id, item_key, conclusion, evidence, state
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(candidate_snapshot_id, item_key) DO UPDATE SET
                        conclusion=excluded.conclusion,
                        evidence=excluded.evidence,
                        state=excluded.state
                    """,
                    (
                        candidate_snapshot_id,
                        str(item.get("item_key") or "")[:128],
                        str(item.get("conclusion") or "")[:10_000],
                        str(item.get("evidence") or "")[:10_000],
                        state,
                    ),
                )
        return len(items)

    def list_cases(self, candidate_snapshot_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT c.*, r.rating, r.reasons_json, r.note, r.rated_at
                FROM persona_review_cases c
                LEFT JOIN persona_review_ratings r ON r.case_id=c.id
                WHERE c.candidate_snapshot_id=? ORDER BY c.sequence
                """,
                (candidate_snapshot_id,),
            ).fetchall()
            evidence = connection.execute(
                """
                SELECT item_key, conclusion, evidence, state
                FROM persona_candidate_evidence
                WHERE candidate_snapshot_id=? ORDER BY item_key
                """,
                (candidate_snapshot_id,),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["reasons"] = json.loads(str(item.pop("reasons_json") or "[]"))
            items.append(item)
        counts = {rating: sum(1 for item in items if item.get("rating") == rating) for rating in RATINGS}
        rated = sum(counts.values())
        acceptable = counts["realistic"] + counts["acceptable"]
        return {
            "candidate_snapshot_id": candidate_snapshot_id,
            "items": items,
            "evidence": [dict(row) for row in evidence],
            "summary": {
                "total": len(items),
                "rated": rated,
                "realistic": counts["realistic"],
                "acceptable": counts["acceptable"],
                "artificial": counts["artificial"],
                "acceptable_percent": round(acceptable * 100 / rated, 1) if rated else 0.0,
                "passes_85_percent": rated == len(items) and rated > 0 and acceptable / rated >= 0.85,
                "passes_strict_review": rated == len(items) and rated > 0 and counts["artificial"] == 0,
            },
        }

    def rate_case(
        self,
        case_id: str,
        *,
        rating: str,
        reasons: list[str],
        note: str,
        actor: str,
    ) -> dict[str, Any]:
        normalized_rating = str(rating or "").strip().lower()
        if normalized_rating not in RATINGS:
            raise ValueError("盲评结果无效")
        normalized_reasons = list(
            dict.fromkeys(str(item or "").strip() for item in reasons if str(item or "").strip())
        )
        if len(normalized_reasons) > 12 or any(len(item) > 100 for item in normalized_reasons):
            raise ValueError("人机感原因过多或过长")
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM persona_review_cases WHERE id=?", (case_id,)).fetchone() is None:
                raise KeyError("审核场景不存在")
            connection.execute(
                """
                INSERT INTO persona_review_ratings(
                    case_id, rating, reasons_json, note, rated_by, rated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    rating=excluded.rating,
                    reasons_json=excluded.reasons_json,
                    note=excluded.note,
                    rated_by=excluded.rated_by,
                    rated_at=excluded.rated_at
                """,
                (
                    case_id,
                    normalized_rating,
                    json.dumps(normalized_reasons, ensure_ascii=False),
                    str(note or "")[:2_000],
                    str(actor or "owner")[:128],
                    now,
                ),
            )
        return {
            "case_id": case_id,
            "rating": normalized_rating,
            "reasons": normalized_reasons,
            "note": str(note or "")[:2_000],
            "rated_at": now,
        }

    def carry_forward_exact_ratings(
        self,
        *,
        source_snapshot_id: str,
        target_snapshot_id: str,
    ) -> int:
        """只继承场景、上下文和候选回复完全一致的人工评分。"""
        with self._connection() as connection:
            before = connection.total_changes
            connection.execute(
                """
                INSERT INTO persona_review_ratings(
                    case_id, rating, reasons_json, note, rated_by, rated_at
                )
                SELECT target.id, rating.rating, rating.reasons_json, rating.note,
                       rating.rated_by, rating.rated_at
                FROM persona_review_cases source
                JOIN persona_review_ratings rating ON rating.case_id=source.id
                JOIN persona_review_cases target
                  ON target.candidate_snapshot_id=?
                 AND target.category=source.category
                 AND target.user_message=source.user_message
                 AND target.context_note=source.context_note
                 AND target.candidate_reply=source.candidate_reply
                LEFT JOIN persona_review_ratings existing ON existing.case_id=target.id
                WHERE source.candidate_snapshot_id=? AND existing.case_id IS NULL
                """,
                (target_snapshot_id, source_snapshot_id),
            )
            return connection.total_changes - before

    def approved_style_examples(
        self,
        *,
        character_id: str,
        message: str,
        limit: int = 3,
    ) -> list[str]:
        """只返回 Owner 明确评为“像真人”的候选回复，未审核产物绝不进入示例库。"""
        normalized_character = str(character_id or "").strip()
        if not normalized_character:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT c.user_message, c.candidate_reply, r.rated_at
                FROM persona_review_cases c
                JOIN persona_review_ratings r ON r.case_id=c.id
                WHERE c.character_id=? AND r.rating='realistic'
                ORDER BY r.rated_at DESC, c.sequence
                LIMIT 200
                """,
                (normalized_character,),
            ).fetchall()
        query_chars = {char for char in str(message or "") if not char.isspace()}
        scored: list[tuple[float, str]] = []
        for row in rows:
            source = str(row["user_message"] or "")
            reply = str(row["candidate_reply"] or "").strip()
            if not reply:
                continue
            source_chars = {char for char in source if not char.isspace()}
            overlap = len(query_chars & source_chars) / max(1, len(query_chars | source_chars))
            scored.append((overlap, reply))
        scored.sort(key=lambda item: item[0], reverse=True)
        return list(dict.fromkeys(reply for _, reply in scored))[: max(1, min(int(limit), 5))]
