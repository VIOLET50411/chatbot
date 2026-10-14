"""角色级互动状态仓储。

所有读写都显式接收 CharacterContext，并以版本比较更新阻止后台任务互相覆盖。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from core.character_context import CharacterContext
from core.interaction_state import (
    InteractionState,
    StateTransition,
    from_legacy_inner_state,
    state_from_payload,
    state_to_payload,
)
from memory.models import InnerState, RelationshipStage


_SCHEMA = """
CREATE TABLE IF NOT EXISTS interaction_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interaction_state_transition (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    result_version INTEGER NOT NULL,
    input_json TEXT NOT NULL,
    before_state_json TEXT NOT NULL,
    after_state_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interaction_state_transition_version
ON interaction_state_transition(result_version DESC);
"""


class StateVersionConflict(RuntimeError):
    """状态在读取后已被另一个任务提交，调用方必须重新计算。"""


def _database_path(context: CharacterContext) -> Path:
    return context.workspace_dir / "soul_mate.db"


def _connect(context: CharacterContext) -> sqlite3.Connection:
    context.workspace_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(_database_path(context), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _encode(value: Mapping[str, Any] | dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _decode_state(raw: str, version: int, updated_at: str | None) -> InteractionState:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    parsed_updated_at = None
    if updated_at:
        try:
            parsed_updated_at = datetime.fromisoformat(updated_at)
        except ValueError:
            pass
    return state_from_payload(payload, version=version, updated_at=parsed_updated_at)


def _legacy_state(connection: sqlite3.Connection) -> InteractionState:
    try:
        row = connection.execute("SELECT * FROM inner_state WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row:
        return InteractionState()

    stage_value = row["stage"] or RelationshipStage.STRANGER.value
    try:
        stage = RelationshipStage(stage_value)
    except ValueError:
        stage = RelationshipStage.STRANGER
    legacy = InnerState(
        happiness=row["happiness"],
        energy=row["energy"],
        irritability=row["irritability"],
        affection=row["affection"],
        loneliness=row["loneliness"],
        trust=row["trust"],
        intimacy=row["intimacy"],
        resentment=row["resentment"],
        stage=stage,
        current_activity=row["current_activity"] or "没什么特别的",
        last_chat_time=datetime.fromisoformat(row["last_chat_time"]) if row["last_chat_time"] else None,
        last_tick_time=datetime.fromisoformat(row["last_tick_time"]) if row["last_tick_time"] else None,
    )
    return from_legacy_inner_state(legacy)


def _load_or_migrate(connection: sqlite3.Connection) -> InteractionState:
    row = connection.execute("SELECT * FROM interaction_state WHERE id=1").fetchone()
    if row:
        return _decode_state(row["state_json"], row["version"], row["updated_at"])

    state = _legacy_state(connection)
    created_at = datetime.now()
    payload = state_to_payload(state)
    cursor = connection.execute(
        "INSERT OR IGNORE INTO interaction_state (id, version, state_json, updated_at) VALUES (1, 0, ?, ?)",
        (_encode(payload), created_at.isoformat()),
    )
    if cursor.rowcount != 1:
        # 首次初始化被并发任务抢先完成：读取赢家提交的状态即可。
        # 审计仅由实际插入状态的任务写入，避免产生重复迁移记录。
        current = connection.execute("SELECT * FROM interaction_state WHERE id=1").fetchone()
        if current:
            return _decode_state(current["state_json"], current["version"], current["updated_at"])
        raise RuntimeError("互动状态初始化冲突后未找到已提交状态")

    connection.execute(
        """
        INSERT INTO interaction_state_transition
        (reason, base_version, result_version, input_json, before_state_json, after_state_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy_state_migration",
            -1,
            0,
            _encode({"source": "inner_state"}),
            _encode({}),
            _encode(payload),
            created_at.isoformat(),
        ),
    )
    return replace(state, updated_at=created_at)


