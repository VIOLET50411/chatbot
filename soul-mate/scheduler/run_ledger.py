"""全局运行台账：跨角色的调度任务执行审计。

联系人主动意图记录位于核心数据库；本模块只记录调度/后台任务本身的执行历史
（哪个任务、哪个角色、何时开始/结束、成功还是失败），
供后续统一接入调度面板与管理台的可观测性视图使用。

设计约束：
- 数据库固定位于 `SOUL_MATE_ROOT/data/platform/runtime_ledger.db`（借助
  `core.character_context.resolve_workspace_path` 强制路径必须落在项目目录内，
  不接受调用方传入任意路径）。
- 所有查询只使用参数化 SQL，不拼接调用方提供的列名、表名或原始 SQL 片段。
- 状态迁移在单个事务内完成读取校验与写入，避免并发下的状态错乱。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Literal, Mapping

from core.character_context import resolve_workspace_path


RuntimeJobStatus = Literal["running", "succeeded", "failed", "skipped"]

_STATUSES: frozenset[str] = frozenset({"running", "succeeded", "failed", "skipped"})
_TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "skipped"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_job_run (
    id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    character_id TEXT,
    scheduled_at TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error_summary TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    details_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_job_run_job
ON runtime_job_run(job_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_job_run_character
ON runtime_job_run(character_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_job_run_status
ON runtime_job_run(status, started_at DESC);
"""

_SCHEMA_INITIALIZATION_LOCK = Lock()


class RuntimeJobRunStateConflict(RuntimeError):
    """任务台账记录已不处于预期状态，调用方必须重新读取。"""


@dataclass(frozen=True, slots=True)
class RuntimeJobRun:
    id: str
    job_name: str
    character_id: str | None
    scheduled_at: datetime | None
    started_at: datetime
    finished_at: datetime | None
    status: RuntimeJobStatus
    error_summary: str
    duration_ms: int | None
    details: Mapping[str, object]


def _database_path() -> Path:
    """全局平台账本固定路径，禁止调用方指定任意位置。"""
    platform_dir = resolve_workspace_path("data/platform")
    return platform_dir / "runtime_ledger.db"


def _connect() -> sqlite3.Connection:
    db_path = _database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        with _SCHEMA_INITIALIZATION_LOCK:
            connection.executescript(_SCHEMA)
            connection.commit()
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        raise


def _now() -> datetime:
    return datetime.now()


def _encode_details(details: Mapping[str, object] | None) -> str:
    payload = dict(details) if details else {}
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError as exc:
        raise ValueError("details 必须是可 JSON 序列化的映射") from exc


