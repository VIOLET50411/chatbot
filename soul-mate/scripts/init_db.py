"""SQLite 数据库初始化 — 建表 + 默认数据"""
import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.fact_store import initialize_memory_fact_schema
from bridge.v1_store import BridgeStore


SCHEMA = """
-- 用户身份档案（L5，最稳定层）
CREATE TABLE IF NOT EXISTS user_profile (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    confirmed INTEGER DEFAULT 0,  -- 0=推断 1=已确认
    updated_at TEXT DEFAULT (datetime('now'))
);

-- 人设配置
CREATE TABLE IF NOT EXISTS persona (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- 对话历史（原始记录，保留30天）
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,   -- user / assistant
    content     TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- 每日对话摘要（永久保留）
CREATE TABLE IF NOT EXISTS daily_summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL UNIQUE,  -- YYYY-MM-DD
    summary     TEXT NOT NULL,
    mood_arc    TEXT,   -- 情绪变化轨迹
    key_events  TEXT,   -- JSON list
    relation_change TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- 每日议程。只保存可用于当前活动和主动意图的公开字段。
CREATE TABLE IF NOT EXISTS daily_agenda (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL UNIQUE,
    morning     TEXT,
    afternoon   TEXT,
    evening     TEXT,
    mood_note   TEXT,
    possible_share TEXT,     -- JSON list
    created_at  TEXT DEFAULT (datetime('now'))
);

-- 本地结构迁移审计。迁移只清理历史形态，不产生新的运行时事实或人格数据。
CREATE TABLE IF NOT EXISTS schema_migrations (
    id          TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 内心状态（单行，常更新）
CREATE TABLE IF NOT EXISTS inner_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    happiness       REAL DEFAULT 6.0,
    energy          REAL DEFAULT 7.0,
    irritability    REAL DEFAULT 2.0,
    affection       REAL DEFAULT 5.0,
    loneliness      REAL DEFAULT 3.0,
    trust           REAL DEFAULT 20.0,
    intimacy        REAL DEFAULT 10.0,
    resentment      REAL DEFAULT 0.0,
    stage           TEXT DEFAULT '初识',
    current_activity TEXT DEFAULT '没什么特别的',
    last_chat_time  TEXT,
    last_tick_time  TEXT,
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- 人际关系图谱（L3）
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    relation    TEXT,    -- 朋友/同事/家人/...
    notes       TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- 重要事件（L2，同时写入 ChromaDB 做向量索引）
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    importance  INTEGER DEFAULT 3,   -- 1-5
    event_date  TEXT,
    source      TEXT DEFAULT 'conversation',
    created_at  TEXT DEFAULT (datetime('now'))
);

-- 语料库
CREATE TABLE IF NOT EXISTS corpus (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    source_type TEXT NOT NULL,   -- douyin / moments / chat / manual
    style_json  TEXT,            -- 分析出的风格 JSON
    created_at  TEXT DEFAULT (datetime('now'))
);

-- 聚合风格（所有语料合并后的结果）
CREATE TABLE IF NOT EXISTS aggregated_style (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    style_json  TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- 系统配置（key-value）
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 主动消息日志
CREATE TABLE IF NOT EXISTS proactive_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL,
    content     TEXT NOT NULL,
    sent_at     TEXT DEFAULT (datetime('now'))
);
"""

DEFAULT_PERSONA = {
    "name": "未设置",
    "age": "25",
    "occupation": "未设置",
    "personality": "未设置",
    "speaking_style": "自然、简短、真实",
    "nickname_for_user": "你",
}

DEFAULT_PROFILE = {
    "name": "用户",
}

DEFAULT_CONFIG = {
    "daily_proactive_count": "0",
    "daily_proactive_limit": "5",
    "last_proactive_date": "",
}


def _remove_legacy_agenda_private_thought(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(daily_agenda)").fetchall()
    }
    migration_id = "remove_daily_agenda_private_thought_v1"
    if "private_thought" not in columns:
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations (id) VALUES (?)",
            (migration_id,),
        )
        return

    # SQLite 无法删除列；重建使隐藏内容不再留在可读取的运行时表中。
    connection.execute("""
        CREATE TABLE daily_agenda_without_private_thought (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL UNIQUE,
            morning     TEXT,
            afternoon   TEXT,
            evening     TEXT,
            mood_note   TEXT,
            possible_share TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    connection.execute("""
        INSERT INTO daily_agenda_without_private_thought
        (id, date, morning, afternoon, evening, mood_note, possible_share, created_at)
        SELECT id, date, morning, afternoon, evening, mood_note, possible_share, created_at
        FROM daily_agenda
    """)
    connection.execute("DROP TABLE daily_agenda")
    connection.execute(
        "ALTER TABLE daily_agenda_without_private_thought RENAME TO daily_agenda"
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations (id) VALUES (?)",
        (migration_id,),
    )


def init(data_dir: str | Path) -> None:
    """初始化明确指定的角色工作空间，禁止回落到全局数据目录。"""
    target = Path(data_dir)
    db_path = target / "soul_mate.db"

    target.mkdir(parents=True, exist_ok=True)
    (target / "chroma").mkdir(exist_ok=True)
    (target / "corpus").mkdir(exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL 是角色工作空间的建库属性；运行期连接不应重复修改它，避免并发 reservation 在初始化阶段互锁。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _remove_legacy_agenda_private_thought(conn)
    initialize_memory_fact_schema(conn)

    conn.execute("INSERT OR IGNORE INTO inner_state (id) VALUES (1)")
    conn.execute("INSERT OR IGNORE INTO aggregated_style (id, style_json) VALUES (1, '{}')")

    for k, v in DEFAULT_PERSONA.items():
        conn.execute("INSERT OR IGNORE INTO persona (key, value) VALUES (?, ?)", (k, v))

    for k, v in DEFAULT_PROFILE.items():
        conn.execute("INSERT OR IGNORE INTO user_profile (key, value) VALUES (?, ?)", (k, v))

    for k, v in DEFAULT_CONFIG.items():
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()
    BridgeStore(db_path).initialize_schema()
    print(f"Database initialized: {db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="初始化指定角色的工作空间")
    parser.add_argument("--data-dir", required=True, help="角色工作空间路径")
    arguments = parser.parse_args()
    init(arguments.data_dir)