def load_interaction_state(context: CharacterContext) -> InteractionState:
    connection = _connect(context)
    try:
        with connection:
            connection.executescript(_SCHEMA)
            return _load_or_migrate(connection)
    finally:
        connection.close()


def commit_interaction_transition(
    context: CharacterContext,
    *,
    expected: InteractionState,
    next_state: InteractionState,
    reason: str,
    inputs: Mapping[str, Any],
) -> InteractionState:
    if next_state.version != expected.version:
        raise ValueError("状态演化函数不得预先修改版本号")

    connection = _connect(context)
    try:
        with connection:
            connection.executescript(_SCHEMA)
            current = _load_or_migrate(connection)
            if current.version != expected.version:
                raise StateVersionConflict(
                    f"角色 {context.character_id} 的互动状态已推进，不能覆盖版本 {expected.version}"
                )
            committed_at = datetime.now()
            result = replace(next_state, version=expected.version + 1, updated_at=committed_at)
            cursor = connection.execute(
                """
                UPDATE interaction_state
                SET version=?, state_json=?, updated_at=?
                WHERE id=1 AND version=?
                """,
                (
                    result.version,
                    _encode(state_to_payload(result)),
                    committed_at.isoformat(),
                    expected.version,
                ),
            )
            if cursor.rowcount != 1:
                raise StateVersionConflict(
                    f"角色 {context.character_id} 的互动状态提交失败，不能覆盖版本 {expected.version}"
                )
            connection.execute(
                """
                INSERT INTO interaction_state_transition
                (reason, base_version, result_version, input_json, before_state_json, after_state_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reason,
                    expected.version,
                    result.version,
                    _encode(dict(inputs)),
                    _encode(state_to_payload(expected)),
                    _encode(state_to_payload(result)),
                    committed_at.isoformat(),
                ),
            )
            return result
    finally:
        connection.close()


def _to_transition(row: sqlite3.Row) -> StateTransition:
    try:
        inputs = json.loads(row["input_json"])
    except json.JSONDecodeError:
        inputs = {}
    try:
        before_payload = json.loads(row["before_state_json"])
    except json.JSONDecodeError:
        before_payload = {}
    try:
        after_payload = json.loads(row["after_state_json"])
    except json.JSONDecodeError:
        after_payload = {}
    return StateTransition(
        transition_id=row["id"],
        reason=row["reason"],
        base_version=row["base_version"],
        result_version=row["result_version"],
        inputs=inputs if isinstance(inputs, dict) else {},
        before=state_from_payload(before_payload, version=max(0, row["base_version"])),
        after=state_from_payload(after_payload, version=row["result_version"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def list_interaction_transitions(
    context: CharacterContext,
    *,
    limit: int = 100,
) -> list[StateTransition]:
    connection = _connect(context)
    try:
        with connection:
            connection.executescript(_SCHEMA)
            _load_or_migrate(connection)
            rows = connection.execute(
                """
                SELECT * FROM interaction_state_transition
                ORDER BY result_version DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    finally:
        connection.close()

    return [_to_transition(row) for row in rows]


def find_interaction_transition_by_analysis_reason(
    context: CharacterContext,
    analysis_reason: str,
) -> StateTransition | None:
    """按稳定输入标识精确查找互动迁移，不受审计分页上限影响。"""
    normalized_reason = str(analysis_reason or "").strip()
    if not normalized_reason:
        raise ValueError("互动迁移查询标识不能为空")

    connection = _connect(context)
    try:
        with connection:
            connection.executescript(_SCHEMA)
            _load_or_migrate(connection)
            rows = connection.execute(
                """
                SELECT * FROM interaction_state_transition
                WHERE instr(input_json, '"analysis_reason"') > 0
                ORDER BY result_version DESC, id DESC
                """
            ).fetchall()
    finally:
        connection.close()

    for row in rows:
        transition = _to_transition(row)
        if transition.inputs.get("analysis_reason") == normalized_reason:
            return transition
    return None
