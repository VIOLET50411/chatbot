"""记忆读写总管 — SQLite + ChromaDB 统一接口"""
import sqlite3
import json
import uuid
import os
from datetime import datetime
from pathlib import Path
from memory.models import Memory, MemoryType, InnerState, DailyAgenda
from memory import vector_store
from core.character_context import require_character_context


def _workspace_dir() -> Path:
    return require_character_context().workspace_dir


def _conn():
    db_path = _workspace_dir() / "soul_mate.db"
    c = sqlite3.connect(db_path, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# ──────────────────────────────
# 内心状态
# ──────────────────────────────

async def get_inner_state() -> InnerState:
    raise RuntimeError("旧 inner_state 读取已停用，请通过 interaction_service 读取角色互动状态")


async def save_inner_state(state: InnerState) -> None:
    raise RuntimeError("旧 inner_state 写入已停用，互动状态只能通过 interaction_service 提交")


# ──────────────────────────────
# 对话历史
# ──────────────────────────────

async def save_message(role: str, content: str) -> int:
    connection = _conn()
    try:
        with connection:
            cursor = connection.execute(
                "INSERT INTO conversations (role, content) VALUES (?, ?)",
                (role, content),
            )
            return int(cursor.lastrowid)
    finally:
        connection.close()


async def get_recent_messages(limit: int = 20) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def get_today_conversations() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM conversations WHERE date(created_at)=date('now') ORDER BY id"
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


async def archive_old_conversations(days: int = 30):
    with _conn() as c:
        c.execute("DELETE FROM conversations WHERE created_at < datetime('now', ? || ' days')", (f"-{days}",))


# ──────────────────────────────
# 每日摘要
# ──────────────────────────────

async def save_daily_summary(date: str, summary_data: dict):
    raise RuntimeError("旧每日摘要写入已停用，运行时记忆只能通过事实链发布")


async def get_recent_summaries(days: int = 3) -> list[str]:
    raise RuntimeError("旧每日摘要读取已停用，禁止绕过事实链进入运行时")


# ──────────────────────────────
# 每日议程
# ──────────────────────────────

async def save_daily_agenda(agenda: DailyAgenda):
    connection = _conn()
    try:
        with connection:
            connection.execute("""
                INSERT OR REPLACE INTO daily_agenda
                (date, morning, afternoon, evening, mood_note, possible_share)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                agenda.date, agenda.morning, agenda.afternoon, agenda.evening,
                agenda.mood_note,
                json.dumps(agenda.possible_share, ensure_ascii=False),
            ))
    finally:
        connection.close()


async def get_today_agenda() -> DailyAgenda | None:
    today = datetime.now().strftime("%Y-%m-%d")
    connection = _conn()
    try:
        row = connection.execute("""
            SELECT date, morning, afternoon, evening, mood_note, possible_share
            FROM daily_agenda
            WHERE date=?
        """, (today,)).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    return DailyAgenda(
        date=row["date"],
        morning=row["morning"] or "",
        afternoon=row["afternoon"] or "",
        evening=row["evening"] or "",
        mood_note=row["mood_note"] or "",
        possible_share=json.loads(row["possible_share"] or "[]"),
    )


# ──────────────────────────────
# 记忆（向量 + 关系型）
# ──────────────────────────────

async def save_memory(memory: Memory, embedding: list[float]):
    raise RuntimeError("旧记忆写入已停用，请通过 fact_service 发布可审计事实")


async def search_memories(query_embedding: list[float], top_k: int = 5, query_text: str = "") -> list[dict]:
    raise RuntimeError("旧记忆检索已停用，请通过 retriever 读取已发布事实")


# ──────────────────────────────
# 用户档案
# ──────────────────────────────

async def get_user_profile(confirmed_only: bool = False) -> dict:
    with _conn() as c:
        if confirmed_only:
            rows = c.execute(
                "SELECT key, value FROM user_profile WHERE confirmed=1"
            ).fetchall()
        else:
            rows = c.execute("SELECT key, value FROM user_profile").fetchall()
    return {r["key"]: r["value"] for r in rows}


async def set_profile_key(key: str, value: str, confirmed: bool = False):
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO user_profile (key, value, confirmed, updated_at)
            VALUES (?, ?, ?, datetime('now'))
        """, (key, value, 1 if confirmed else 0))


# ──────────────────────────────
# 人设
# ──────────────────────────────

async def get_persona() -> dict:
    raise RuntimeError("旧人格 KV 读取已停用，请读取已发布角色档案快照")


async def set_persona_key(key: str, value: str) -> None:
    raise RuntimeError("旧人格 KV 写入已停用，请通过角色档案审核工作台发布")


# ──────────────────────────────
# 风格
# ──────────────────────────────

async def get_aggregated_style() -> dict:
    with _conn() as c:
        row = c.execute("SELECT style_json FROM aggregated_style WHERE id=1").fetchone()
    return json.loads(row["style_json"]) if row else {}


async def save_aggregated_style(style: dict) -> None:
    raise RuntimeError("旧聚合风格写入已停用，请审核后发布角色档案快照")


# ──────────────────────────────
# 语料
# ──────────────────────────────

async def save_corpus(content: str, source_type: str, style_json: dict) -> None:
    raise RuntimeError("旧语料写入已停用，请通过角色档案素材导入接口处理")


async def get_all_corpus_styles() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT style_json FROM corpus WHERE style_json IS NOT NULL").fetchall()
    result = []
    for r in rows:
        try:
            result.append(json.loads(r["style_json"]))
        except Exception:
            pass
    return result


# ──────────────────────────────
# 主动消息日志
# ──────────────────────────────

async def log_proactive(trigger_type: str, content: str):
    raise RuntimeError("旧主动消息日志已停用，请通过 ProactiveIntent 审计链记录发送结果")


async def get_today_proactive_count() -> int:
    raise RuntimeError("旧主动消息计数已停用，请通过 ProactiveIntent 冷却策略判断资格")


# ──────────────────────────────
# 记忆管理（列表 / 标记更新 / 删除）
# ──────────────────────────────

async def list_memories(
    limit: int = 50,
    offset: int = 0,
    type_filter: str | None = None,
) -> dict:
    raise RuntimeError("旧记忆列表已停用，请通过事实审核接口读取权威记录")


async def update_memory_flags(
    memory_id: str,
    pinned: bool | None = None,
    resolved: bool | None = None,
) -> bool:
    raise RuntimeError("旧向量标记已停用，事实属性变更必须进入可审计状态机")


async def delete_memory_entry(memory_id: str):
    raise RuntimeError("旧记忆删除已停用，请通过事实归档服务处理")


# ──────────────────────────────
# 语料管理
# ──────────────────────────────

async def list_corpus(
    limit: int = 50,
    offset: int = 0,
    source_type: str | None = None,
) -> dict:
    with _conn() as c:
        if source_type:
            rows = c.execute(
                "SELECT id,content,source_type,style_json,created_at FROM corpus "
                "WHERE source_type=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (source_type, limit, offset),
            ).fetchall()
            total = c.execute(
                "SELECT COUNT(*) FROM corpus WHERE source_type=?", (source_type,)
            ).fetchone()[0]
        else:
            rows = c.execute(
                "SELECT id,content,source_type,style_json,created_at FROM corpus "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = c.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]

    return {
        "total": total,
        "items": [
            {
                "id":          r["id"],
                "content":     r["content"],
                "source_type": r["source_type"],
                "style":       json.loads(r["style_json"] or "{}"),
                "created_at":  r["created_at"],
            }
            for r in rows
        ],
    }


async def delete_corpus_entry(entry_id: int) -> None:
    raise RuntimeError("旧语料删除已停用，历史素材只允许在档案层软归档")


async def add_corpus_manual(content: str, source_type: str = "manual") -> None:
    raise RuntimeError("旧语料写入已停用，请通过角色档案素材导入接口处理")


# ──────────────────────────────
# 用户档案 — 列表接口（已有 get/set，补齐 bulk）
# ──────────────────────────────

async def get_profile_full() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT key,value,confirmed,updated_at FROM user_profile"
        ).fetchall()
    return [dict(r) for r in rows]