def _decode_details(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalized_job_name(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("job_name 不能为空")
    if len(normalized) > 128:
        raise ValueError("job_name 不能超过 128 个字符")
    return normalized


def _normalized_character_id(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > 128:
        raise ValueError("character_id 不能超过 128 个字符")
    return normalized


def _normalized_error_summary(value: object | None) -> str:
    if value is None:
        return ""
    normalized = str(value).strip()
    return normalized[:500]


def _normalized_status_filter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if normalized not in _STATUSES:
        raise ValueError("status 无效")
    return normalized


def _bounded_page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(200, int(limit))), max(0, int(offset))


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _to_run(row: sqlite3.Row) -> RuntimeJobRun:
    status = str(row["status"])
    if status not in _STATUSES:
        status = "failed"
    return RuntimeJobRun(
        id=str(row["id"]),
        job_name=str(row["job_name"]),
        character_id=str(row["character_id"]) if row["character_id"] else None,
        scheduled_at=_parse_datetime(row["scheduled_at"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=_parse_datetime(row["finished_at"]),
        status=status,  # type: ignore[assignment]
        error_summary=str(row["error_summary"] or ""),
        duration_ms=int(row["duration_ms"]) if row["duration_ms"] is not None else None,
        details=_decode_details(row["details_json"]),
    )


def _load(connection: sqlite3.Connection, run_id: str) -> RuntimeJobRun | None:
    row = connection.execute(
        "SELECT * FROM runtime_job_run WHERE id=?", (run_id,)
    ).fetchone()
    return _to_run(row) if row else None


def start_job_run(
    job_name: str,
    *,
    character_id: str | None = None,
    scheduled_at: datetime | None = None,
    now: datetime | None = None,
    details: Mapping[str, object] | None = None,
) -> RuntimeJobRun:
    """记录一次任务开始执行，状态为 running。"""
    normalized_job_name = _normalized_job_name(job_name)
    normalized_character_id = _normalized_character_id(character_id)
    started_at = now or _now()
    encoded_details = _encode_details(details)
    run_id = str(uuid.uuid4())

    connection = _connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runtime_job_run
                (id, job_name, character_id, scheduled_at, started_at, finished_at,
                 status, error_summary, duration_ms, details_json, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, 'running', '', NULL, ?, ?)
                """,
                (
                    run_id,
                    normalized_job_name,
                    normalized_character_id,
                    (scheduled_at or started_at).isoformat(),
                    started_at.isoformat(),
                    encoded_details,
                    started_at.isoformat(),
                ),
            )
            run = _load(connection, run_id)
            assert run is not None
            return run
    finally:
        connection.close()


def complete_job_run(
    run_id: str,
    *,
    status: Literal["succeeded", "failed"],
    now: datetime | None = None,
    error: object = None,
    details: Mapping[str, object] | None = None,
) -> RuntimeJobRun:
    """将一次运行中的任务标记为成功或失败，并计算耗时。"""
    if status not in ("succeeded", "failed"):
        raise ValueError("complete_job_run 只能标记为 succeeded 或 failed")
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        raise ValueError("run_id 不能为空")
    finished_at = now or _now()
    error_summary = _normalized_error_summary(error)

    connection = _connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _load(connection, normalized_run_id)
            if current is None:
                raise KeyError("运行记录不存在")
            if current.status != "running":
                raise RuntimeJobRunStateConflict("运行记录当前状态不允许此操作")

            duration_ms = max(
                0,
                int((finished_at - current.started_at).total_seconds() * 1000),
            )
            merged_details = dict(current.details)
            if details:
                merged_details.update(dict(details))
            encoded_details = _encode_details(merged_details)

            cursor = connection.execute(
                """
                UPDATE runtime_job_run
                SET status=?, finished_at=?, error_summary=?, duration_ms=?,
                    details_json=?, updated_at=?
                WHERE id=? AND status='running'
                """,
                (
                    status,
                    finished_at.isoformat(),
                    error_summary,
                    duration_ms,
                    encoded_details,
                    finished_at.isoformat(),
                    normalized_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeJobRunStateConflict("运行记录状态提交冲突")
            run = _load(connection, normalized_run_id)
            assert run is not None
            return run
    finally:
        connection.close()


def skip_job_run(
    job_name: str,
    *,
    character_id: str | None = None,
    reason: str,
    now: datetime | None = None,
    scheduled_at: datetime | None = None,
    details: Mapping[str, object] | None = None,
) -> RuntimeJobRun:
    """记录一次未实际执行、直接跳过的任务（例如资格判断未通过）。"""
    normalized_job_name = _normalized_job_name(job_name)
    normalized_character_id = _normalized_character_id(character_id)
    normalized_reason = _normalized_error_summary(reason) or "skipped"
    occurred_at = now or _now()
    encoded_details = _encode_details(details)
    run_id = str(uuid.uuid4())

    connection = _connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runtime_job_run
                (id, job_name, character_id, scheduled_at, started_at, finished_at,
                 status, error_summary, duration_ms, details_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'skipped', ?, 0, ?, ?)
                """,
                (
                    run_id,
                    normalized_job_name,
                    normalized_character_id,
                    (scheduled_at or occurred_at).isoformat(),
                    occurred_at.isoformat(),
                    occurred_at.isoformat(),
                    normalized_reason,
                    encoded_details,
                    occurred_at.isoformat(),
                ),
            )
            run = _load(connection, run_id)
            assert run is not None
            return run
    finally:
        connection.close()


def get_job_run(run_id: str) -> RuntimeJobRun | None:
    """读取单条运行记录。"""
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return None
    connection = _connect()
    try:
        with connection:
            return _load(connection, normalized_run_id)
    finally:
        connection.close()


def list_job_runs(
    *,
    limit: int = 100,
    offset: int = 0,
    job_name: str | None = None,
    character_id: str | None = None,
    status: str | None = None,
) -> list[RuntimeJobRun]:
    """按 job/character/status 过滤，分页读取运行记录（按开始时间倒序）。"""
    bounded_limit, bounded_offset = _bounded_page(limit, offset)
    normalized_status = _normalized_status_filter(status)
    normalized_job_name = str(job_name).strip() if job_name else None
    normalized_character_id = _normalized_character_id(character_id)

    filters: list[str] = []
    values: list[object] = []
    if normalized_job_name:
        filters.append("job_name=?")
        values.append(normalized_job_name)
    if normalized_character_id:
        filters.append("character_id=?")
        values.append(normalized_character_id)
    if normalized_status:
        filters.append("status=?")
        values.append(normalized_status)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""

    connection = _connect()
    try:
        with connection:
            rows = connection.execute(
                f"""
                SELECT * FROM runtime_job_run{where}
                ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?
                """,
                (*values, bounded_limit, bounded_offset),
            ).fetchall()
            return [_to_run(row) for row in rows]
    finally:
        connection.close()


def count_job_runs(
    *,
    job_name: str | None = None,
    character_id: str | None = None,
    status: str | None = None,
) -> int:
    """按同样的过滤条件统计运行记录数量，供分页总数使用。"""
    normalized_status = _normalized_status_filter(status)
    normalized_job_name = str(job_name).strip() if job_name else None
    normalized_character_id = _normalized_character_id(character_id)

    filters: list[str] = []
    values: list[object] = []
    if normalized_job_name:
        filters.append("job_name=?")
        values.append(normalized_job_name)
    if normalized_character_id:
        filters.append("character_id=?")
        values.append(normalized_character_id)
    if normalized_status:
        filters.append("status=?")
        values.append(normalized_status)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""

    connection = _connect()
    try:
        with connection:
            row = connection.execute(
                f"SELECT COUNT(*) FROM runtime_job_run{where}",
                values,
            ).fetchone()
            return int(row[0])
    finally:
        connection.close()


def summarize_job_runs(
    *,
    job_name: str | None = None,
    character_id: str | None = None,
) -> dict[str, int]:
    """按状态汇总计数，供调度面板展示整体健康度。"""
    normalized_job_name = str(job_name).strip() if job_name else None
    normalized_character_id = _normalized_character_id(character_id)

    filters: list[str] = []
    values: list[object] = []
    if normalized_job_name:
        filters.append("job_name=?")
        values.append(normalized_job_name)
    if normalized_character_id:
        filters.append("character_id=?")
        values.append(normalized_character_id)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""

    connection = _connect()
    try:
        with connection:
            rows = connection.execute(
                f"""
                SELECT status, COUNT(*) AS total FROM runtime_job_run{where}
                GROUP BY status
                """,
                values,
            ).fetchall()
            summary = {status: 0 for status in _STATUSES}
            for row in rows:
                status = str(row["status"])
                if status in _STATUSES:
                    summary[status] = int(row["total"])
            return summary
    finally:
        connection.close()

