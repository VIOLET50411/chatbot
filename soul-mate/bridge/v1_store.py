"""联系人隔离、入站幂等和出站租约的 SQLite 权威存储。"""
from __future__ import annotations

import json
import hashlib
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from bridge.v1_models import InboundEnvelope


BRIDGE_SCHEMA_VERSION = "bridge_contacts_outbox_v1"
BRIDGE_CONTACT_PROFILE_SCHEMA_VERSION = "bridge_contact_profiles_v2"
BRIDGE_CONTACT_RUNTIME_SCHEMA_VERSION = "bridge_contact_runtime_v3"
BRIDGE_INBOUND_BATCH_SCHEMA_VERSION = "bridge_inbound_batches_v4"
BRIDGE_ATTACHMENT_LINK_SCHEMA_VERSION = "bridge_attachment_links_v5"
BRIDGE_MATERIAL_REVIEW_SCHEMA_VERSION = "bridge_material_review_v6"
BRIDGE_CONTACT_PROACTIVE_SCHEMA_VERSION = "bridge_contact_proactive_v7"
BRIDGE_MOMENT_SCHEMA_VERSION = "bridge_moments_v8"
BRIDGE_PERSONA_EVOLUTION_SCHEMA_VERSION = "bridge_persona_evolution_v9"
BRIDGE_PERSONA_BASELINE_SCHEMA_VERSION = "bridge_persona_baseline_v10"
BRIDGE_CONTACT_DISCOVERY_SCHEMA_VERSION = "bridge_contact_discovery_v11"
BRIDGE_CONTACT_MEMORY_LAYERS_SCHEMA_VERSION = "bridge_contact_memory_layers_v12"
BRIDGE_CONTACT_DELIVERY_POLICY_SCHEMA_VERSION = "bridge_contact_delivery_policy_v13"
BRIDGE_WEB_MEDIA_PROVENANCE_SCHEMA_VERSION = "bridge_web_media_provenance_v14"
BRIDGE_MEDIA_PRESENTATION_SCHEMA_VERSION = "bridge_media_presentation_v15"
BRIDGE_VALIDATION_CONTEXT_SCHEMA_VERSION = "bridge_validation_context_v16"


class UnknownContact(LookupError):
    """外部联系人不在启用的白名单中。"""


class BridgeStateConflict(RuntimeError):
    """幂等键或 outbox 状态与请求不一致。"""


class OutboundActionNotFound(LookupError):
    """出站动作不存在。"""


@dataclass(frozen=True, slots=True)
class Contact:
    id: str
    external_id: str
    display_name: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class InboundRecordResult:
    action: str
    event_id: str
    contact_id: str


@dataclass(frozen=True, slots=True)
class AttachmentRecord:
    id: str
    contact_id: str
    kind: str
    storage_path: str
    sha256: str
    mime_type: str
    size_bytes: int
    duplicate: bool


@dataclass(frozen=True, slots=True)
class LegacyContactMigrationResult:
    contact_id: str
    already_applied: bool
    profile_rows: int = 0
    conversation_rows: int = 0
    summary_rows: int = 0
    memory_rows: int = 0


@dataclass(frozen=True, slots=True)
class InboundBatch:
    batch_id: str
    contact_id: str
    event_ids: tuple[str, ...]
    combined_text: str
    attempts: int
    context_mode: str = "live"


@dataclass(frozen=True, slots=True)
class InboundMedia:
    attachment_id: str
    kind: str
    storage_path: str
    mime_type: str
    vision_summary: str


@dataclass(frozen=True, slots=True)
class InboundBatchPart:
    event_id: str
    event_type: str
    text_content: str
    media: tuple[InboundMedia, ...]


@dataclass(frozen=True, slots=True)
class MomentPost:
    id: str
    status: str
    text: str
    attachment_refs: tuple[str, ...]
    privacy_tags: tuple[str, ...]
    source: str
    dispatch_contact_id: str
    outbound_action_id: str | None
    provider_post_id: str | None
    error_summary: str
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoredAction:
    action_id: str
    contact_id: str
    external_contact_id: str
    action_type: str
    payload: dict[str, Any]
    scheduled_at: datetime
    status: str
    attempts: int
    lease_token: str | None
    lease_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class LeasedAction:
    action_id: str
    external_contact_id: str
    action_type: str
    payload: dict[str, Any]
    scheduled_at: datetime
    lease_token: str
    lease_expires_at: datetime
    attempt: int


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        id TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE contacts (
        id TEXT PRIMARY KEY,
        external_id TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE contact_conversations (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
        content TEXT NOT NULL,
        source_event_id TEXT,
        outbound_action_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_contact_conversations_contact_created
    ON contact_conversations(contact_id, created_at DESC)
    """,
    """
    CREATE TABLE contact_memories (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        memory_type TEXT NOT NULL,
        content TEXT NOT NULL,
        importance INTEGER NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
        status TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate', 'published', 'rejected', 'archived')),
        source_event_ids_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_contact_memories_contact_status
    ON contact_memories(contact_id, status, updated_at DESC)
    """,
    """
    CREATE TABLE contact_relationships (
        contact_id TEXT PRIMARY KEY REFERENCES contacts(id) ON DELETE CASCADE,
        stage TEXT NOT NULL DEFAULT '初识',
        trust REAL NOT NULL DEFAULT 20,
        intimacy REAL NOT NULL DEFAULT 10,
        affection REAL NOT NULL DEFAULT 5,
        comfort REAL NOT NULL DEFAULT 50,
        resentment REAL NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE attachments (
        id TEXT PRIMARY KEY,
        contact_id TEXT REFERENCES contacts(id) ON DELETE SET NULL,
        kind TEXT NOT NULL CHECK (kind IN ('image', 'sticker')),
        storage_path TEXT NOT NULL,
        sha256 TEXT NOT NULL UNIQUE,
        mime_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending', 'approved', 'rejected')),
        vision_summary TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE sticker_assets (
        id TEXT PRIMARY KEY,
        attachment_id TEXT NOT NULL UNIQUE REFERENCES attachments(id) ON DELETE CASCADE,
        stable_hash TEXT NOT NULL UNIQUE,
        labels_json TEXT NOT NULL DEFAULT '[]',
        favorite_fingerprint TEXT,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE inbound_events (
        event_id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
        agent_id TEXT NOT NULL,
        message_hash TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN ('text', 'image', 'emoji', 'sticker')),
        text_content TEXT,
        attachment_refs_json TEXT NOT NULL DEFAULT '[]',
        occurred_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'aggregating', 'processing', 'completed', 'failed')),
        error_summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(contact_id, message_hash)
    )
    """,
    """
    CREATE INDEX idx_inbound_events_contact_status
    ON inbound_events(contact_id, status, occurred_at)
    """,
    """
    CREATE TABLE outbound_actions (
        action_id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
        action_type TEXT NOT NULL CHECK (action_type IN ('text', 'image', 'emoji', 'sticker', 'moment')),
        payload_json TEXT NOT NULL,
        dedupe_key TEXT NOT NULL UNIQUE,
        scheduled_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'leased', 'retry_wait', 'sent', 'failed', 'delivery_unknown', 'cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
        leased_by TEXT,
        lease_token TEXT,
        lease_expires_at TEXT,
        provider_message_id TEXT,
        last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        sent_at TEXT
    )
    """,
    """
    CREATE INDEX idx_outbound_actions_ready
    ON outbound_actions(status, scheduled_at, created_at)
    """,
    """
    CREATE TABLE material_reviews (
        id TEXT PRIMARY KEY,
        source_material_id TEXT NOT NULL UNIQUE,
        source_channel TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending', 'keep', 'exclude', 'split', 'corrected')),
        corrected_content TEXT,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        reviewed_by TEXT,
        reviewed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE persona_evolution_events (
        id TEXT PRIMARY KEY,
        dimension TEXT NOT NULL,
        before_json TEXT NOT NULL,
        after_json TEXT NOT NULL,
        evidence_ids_json TEXT NOT NULL,
        confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
        evidence_start_at TEXT NOT NULL,
        evidence_end_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate', 'published', 'rejected', 'rolled_back')),
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        published_at TEXT,
        rolled_back_at TEXT
    )
    """,
    """
    CREATE TABLE bridge_heartbeats (
        agent_id TEXT PRIMARY KEY,
        bridge_version TEXT NOT NULL DEFAULT '',
        wechat_version TEXT NOT NULL DEFAULT '',
        plus_active INTEGER NOT NULL DEFAULT 0 CHECK (plus_active IN (0, 1)),
        window_ready INTEGER NOT NULL DEFAULT 0 CHECK (window_ready IN (0, 1)),
        capabilities_json TEXT NOT NULL DEFAULT '[]',
        last_seen_at TEXT NOT NULL
    )
    """,
)


_CONTACT_PROFILE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE contact_profiles (
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        confirmed INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
        updated_at TEXT NOT NULL,
        PRIMARY KEY(contact_id, key)
    )
    """,
    """
    CREATE TABLE contact_daily_summaries (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        date TEXT NOT NULL,
        summary TEXT NOT NULL,
        mood_arc TEXT,
        key_events_json TEXT NOT NULL DEFAULT '[]',
        relation_change TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(contact_id, date)
    )
    """,
    """
    CREATE INDEX idx_contact_daily_summaries_contact_date
    ON contact_daily_summaries(contact_id, date DESC)
    """,
    """
    CREATE TABLE legacy_contact_migrations (
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        migration_id TEXT NOT NULL,
        migrated_at TEXT NOT NULL,
        profile_rows INTEGER NOT NULL DEFAULT 0,
        conversation_rows INTEGER NOT NULL DEFAULT 0,
        summary_rows INTEGER NOT NULL DEFAULT 0,
        memory_rows INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(contact_id, migration_id)
    )
    """,
)


_ATTACHMENT_LINK_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE attachment_contact_links (
        attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        PRIMARY KEY(attachment_id, contact_id)
    )
    """,
    """
    CREATE INDEX idx_attachment_contact_links_contact
    ON attachment_contact_links(contact_id, attachment_id)
    """,
    """
    INSERT OR IGNORE INTO attachment_contact_links(attachment_id, contact_id, created_at)
    SELECT id, contact_id, created_at FROM attachments WHERE contact_id IS NOT NULL
    """,
)


_MATERIAL_REVIEW_SCHEMA_STATEMENTS = (
    "ALTER TABLE material_reviews ADD COLUMN review_note TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE material_reviews ADD COLUMN split_contents_json TEXT NOT NULL DEFAULT '[]'",
)


_CONTACT_PROACTIVE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE contact_proactive_attempts (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        dedupe_key TEXT NOT NULL UNIQUE,
        intent_type TEXT NOT NULL CHECK (intent_type IN ('share', 'loneliness', 'check_in')),
        status TEXT NOT NULL CHECK (status IN ('generating', 'queued', 'suppressed', 'failed')),
        reason TEXT NOT NULL,
        source_json TEXT NOT NULL DEFAULT '{}',
        message_text TEXT NOT NULL DEFAULT '',
        outbound_action_id TEXT REFERENCES outbound_actions(action_id) ON DELETE SET NULL,
        attempts INTEGER NOT NULL DEFAULT 1,
        error_summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_contact_proactive_recent
    ON contact_proactive_attempts(contact_id, status, created_at DESC)
    """,
)


_MOMENT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE moment_posts (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK (status IN (
            'prepared', 'dispatching', 'published', 'delivery_unknown', 'failed', 'cancelled'
        )),
        text TEXT NOT NULL,
        attachment_refs_json TEXT NOT NULL DEFAULT '[]',
        privacy_tags_json TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('manual', 'manual_test', 'autonomous')),
        dispatch_contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
        outbound_action_id TEXT UNIQUE REFERENCES outbound_actions(action_id) ON DELETE SET NULL,
        provider_post_id TEXT,
        error_summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        dispatched_at TEXT,
        published_at TEXT
    )
    """,
    """
    CREATE INDEX idx_moment_posts_status_created
    ON moment_posts(status, created_at DESC)
    """,
)


_PERSONA_EVOLUTION_SCHEMA_STATEMENTS = (
    "ALTER TABLE persona_evolution_events ADD COLUMN baseline_snapshot_id TEXT",
    "ALTER TABLE persona_evolution_events ADD COLUMN result_snapshot_id TEXT",
    "ALTER TABLE persona_evolution_events ADD COLUMN trigger_kind TEXT NOT NULL DEFAULT 'automatic' CHECK (trigger_kind IN ('automatic', 'manual_review'))",
    """
    CREATE TABLE persona_evolution_control (
        id INTEGER PRIMARY KEY CHECK (id=1),
        paused INTEGER NOT NULL DEFAULT 1 CHECK (paused IN (0, 1)),
        reason TEXT NOT NULL DEFAULT '等待素材审核完成',
        updated_by TEXT NOT NULL DEFAULT 'system',
        updated_at TEXT NOT NULL
    )
    """,
    """
    INSERT INTO persona_evolution_control(id, paused, reason, updated_by, updated_at)
    VALUES (1, 1, '等待素材审核完成', 'system', datetime('now'))
    """,
)


_PERSONA_BASELINE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE persona_baseline_drafts (
        id TEXT PRIMARY KEY,
        character_id TEXT NOT NULL,
        baseline_snapshot_id TEXT NOT NULL,
        result_snapshot_id TEXT,
        review_fingerprint TEXT NOT NULL,
        resolution_json TEXT NOT NULL,
        stable_profile_json TEXT NOT NULL,
        channel_styles_json TEXT NOT NULL,
        selected_claim_ids_json TEXT NOT NULL,
        derived_claims_json TEXT NOT NULL,
        supersede_claim_ids_json TEXT NOT NULL,
        contact_id TEXT,
        contact_delete_keys_json TEXT NOT NULL DEFAULT '[]',
        contact_profile_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft', 'activated', 'stale')),
        note TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        activated_at TEXT
    )
    """,
    """
    CREATE INDEX idx_persona_baseline_drafts_character_status
    ON persona_baseline_drafts(character_id, status, created_at DESC)
    """,
)


_CONTACT_RUNTIME_SCHEMA_STATEMENTS = (
    "ALTER TABLE contact_relationships ADD COLUMN intimacy_context TEXT NOT NULL DEFAULT 'normal'",
    "ALTER TABLE contact_relationships ADD COLUMN last_chat_at TEXT",
    """
    CREATE TABLE contact_memory_embeddings (
        memory_id TEXT PRIMARY KEY REFERENCES contact_memories(id) ON DELETE CASCADE,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        model TEXT NOT NULL,
        dimensions INTEGER NOT NULL,
        vector_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_contact_memory_embeddings_contact
    ON contact_memory_embeddings(contact_id, updated_at DESC)
    """,
    """
    CREATE VIRTUAL TABLE contact_memories_fts
    USING fts5(memory_id UNINDEXED, contact_id UNINDEXED, content, tokenize='unicode61')
    """,
    """
    CREATE TRIGGER contact_memories_fts_insert AFTER INSERT ON contact_memories BEGIN
        INSERT INTO contact_memories_fts(memory_id, contact_id, content)
        VALUES (new.id, new.contact_id, new.content);
    END
    """,
    """
    CREATE TRIGGER contact_memories_fts_update AFTER UPDATE OF content, contact_id ON contact_memories BEGIN
        DELETE FROM contact_memories_fts WHERE memory_id=old.id;
        INSERT INTO contact_memories_fts(memory_id, contact_id, content)
        VALUES (new.id, new.contact_id, new.content);
    END
    """,
    """
    CREATE TRIGGER contact_memories_fts_delete AFTER DELETE ON contact_memories BEGIN
        DELETE FROM contact_memories_fts WHERE memory_id=old.id;
    END
    """,
    """
    INSERT INTO contact_memories_fts(memory_id, contact_id, content)
    SELECT id, contact_id, content FROM contact_memories
    """,
)


_INBOUND_BATCH_SCHEMA_STATEMENTS = (
    "ALTER TABLE inbound_events ADD COLUMN batch_id TEXT",
    """
    CREATE TABLE inbound_batches (
        batch_id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
        status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed')),
        event_ids_json TEXT NOT NULL,
        combined_text TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 1,
        error_summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE INDEX idx_inbound_batches_status_updated
    ON inbound_batches(status, updated_at)
    """,
    """
    CREATE INDEX idx_inbound_events_batch
    ON inbound_events(batch_id, status)
    """,
)


_CONTACT_DISCOVERY_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE contact_candidates (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL UNIQUE,
        suggested_external_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'ignored')),
        seen_count INTEGER NOT NULL DEFAULT 1,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        confirmed_contact_id TEXT REFERENCES contacts(id) ON DELETE SET NULL,
        reviewed_at TEXT
    )
    """,
    """
    CREATE INDEX idx_contact_candidates_status_seen
    ON contact_candidates(status, last_seen_at DESC)
    """,
)


_CONTACT_MEMORY_LAYERS_SCHEMA_STATEMENTS = (
    "ALTER TABLE contact_memories ADD COLUMN memory_layer TEXT NOT NULL DEFAULT 'daily'",
    "ALTER TABLE contact_memories ADD COLUMN valence REAL NOT NULL DEFAULT 0.0",
    "ALTER TABLE contact_memories ADD COLUMN arousal REAL NOT NULL DEFAULT 0.5",
    "ALTER TABLE contact_memories ADD COLUMN initial_strength REAL NOT NULL DEFAULT 1.0",
    "ALTER TABLE contact_memories ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE contact_memories ADD COLUMN last_recalled_at TEXT",
    "ALTER TABLE contact_memories ADD COLUMN expires_at TEXT",
    "ALTER TABLE contact_memories ADD COLUMN source_trace_id TEXT",
    "ALTER TABLE contact_memories ADD COLUMN consolidated_from_json TEXT NOT NULL DEFAULT '[]'",
    """
    UPDATE contact_memories
    SET memory_layer = CASE memory_type
            WHEN 'preference' THEN 'core'
            WHEN 'relationship' THEN 'diary'
            ELSE 'daily'
        END,
        expires_at = CASE
            WHEN memory_type IN ('event', 'emotion') THEN datetime(created_at, '+21 days')
            ELSE NULL
        END,
        source_trace_id = COALESCE(source_trace_id, id)
    """,
    """
    CREATE INDEX idx_contact_memories_layer_expiry
    ON contact_memories(contact_id, memory_layer, status, expires_at)
    """,
)


_CONTACT_DELIVERY_POLICY_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE contact_delivery_policies (
        contact_id TEXT PRIMARY KEY REFERENCES contacts(id) ON DELETE CASCADE,
        proactive_enabled INTEGER NOT NULL DEFAULT 0 CHECK (proactive_enabled IN (0, 1)),
        max_daily INTEGER CHECK (max_daily IS NULL OR (max_daily BETWEEN 1 AND 10)),
        cooldown_hours INTEGER CHECK (cooldown_hours IS NULL OR (cooldown_hours BETWEEN 1 AND 48)),
        active_start_hour INTEGER CHECK (active_start_hour IS NULL OR (active_start_hour BETWEEN 0 AND 23)),
        active_end_hour INTEGER CHECK (active_end_hour IS NULL OR (active_end_hour BETWEEN 1 AND 24)),
        check_in_after_hours INTEGER CHECK (check_in_after_hours IS NULL OR (check_in_after_hours BETWEEN 1 AND 168)),
        updated_at TEXT NOT NULL
    )
    """,
    """
    INSERT INTO contact_delivery_policies(contact_id, proactive_enabled, updated_at)
    SELECT id, 0, datetime('now') FROM contacts
    """,
)


_WEB_MEDIA_PROVENANCE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE web_media_provenance (
        attachment_id TEXT PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,
        source_page_url TEXT NOT NULL,
        image_url TEXT NOT NULL,
        source_platform TEXT NOT NULL,
        attribution TEXT NOT NULL DEFAULT '',
        usage_scope TEXT NOT NULL DEFAULT 'chat' CHECK (usage_scope IN ('chat', 'moments', 'both')),
        claim_mode TEXT NOT NULL DEFAULT 'found_online' CHECK (claim_mode='found_online'),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_web_media_provenance_platform
    ON web_media_provenance(source_platform, created_at DESC)
    """,
)

_MEDIA_PRESENTATION_SCHEMA_STATEMENTS = (
    """
    ALTER TABLE web_media_provenance
    ADD COLUMN presentation_mode TEXT NOT NULL DEFAULT 'source_explicit'
    CHECK (presentation_mode IN ('source_explicit', 'private_roleplay'))
    """,
)


_VALIDATION_CONTEXT_SCHEMA_STATEMENTS = (
    "ALTER TABLE inbound_events ADD COLUMN context_mode TEXT NOT NULL DEFAULT 'live' "
    "CHECK (context_mode IN ('live', 'validation'))",
    "ALTER TABLE inbound_batches ADD COLUMN context_mode TEXT NOT NULL DEFAULT 'live' "
    "CHECK (context_mode IN ('live', 'validation'))",
    "ALTER TABLE inbound_events ADD COLUMN source_type TEXT NOT NULL DEFAULT 'text' "
    "CHECK (source_type IN ('text', 'voice', 'image', 'emoji', 'sticker'))",
    "UPDATE inbound_events SET source_type=event_type",
    "CREATE INDEX idx_inbound_events_contact_mode_status_received "
    "ON inbound_events(contact_id, context_mode, status, created_at)",
)


_BRIDGE_TABLES = (
    "bridge_heartbeats",
    "contact_delivery_policies",
    "moment_posts",
    "contact_proactive_attempts",
    "persona_baseline_drafts",
    "persona_evolution_control",
    "persona_evolution_events",
    "material_reviews",
    "legacy_contact_migrations",
    "contact_daily_summaries",
    "contact_profiles",
    "contact_memory_embeddings",
    "contact_memories_fts",
    "outbound_actions",
    "inbound_batches",
    "inbound_events",
    "sticker_assets",
    "web_media_provenance",
    "attachment_contact_links",
    "attachments",
    "contact_relationships",
    "contact_memories",
    "contact_conversations",
    "contact_candidates",
    "contacts",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value)).astimezone(timezone.utc)


def _json_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class BridgeStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @classmethod
    def from_environment(cls) -> "BridgeStore":
        root = Path(__file__).resolve().parents[1]
        configured = Path(os.getenv("DATA_DIR", "data"))
        data_dir = configured if configured.is_absolute() else root / configured
        return cls(data_dir.resolve() / "soul_mate.db")

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_schema(self) -> None:
        """在单个事务中执行加法迁移；任一语句失败都会完整回滚。"""
        with self._connection(immediate=True) as connection:
            connection.execute(_SCHEMA_STATEMENTS[0])
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_SCHEMA_VERSION,),
            ).fetchone()
            if not applied:
                for statement in _SCHEMA_STATEMENTS[1:]:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_SCHEMA_VERSION,),
                )
            profile_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_CONTACT_PROFILE_SCHEMA_VERSION,),
            ).fetchone()
            if not profile_applied:
                for statement in _CONTACT_PROFILE_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_CONTACT_PROFILE_SCHEMA_VERSION,),
                )
            runtime_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_CONTACT_RUNTIME_SCHEMA_VERSION,),
            ).fetchone()
            if not runtime_applied:
                for statement in _CONTACT_RUNTIME_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_CONTACT_RUNTIME_SCHEMA_VERSION,),
                )
            batch_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_INBOUND_BATCH_SCHEMA_VERSION,),
            ).fetchone()
            if not batch_applied:
                for statement in _INBOUND_BATCH_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_INBOUND_BATCH_SCHEMA_VERSION,),
                )
            attachment_links_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_ATTACHMENT_LINK_SCHEMA_VERSION,),
            ).fetchone()
            if not attachment_links_applied:
                for statement in _ATTACHMENT_LINK_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_ATTACHMENT_LINK_SCHEMA_VERSION,),
                )
            material_review_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_MATERIAL_REVIEW_SCHEMA_VERSION,),
            ).fetchone()
            if not material_review_applied:
                for statement in _MATERIAL_REVIEW_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_MATERIAL_REVIEW_SCHEMA_VERSION,),
                )
            contact_proactive_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_CONTACT_PROACTIVE_SCHEMA_VERSION,),
            ).fetchone()
            if not contact_proactive_applied:
                for statement in _CONTACT_PROACTIVE_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_CONTACT_PROACTIVE_SCHEMA_VERSION,),
                )
            moment_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_MOMENT_SCHEMA_VERSION,),
            ).fetchone()
            if not moment_applied:
                for statement in _MOMENT_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_MOMENT_SCHEMA_VERSION,),
                )
            persona_evolution_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_PERSONA_EVOLUTION_SCHEMA_VERSION,),
            ).fetchone()
            if not persona_evolution_applied:
                for statement in _PERSONA_EVOLUTION_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_PERSONA_EVOLUTION_SCHEMA_VERSION,),
                )
            persona_baseline_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_PERSONA_BASELINE_SCHEMA_VERSION,),
            ).fetchone()
            if not persona_baseline_applied:
                for statement in _PERSONA_BASELINE_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_PERSONA_BASELINE_SCHEMA_VERSION,),
                )
            contact_discovery_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_CONTACT_DISCOVERY_SCHEMA_VERSION,),
            ).fetchone()
            if not contact_discovery_applied:
                for statement in _CONTACT_DISCOVERY_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_CONTACT_DISCOVERY_SCHEMA_VERSION,),
                )
            memory_layers_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_CONTACT_MEMORY_LAYERS_SCHEMA_VERSION,),
            ).fetchone()
            if not memory_layers_applied:
                for statement in _CONTACT_MEMORY_LAYERS_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_CONTACT_MEMORY_LAYERS_SCHEMA_VERSION,),
                )
            delivery_policy_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_CONTACT_DELIVERY_POLICY_SCHEMA_VERSION,),
            ).fetchone()
            if not delivery_policy_applied:
                for statement in _CONTACT_DELIVERY_POLICY_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_CONTACT_DELIVERY_POLICY_SCHEMA_VERSION,),
                )
            web_media_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_WEB_MEDIA_PROVENANCE_SCHEMA_VERSION,),
            ).fetchone()
            if not web_media_applied:
                for statement in _WEB_MEDIA_PROVENANCE_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_WEB_MEDIA_PROVENANCE_SCHEMA_VERSION,),
                )
            media_presentation_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_MEDIA_PRESENTATION_SCHEMA_VERSION,),
            ).fetchone()
            if not media_presentation_applied:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(web_media_provenance)").fetchall()
                }
                if "presentation_mode" not in columns:
                    for statement in _MEDIA_PRESENTATION_SCHEMA_STATEMENTS:
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_MEDIA_PRESENTATION_SCHEMA_VERSION,),
                )
            validation_context_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE id=?",
                (BRIDGE_VALIDATION_CONTEXT_SCHEMA_VERSION,),
            ).fetchone()
            if not validation_context_applied:
                for statement in _VALIDATION_CONTEXT_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(id) VALUES (?)",
                    (BRIDGE_VALIDATION_CONTEXT_SCHEMA_VERSION,),
                )
            self._seed_material_reviews(connection)

    @staticmethod
    def _seed_material_reviews(connection: sqlite3.Connection) -> None:
        """把历史素材登记为待审核，不修改来源记录和当前只读人格快照。"""
        source_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_material'"
        ).fetchone()
        if source_exists is None:
            return
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(source_material)").fetchall()
        }
        if not {"id", "source_type"}.issubset(columns):
            return
        now = _iso(_utc_now())
        connection.execute(
            """
            INSERT OR IGNORE INTO material_reviews(
                id, source_material_id, source_channel, decision,
                corrected_content, evidence_json, reviewed_by, reviewed_at,
                created_at, updated_at, review_note, split_contents_json
            )
            SELECT 'review:' || source.id,
                   source.id,
                   CASE
                       WHEN source.source_type='chat' THEN 'chat'
                       WHEN source.source_type='moments' THEN 'moments'
                       WHEN source.source_type='canonical_setup' THEN 'identity'
                       ELSE source.source_type
                   END,
                   'pending', NULL, '[]', NULL, NULL, ?, ?, '', '[]'
            FROM source_material AS source
            WHERE COALESCE(source.is_deleted, 0)=0
            """,
            (now, now),
        )

    def rollback_schema(self) -> None:
        """仅供迁移恢复工具调用；调用方必须先停止应用并备份当前库。"""
        with self._connection(immediate=True) as connection:
            for table in _BRIDGE_TABLES:
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.execute(
                "DELETE FROM schema_migrations WHERE id IN (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    BRIDGE_SCHEMA_VERSION,
                    BRIDGE_CONTACT_PROFILE_SCHEMA_VERSION,
                    BRIDGE_CONTACT_RUNTIME_SCHEMA_VERSION,
                    BRIDGE_INBOUND_BATCH_SCHEMA_VERSION,
                    BRIDGE_ATTACHMENT_LINK_SCHEMA_VERSION,
                    BRIDGE_MATERIAL_REVIEW_SCHEMA_VERSION,
                    BRIDGE_CONTACT_PROACTIVE_SCHEMA_VERSION,
                    BRIDGE_MOMENT_SCHEMA_VERSION,
                    BRIDGE_PERSONA_EVOLUTION_SCHEMA_VERSION,
                    BRIDGE_PERSONA_BASELINE_SCHEMA_VERSION,
                    BRIDGE_CONTACT_DISCOVERY_SCHEMA_VERSION,
                    BRIDGE_CONTACT_MEMORY_LAYERS_SCHEMA_VERSION,
                    BRIDGE_CONTACT_DELIVERY_POLICY_SCHEMA_VERSION,
                    BRIDGE_WEB_MEDIA_PROVENANCE_SCHEMA_VERSION,
                    BRIDGE_MEDIA_PRESENTATION_SCHEMA_VERSION,
                ),
            )

    def record_contact_candidates(self, display_names: list[str]) -> int:
        """只登记会话显示名，绝不读取未知联系人的消息正文。"""
        names = list(dict.fromkeys(str(item or "").strip() for item in display_names if str(item or "").strip()))
        if len(names) > 100 or any(len(item) > 128 for item in names):
            raise ValueError("联系人候选数量或名称长度超出限制")
        now = _iso(_utc_now())
        changed = 0
        with self._connection(immediate=True) as connection:
            existing_contacts = {
                str(row[0]).casefold()
                for row in connection.execute("SELECT display_name FROM contacts").fetchall()
            }
            for name in names:
                if name.casefold() in existing_contacts:
                    continue
                suggested = "wx:auto:" + hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()[:32]
                result = connection.execute(
                    """
                    INSERT INTO contact_candidates(
                        id, display_name, suggested_external_id, status, seen_count,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, 'pending', 1, ?, ?)
                    ON CONFLICT(display_name) DO UPDATE SET
                        seen_count=contact_candidates.seen_count + 1,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (str(uuid.uuid4()), name, suggested, now, now),
                )
                changed += int(result.rowcount > 0)
        return changed

    def list_contact_candidates(self, *, include_reviewed: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM contact_candidates"
        if not include_reviewed:
            query += " WHERE status='pending'"
        query += " ORDER BY status='pending' DESC, last_seen_at DESC, display_name COLLATE NOCASE"
        with self._connection() as connection:
            rows = connection.execute(query).fetchall()
        return [
            {
                "id": str(row["id"]),
                "display_name": str(row["display_name"]),
                "suggested_external_id": str(row["suggested_external_id"]),
                "status": str(row["status"]),
                "seen_count": int(row["seen_count"]),
                "first_seen_at": str(row["first_seen_at"]),
                "last_seen_at": str(row["last_seen_at"]),
                "confirmed_contact_id": str(row["confirmed_contact_id"] or ""),
            }
            for row in rows
        ]

    def review_contact_candidate(self, candidate_id: str, *, decision: str) -> Contact | None:
        normalized_decision = str(decision or "").strip().lower()
        if normalized_decision not in {"confirm", "ignore"}:
            raise ValueError("联系人候选处理方式无效")
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM contact_candidates WHERE id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError("联系人候选不存在")
            if normalized_decision == "ignore":
                connection.execute(
                    "UPDATE contact_candidates SET status='ignored', reviewed_at=? WHERE id=?",
                    (_iso(_utc_now()), candidate_id),
                )
                return None
            existing = connection.execute(
                "SELECT id, external_id, display_name, enabled FROM contacts WHERE external_id=?",
                (str(row["suggested_external_id"]),),
            ).fetchone()
            if existing is None:
                contact_id = str(uuid.uuid4())
                now = _iso(_utc_now())
                connection.execute(
                    """
                    INSERT INTO contacts(id, external_id, display_name, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (contact_id, str(row["suggested_external_id"]), str(row["display_name"]), now, now),
                )
                connection.execute(
                    """
                    INSERT INTO contact_relationships(contact_id, updated_at)
                    VALUES (?, ?)
                    """,
                    (contact_id, now),
                )
                connection.execute(
                    """
                    INSERT INTO contact_delivery_policies(contact_id, proactive_enabled, updated_at)
                    VALUES (?, 0, ?)
                    """,
                    (contact_id, now),
                )
                existing = connection.execute(
                    "SELECT id, external_id, display_name, enabled FROM contacts WHERE id=?",
                    (contact_id,),
                ).fetchone()
            connection.execute(
                """
                UPDATE contact_candidates
                SET status='confirmed', confirmed_contact_id=?, reviewed_at=?
                WHERE id=?
                """,
                (str(existing["id"]), _iso(_utc_now()), candidate_id),
            )
        return Contact(str(existing["id"]), str(existing["external_id"]), str(existing["display_name"]), bool(existing["enabled"]))

    def create_contact(self, *, external_id: str, display_name: str, enabled: bool = True) -> Contact:
        normalized_external = str(external_id or "").strip()
        normalized_name = str(display_name or "").strip()
        if not normalized_external or len(normalized_external) > 128:
            raise ValueError("微信联系人外部 ID 无效")
        if not normalized_name or len(normalized_name) > 128:
            raise ValueError("联系人显示名无效")
        now = _iso(_utc_now())
        contact_id = str(uuid.uuid4())
        with self._connection(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO contacts(id, external_id, display_name, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(external_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (contact_id, normalized_external, normalized_name, int(enabled), now, now),
            )
            row = connection.execute(
                "SELECT id, external_id, display_name, enabled FROM contacts WHERE external_id=?",
                (normalized_external,),
            ).fetchone()
            if row is None:
                raise RuntimeError("联系人写入失败")
            connection.execute(
                """
                INSERT OR IGNORE INTO contact_relationships(contact_id, updated_at)
                VALUES (?, ?)
                """,
                (row["id"], now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO contact_delivery_policies(contact_id, proactive_enabled, updated_at)
                VALUES (?, 0, ?)
                """,
                (row["id"], now),
            )
        return Contact(str(row["id"]), str(row["external_id"]), str(row["display_name"]), bool(row["enabled"]))

    def get_contact_delivery_policy(self, contact_id: str) -> dict[str, object | None]:
        with self._connection() as connection:
            contact = connection.execute("SELECT 1 FROM contacts WHERE id=?", (contact_id,)).fetchone()
            if contact is None:
                raise UnknownContact("联系人不存在")
            row = connection.execute(
                "SELECT * FROM contact_delivery_policies WHERE contact_id=?",
                (contact_id,),
            ).fetchone()
        if row is None:
            return {
                "proactive_enabled": False,
                "max_daily": None,
                "cooldown_hours": None,
                "active_start_hour": None,
                "active_end_hour": None,
                "check_in_after_hours": None,
            }
        return {
            "proactive_enabled": bool(row["proactive_enabled"]),
            "max_daily": int(row["max_daily"]) if row["max_daily"] is not None else None,
            "cooldown_hours": int(row["cooldown_hours"]) if row["cooldown_hours"] is not None else None,
            "active_start_hour": int(row["active_start_hour"]) if row["active_start_hour"] is not None else None,
            "active_end_hour": int(row["active_end_hour"]) if row["active_end_hour"] is not None else None,
            "check_in_after_hours": int(row["check_in_after_hours"]) if row["check_in_after_hours"] is not None else None,
        }

    def update_contact_delivery_policy(
        self,
        contact_id: str,
        *,
        proactive_enabled: bool | None = None,
        max_daily: int | None = None,
        cooldown_hours: int | None = None,
        active_start_hour: int | None = None,
        active_end_hour: int | None = None,
        check_in_after_hours: int | None = None,
    ) -> dict[str, object | None]:
        values = {
            "max_daily": max_daily,
            "cooldown_hours": cooldown_hours,
            "active_start_hour": active_start_hour,
            "active_end_hour": active_end_hour,
            "check_in_after_hours": check_in_after_hours,
        }
        limits = {
            "max_daily": (1, 10),
            "cooldown_hours": (1, 48),
            "active_start_hour": (0, 23),
            "active_end_hour": (1, 24),
            "check_in_after_hours": (1, 168),
        }
        for key, value in values.items():
            if value is not None and not limits[key][0] <= int(value) <= limits[key][1]:
                raise ValueError(f"联系人主动消息设置超出范围：{key}")
        now = _iso(_utc_now())
        with self._connection(immediate=True) as connection:
            contact = connection.execute("SELECT 1 FROM contacts WHERE id=?", (contact_id,)).fetchone()
            if contact is None:
                raise UnknownContact("联系人不存在")
            connection.execute(
                """
                INSERT OR IGNORE INTO contact_delivery_policies(contact_id, proactive_enabled, updated_at)
                VALUES (?, 0, ?)
                """,
                (contact_id, now),
            )
            current = connection.execute(
                "SELECT * FROM contact_delivery_policies WHERE contact_id=?",
                (contact_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE contact_delivery_policies
                SET proactive_enabled=?, max_daily=?, cooldown_hours=?,
                    active_start_hour=?, active_end_hour=?, check_in_after_hours=?, updated_at=?
                WHERE contact_id=?
                """,
                (
                    int(proactive_enabled) if proactive_enabled is not None else current["proactive_enabled"],
                    max_daily if max_daily is not None else current["max_daily"],
                    cooldown_hours if cooldown_hours is not None else current["cooldown_hours"],
                    active_start_hour if active_start_hour is not None else current["active_start_hour"],
                    active_end_hour if active_end_hour is not None else current["active_end_hour"],
                    check_in_after_hours if check_in_after_hours is not None else current["check_in_after_hours"],
                    now,
                    contact_id,
                ),
            )
        return self.get_contact_delivery_policy(contact_id)

    def get_contact_by_external(self, external_id: str, *, enabled_only: bool = True) -> Contact | None:
        query = "SELECT id, external_id, display_name, enabled FROM contacts WHERE external_id=?"
        params: tuple[object, ...] = (str(external_id or "").strip(),)
        if enabled_only:
            query += " AND enabled=1"
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            return None
        return Contact(str(row["id"]), str(row["external_id"]), str(row["display_name"]), bool(row["enabled"]))

    def get_contact(self, contact_id: str) -> Contact | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, external_id, display_name, enabled FROM contacts WHERE id=?",
                (str(contact_id or "").strip(),),
            ).fetchone()
        if row is None:
            return None
        return Contact(str(row["id"]), str(row["external_id"]), str(row["display_name"]), bool(row["enabled"]))

    def list_contacts(self, *, include_disabled: bool = True) -> list[Contact]:
        query = "SELECT id, external_id, display_name, enabled FROM contacts"
        if not include_disabled:
            query += " WHERE enabled=1"
        query += " ORDER BY enabled DESC, display_name COLLATE NOCASE, id"
        with self._connection() as connection:
            rows = connection.execute(query).fetchall()
        return [
            Contact(str(row["id"]), str(row["external_id"]), str(row["display_name"]), bool(row["enabled"]))
            for row in rows
        ]

    def update_contact(
        self,
        contact_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> Contact:
        if display_name is None and enabled is None:
            raise ValueError("至少需要修改一个联系人字段")
        normalized_name = None
        if display_name is not None:
            normalized_name = str(display_name).strip()
            if not normalized_name or len(normalized_name) > 128:
                raise ValueError("联系人显示名无效")
        now = _iso(_utc_now())
        with self._connection(immediate=True) as connection:
            current = connection.execute(
                "SELECT id, external_id, display_name, enabled FROM contacts WHERE id=?",
                (contact_id,),
            ).fetchone()
            if current is None:
                raise UnknownContact("联系人不存在")
            connection.execute(
                """
                UPDATE contacts
                SET display_name=?, enabled=?, updated_at=?
                WHERE id=?
                """,
                (
                    normalized_name if normalized_name is not None else current["display_name"],
                    int(enabled) if enabled is not None else current["enabled"],
                    now,
                    contact_id,
                ),
            )
            row = connection.execute(
                "SELECT id, external_id, display_name, enabled FROM contacts WHERE id=?",
                (contact_id,),
            ).fetchone()
        return Contact(str(row["id"]), str(row["external_id"]), str(row["display_name"]), bool(row["enabled"]))

    def register_attachment(
        self,
        *,
        external_contact_id: str,
        kind: str,
        storage_path: str,
        sha256: str,
        mime_type: str,
        size_bytes: int,
    ) -> AttachmentRecord:
        """登记内容寻址附件，并显式授权给上传该附件的联系人。"""
        if kind not in {"image", "sticker"}:
            raise ValueError("附件类型无效")
        normalized_sha = str(sha256 or "").strip().lower()
        if len(normalized_sha) != 64 or any(char not in "0123456789abcdef" for char in normalized_sha):
            raise ValueError("附件 SHA-256 无效")
        normalized_path = str(storage_path or "").strip()
        normalized_mime = str(mime_type or "").strip().lower()
        if not normalized_path or len(normalized_path) > 512:
            raise ValueError("附件存储路径无效")
        if not normalized_mime or len(normalized_mime) > 128:
            raise ValueError("附件 MIME 类型无效")
        if int(size_bytes) < 1:
            raise ValueError("附件内容不能为空")

        now = _iso(_utc_now())
        with self._connection(immediate=True) as connection:
            contact = connection.execute(
                "SELECT id FROM contacts WHERE external_id=? AND enabled=1",
                (str(external_contact_id or "").strip(),),
            ).fetchone()
            if contact is None:
                raise UnknownContact("联系人不在白名单中")
            contact_id = str(contact["id"])
            existing = connection.execute(
                """
                SELECT id, kind, storage_path, sha256, mime_type, size_bytes
                FROM attachments WHERE sha256=?
                """,
                (normalized_sha,),
            ).fetchone()
            duplicate = existing is not None
            if existing is None:
                attachment_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO attachments(
                        id, contact_id, kind, storage_path, sha256, mime_type,
                        size_bytes, review_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        attachment_id,
                        contact_id,
                        kind,
                        normalized_path,
                        normalized_sha,
                        normalized_mime,
                        int(size_bytes),
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id, kind, storage_path, sha256, mime_type, size_bytes
                    FROM attachments WHERE id=?
                    """,
                    (attachment_id,),
                ).fetchone()
            else:
                row = existing
                if (
                    str(row["kind"]) != kind
                    or int(row["size_bytes"]) != int(size_bytes)
                    or str(row["mime_type"]) != normalized_mime
                ):
                    raise BridgeStateConflict("相同附件摘要对应了不同的附件元数据")
            connection.execute(
                """
                INSERT OR IGNORE INTO attachment_contact_links(attachment_id, contact_id, created_at)
                VALUES (?, ?, ?)
                """,
                (row["id"], contact_id, now),
            )
        return AttachmentRecord(
            id=str(row["id"]),
            contact_id=contact_id,
            kind=str(row["kind"]),
            storage_path=str(row["storage_path"]),
            sha256=str(row["sha256"]),
            mime_type=str(row["mime_type"]),
            size_bytes=int(row["size_bytes"]),
            duplicate=duplicate,
        )

    def get_attachment_for_contact(
        self,
        *,
        attachment_id: str,
        external_contact_id: str,
    ) -> AttachmentRecord:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT attachment.id, link.contact_id, attachment.kind,
                       attachment.storage_path, attachment.sha256,
                       attachment.mime_type, attachment.size_bytes
                FROM attachments AS attachment
                JOIN attachment_contact_links AS link
                  ON link.attachment_id=attachment.id
                JOIN contacts AS contact ON contact.id=link.contact_id
                WHERE attachment.id=? AND contact.external_id=? AND contact.enabled=1
                """,
                (
                    str(attachment_id or "").strip(),
                    str(external_contact_id or "").strip(),
                ),
            ).fetchone()
        if row is None:
            raise KeyError("附件不存在或未授权给当前联系人")
        return AttachmentRecord(
            id=str(row["id"]),
            contact_id=str(row["contact_id"]),
            kind=str(row["kind"]),
            storage_path=str(row["storage_path"]),
            sha256=str(row["sha256"]),
            mime_type=str(row["mime_type"]),
            size_bytes=int(row["size_bytes"]),
            duplicate=True,
        )

    def list_media_assets(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT attachment.id, attachment.kind, attachment.mime_type,
                       attachment.size_bytes, attachment.review_status,
                       attachment.vision_summary, attachment.created_at,
                       provenance.source_page_url, provenance.image_url,
                       provenance.source_platform, provenance.attribution,
                       provenance.usage_scope, provenance.claim_mode,
                       provenance.presentation_mode,
                       GROUP_CONCAT(contact.display_name, '、') AS contact_names
                FROM attachments AS attachment
                LEFT JOIN web_media_provenance AS provenance ON provenance.attachment_id=attachment.id
                LEFT JOIN attachment_contact_links AS link ON link.attachment_id=attachment.id
                LEFT JOIN contacts AS contact ON contact.id=link.contact_id
                GROUP BY attachment.id
                ORDER BY attachment.created_at DESC, attachment.id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "mime_type": str(row["mime_type"]),
                "size_bytes": int(row["size_bytes"]),
                "review_status": str(row["review_status"]),
                "vision_summary": str(row["vision_summary"] or ""),
                "created_at": str(row["created_at"]),
                "contact_names": str(row["contact_names"] or ""),
                "source_page_url": str(row["source_page_url"] or ""),
                "image_url": str(row["image_url"] or ""),
                "source_platform": str(row["source_platform"] or ""),
                "attribution": str(row["attribution"] or ""),
                "usage_scope": str(row["usage_scope"] or ""),
                "claim_mode": str(row["claim_mode"] or ""),
                "presentation_mode": str(row["presentation_mode"] or "source_explicit"),
            }
            for row in rows
        ]

    def list_approved_media_for_contact(
        self,
        *,
        contact_id: str,
        limit: int = 12,
    ) -> list[dict[str, str]]:
        """返回当前联系人真正可发送的少量媒体摘要，供隐藏决策器选取。"""
        with self._connection() as connection:
            contact = connection.execute(
                "SELECT 1 FROM contacts WHERE id=? AND enabled=1",
                (str(contact_id or "").strip(),),
            ).fetchone()
            if contact is None:
                raise UnknownContact("联系人不存在或已停用")
            rows = connection.execute(
                """
                SELECT attachment.kind, attachment.vision_summary,
                       COALESCE(provenance.presentation_mode, 'source_explicit') AS presentation_mode
                FROM attachments AS attachment
                JOIN attachment_contact_links AS link ON link.attachment_id=attachment.id
                LEFT JOIN web_media_provenance AS provenance ON provenance.attachment_id=attachment.id
                WHERE link.contact_id=?
                  AND attachment.review_status='approved'
                  AND attachment.kind IN ('image', 'sticker')
                  AND TRIM(COALESCE(attachment.vision_summary, ''))<>''
                  AND (
                    attachment.kind<>'image'
                    OR provenance.usage_scope IS NULL
                    OR provenance.usage_scope IN ('chat', 'both')
                  )
                ORDER BY attachment.created_at DESC, attachment.id DESC
                LIMIT ?
                """,
                (str(contact_id or "").strip(), max(1, min(int(limit), 30))),
            ).fetchall()
        return [
            {
                "kind": str(row["kind"]),
                "summary": str(row["vision_summary"]),
                "presentation_mode": str(row["presentation_mode"]),
            }
            for row in rows
        ]

    def register_web_media_provenance(
        self,
        *,
        attachment_id: str,
        source_page_url: str,
        image_url: str,
        source_platform: str,
        attribution: str,
        usage_scope: str,
        vision_summary: str,
        presentation_mode: str = "source_explicit",
    ) -> dict[str, Any]:
        if usage_scope not in {"chat", "moments", "both"}:
            raise ValueError("网络图片用途无效")
        if presentation_mode not in {"source_explicit", "private_roleplay"}:
            raise ValueError("网络图片呈现方式无效")
        if presentation_mode == "private_roleplay" and usage_scope != "chat":
            raise ValueError("沉浸式世界线图片只能用于与指定联系人的私聊")
        now = _iso(_utc_now())
        with self._connection(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM attachments WHERE id=?", (attachment_id,)).fetchone() is None:
                raise KeyError("媒体资产不存在")
            connection.execute(
                """
                INSERT INTO web_media_provenance(
                    attachment_id, source_page_url, image_url, source_platform,
                    attribution, usage_scope, claim_mode, presentation_mode, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'found_online', ?, ?)
                ON CONFLICT(attachment_id) DO UPDATE SET
                    source_page_url=excluded.source_page_url,
                    image_url=excluded.image_url,
                    source_platform=excluded.source_platform,
                    attribution=excluded.attribution,
                    usage_scope=excluded.usage_scope,
                    presentation_mode=excluded.presentation_mode
                """,
                (
                    attachment_id,
                    source_page_url,
                    image_url,
                    source_platform,
                    attribution,
                    usage_scope,
                    presentation_mode,
                    now,
                ),
            )
            connection.execute(
                "UPDATE attachments SET vision_summary=? WHERE id=?",
                (str(vision_summary or "").strip()[:2000], attachment_id),
            )
        return next(item for item in self.list_media_assets() if item["id"] == attachment_id)

    def review_media_asset(self, attachment_id: str, *, review_status: str) -> dict[str, Any]:
        if review_status not in {"pending", "approved", "rejected"}:
            raise ValueError("媒体审核状态无效")
        with self._connection(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE attachments SET review_status=? WHERE id=?",
                (review_status, str(attachment_id or "").strip()),
            )
            if updated.rowcount != 1:
                raise KeyError("媒体资产不存在")
        return next(item for item in self.list_media_assets() if item["id"] == attachment_id)

    def get_media_file(self, attachment_id: str) -> tuple[Path, str]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT storage_path, mime_type FROM attachments WHERE id=?",
                (str(attachment_id or "").strip(),),
            ).fetchone()
        if row is None:
            raise KeyError("媒体资产不存在")
        data_root = self.database_path.parent.resolve()
        resolved = (data_root / str(row["storage_path"])).resolve()
        try:
            resolved.relative_to(data_root)
        except ValueError as exc:
            raise BridgeStateConflict("媒体存储路径越界") from exc
        if not resolved.is_file():
            raise FileNotFoundError("媒体文件不存在")
        return resolved, str(row["mime_type"])

    def find_approved_media_asset(
        self,
        *,
        contact_id: str,
        kind: str,
        query: str = "",
    ) -> dict[str, str] | None:
        if kind not in {"image", "sticker"}:
            return None
        normalized_query = " ".join(str(query or "").split())[:200]
        like_query = f"%{normalized_query}%"
        with self._connection() as connection:
            contact = connection.execute(
                "SELECT 1 FROM contacts WHERE id=? AND enabled=1",
                (contact_id,),
            ).fetchone()
            if contact is None:
                raise UnknownContact("联系人不存在或已停用")
            if kind == "image":
                row = connection.execute(
                    """
                    SELECT attachment.id, attachment.vision_summary,
                           COALESCE(provenance.presentation_mode, 'source_explicit') AS presentation_mode
                    FROM attachments AS attachment
                    JOIN attachment_contact_links AS link ON link.attachment_id=attachment.id
                    LEFT JOIN web_media_provenance AS provenance ON provenance.attachment_id=attachment.id
                    WHERE link.contact_id=? AND attachment.kind='image'
                      AND attachment.review_status='approved'
                      AND (
                        provenance.usage_scope IS NULL
                        OR provenance.usage_scope IN ('chat', 'both')
                      )
                      AND (?='' OR COALESCE(attachment.vision_summary, '') LIKE ?)
                    ORDER BY attachment.created_at DESC LIMIT 1
                    """,
                    (contact_id, normalized_query, like_query),
                ).fetchone()
                return (
                    {
                        "attachment_ref": str(row["id"]),
                        "vision_summary": str(row["vision_summary"] or ""),
                        "presentation_mode": str(row["presentation_mode"]),
                    }
                    if row is not None
                    else None
                )
            row = connection.execute(
                """
                SELECT attachment.id, sticker.favorite_fingerprint, attachment.vision_summary
                FROM sticker_assets AS sticker
                JOIN attachments AS attachment ON attachment.id=sticker.attachment_id
                JOIN attachment_contact_links AS link ON link.attachment_id=attachment.id
                WHERE link.contact_id=? AND attachment.review_status='approved'
                  AND sticker.enabled=1 AND sticker.favorite_fingerprint IS NOT NULL
                  AND (?='' OR COALESCE(attachment.vision_summary, '') LIKE ?)
                ORDER BY attachment.created_at DESC LIMIT 1
                """,
                (contact_id, normalized_query, like_query),
            ).fetchone()
            return (
                {
                    "attachment_ref": str(row["id"]),
                    "favorite_fingerprint": str(row["favorite_fingerprint"]),
                    "vision_summary": str(row["vision_summary"] or ""),
                }
                if row is not None
                else None
            )

    def migrate_legacy_contact(
        self,
        *,
        external_id: str,
        display_name: str,
    ) -> LegacyContactMigrationResult:
        """把旧单联系人数据复制到显式联系人；旧表保留且重复执行不重复写入。"""
        self.initialize_schema()
        contact = self.create_contact(external_id=external_id, display_name=display_name)
        migration_id = "legacy_single_contact_v1"
        now = _iso(_utc_now())
        with self._connection(immediate=True) as connection:
            previous = connection.execute(
                """
                SELECT profile_rows, conversation_rows, summary_rows, memory_rows
                FROM legacy_contact_migrations
                WHERE contact_id=? AND migration_id=?
                """,
                (contact.id, migration_id),
            ).fetchone()
            if previous is not None:
                return LegacyContactMigrationResult(
                    contact_id=contact.id,
                    already_applied=True,
                    profile_rows=int(previous["profile_rows"]),
                    conversation_rows=int(previous["conversation_rows"]),
                    summary_rows=int(previous["summary_rows"]),
                    memory_rows=int(previous["memory_rows"]),
                )

            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            profile_rows = 0
            conversation_rows = 0
            summary_rows = 0
            memory_rows = 0

            if "user_profile" in table_names:
                rows = connection.execute(
                    "SELECT key, value, confirmed, updated_at FROM user_profile"
                ).fetchall()
                for row in rows:
                    result = connection.execute(
                        """
                        INSERT OR IGNORE INTO contact_profiles(contact_id, key, value, confirmed, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            contact.id,
                            row["key"],
                            row["value"],
                            int(row["confirmed"] or 0),
                            str(row["updated_at"] or now),
                        ),
                    )
                    profile_rows += int(result.rowcount == 1)

            if "conversations" in table_names:
                rows = connection.execute(
                    "SELECT id, role, content, created_at FROM conversations ORDER BY id"
                ).fetchall()
                for row in rows:
                    result = connection.execute(
                        """
                        INSERT OR IGNORE INTO contact_conversations(
                            id, contact_id, role, content, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            f"legacy-conversation:{row['id']}",
                            contact.id,
                            row["role"],
                            row["content"],
                            str(row["created_at"] or now),
                        ),
                    )
                    conversation_rows += int(result.rowcount == 1)

            if "daily_summaries" in table_names:
                rows = connection.execute(
                    """
                    SELECT id, date, summary, mood_arc, key_events, relation_change, created_at
                    FROM daily_summaries ORDER BY id
                    """
                ).fetchall()
                for row in rows:
                    result = connection.execute(
                        """
                        INSERT OR IGNORE INTO contact_daily_summaries(
                            id, contact_id, date, summary, mood_arc, key_events_json,
                            relation_change, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"legacy-summary:{row['id']}",
                            contact.id,
                            row["date"],
                            row["summary"],
                            row["mood_arc"],
                            str(row["key_events"] or "[]"),
                            row["relation_change"],
                            str(row["created_at"] or now),
                        ),
                    )
                    summary_rows += int(result.rowcount == 1)

            if "inner_state" in table_names:
                row = connection.execute(
                    """
                    SELECT stage, trust, intimacy, affection, resentment, updated_at
                    FROM inner_state WHERE id=1
                    """
                ).fetchone()
                if row is not None:
                    connection.execute(
                        """
                        UPDATE contact_relationships
                        SET stage=?, trust=?, intimacy=?, affection=?, resentment=?,
                            version=version+1, updated_at=?
                        WHERE contact_id=?
                        """,
                        (
                            str(row["stage"] or "初识"),
                            float(row["trust"] or 0),
                            float(row["intimacy"] or 0),
                            float(row["affection"] or 0),
                            float(row["resentment"] or 0),
                            str(row["updated_at"] or now),
                            contact.id,
                        ),
                    )

            if "memory_fact" in table_names:
                evidence_by_fact: dict[str, list[str]] = {}
                if "memory_fact_evidence" in table_names:
                    for evidence in connection.execute(
                        "SELECT fact_id, source_message_id FROM memory_fact_evidence"
                    ).fetchall():
                        evidence_by_fact.setdefault(str(evidence["fact_id"]), []).append(
                            str(evidence["source_message_id"])
                        )
                rows = connection.execute(
                    """
                    SELECT id, content, memory_type, importance, status, created_at, updated_at
                    FROM memory_fact ORDER BY created_at, id
                    """
                ).fetchall()
                for row in rows:
                    status = str(row["status"] or "candidate")
                    if status not in {"candidate", "published", "rejected", "archived"}:
                        status = "candidate"
                    fact_id = str(row["id"])
                    result = connection.execute(
                        """
                        INSERT OR IGNORE INTO contact_memories(
                            id, contact_id, memory_type, content, importance, status,
                            source_event_ids_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"legacy-memory:{fact_id}",
                            contact.id,
                            str(row["memory_type"] or "event"),
                            str(row["content"] or ""),
                            max(1, min(int(row["importance"] or 3), 5)),
                            status,
                            json.dumps(evidence_by_fact.get(fact_id, []), ensure_ascii=False),
                            str(row["created_at"] or now),
                            str(row["updated_at"] or now),
                        ),
                    )
                    memory_rows += int(result.rowcount == 1)

            connection.execute(
                """
                INSERT INTO legacy_contact_migrations(
                    contact_id, migration_id, migrated_at, profile_rows,
                    conversation_rows, summary_rows, memory_rows
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contact.id,
                    migration_id,
                    now,
                    profile_rows,
                    conversation_rows,
                    summary_rows,
                    memory_rows,
                ),
            )
        return LegacyContactMigrationResult(
            contact_id=contact.id,
            already_applied=False,
            profile_rows=profile_rows,
            conversation_rows=conversation_rows,
            summary_rows=summary_rows,
            memory_rows=memory_rows,
        )

    def record_inbound(self, envelope: InboundEnvelope) -> InboundRecordResult:
        now = _iso(_utc_now())
        stored_event_type = "text" if envelope.type == "voice" else envelope.type
        with self._connection(immediate=True) as connection:
            contact_row = connection.execute(
                "SELECT id FROM contacts WHERE external_id=? AND enabled=1",
                (envelope.external_contact_id,),
            ).fetchone()
            if contact_row is None:
                raise UnknownContact("联系人不在白名单中")
            contact_id = str(contact_row["id"])

            if envelope.type in {"image", "sticker"}:
                placeholders = ",".join("?" for _ in envelope.attachment_refs)
                authorized = connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM attachment_contact_links AS link
                    JOIN attachments AS attachment ON attachment.id=link.attachment_id
                    WHERE link.contact_id=?
                      AND link.attachment_id IN ({placeholders})
                      AND attachment.kind=?
                    """,
                    (contact_id, *envelope.attachment_refs, envelope.type),
                ).fetchone()
                if authorized is None or int(authorized[0]) != len(envelope.attachment_refs):
                    raise BridgeStateConflict("附件不存在、类型不符或未授权给当前联系人")

            existing_event = connection.execute(
                "SELECT contact_id, message_hash FROM inbound_events WHERE event_id=?",
                (envelope.event_id,),
            ).fetchone()
            if existing_event is not None:
                if str(existing_event["contact_id"]) != contact_id or str(existing_event["message_hash"]) != envelope.message_hash:
                    raise BridgeStateConflict("event_id 已被不同消息占用")
                return InboundRecordResult("duplicate", envelope.event_id, contact_id)

            existing_hash = connection.execute(
                "SELECT event_id FROM inbound_events WHERE contact_id=? AND message_hash=?",
                (contact_id, envelope.message_hash),
            ).fetchone()
            if existing_hash is not None:
                return InboundRecordResult("duplicate", str(existing_hash["event_id"]), contact_id)

            connection.execute(
                """
                INSERT INTO inbound_events(
                    event_id, contact_id, agent_id, message_hash, event_type,
                    text_content, attachment_refs_json, occurred_at, created_at, updated_at,
                    context_mode, source_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.event_id,
                    contact_id,
                    envelope.agent_id,
                    envelope.message_hash,
                    stored_event_type,
                    envelope.text,
                    json.dumps(envelope.attachment_refs, ensure_ascii=False),
                    _iso(envelope.occurred_at),
                    now,
                    now,
                    envelope.context_mode,
                    envelope.type,
                ),
            )
        return InboundRecordResult("accepted", envelope.event_id, contact_id)

    def count_inbound(self, *, contact_id: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM inbound_events"
        params: tuple[object, ...] = ()
        if contact_id:
            query += " WHERE contact_id=?"
            params = (contact_id,)
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        return int(row[0]) if row else 0

    def get_inbound_event(self, event_id: str) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT event_id, contact_id, source_type AS event_type, status, batch_id,
                       occurred_at, error_summary
                FROM inbound_events WHERE event_id=?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError("入站事件不存在")
        return dict(row)

    def get_inbound_batch_parts(self, batch_id: str) -> tuple[InboundBatchPart, ...]:
        """按批次原始顺序读取消息和已授权附件，不跨联系人暴露附件。"""
        with self._connection() as connection:
            batch = connection.execute(
                "SELECT contact_id, event_ids_json FROM inbound_batches WHERE batch_id=?",
                (str(batch_id or "").strip(),),
            ).fetchone()
            if batch is None:
                raise KeyError("入站批次不存在")
            try:
                event_ids = [str(item) for item in json.loads(str(batch["event_ids_json"]))]
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise BridgeStateConflict("入站批次事件列表无效") from exc
            if not event_ids:
                return ()
            placeholders = ",".join("?" for _ in event_ids)
            event_rows = connection.execute(
                f"""
                SELECT event_id, source_type AS event_type, text_content, attachment_refs_json
                FROM inbound_events
                WHERE contact_id=? AND event_id IN ({placeholders})
                """,
                (str(batch["contact_id"]), *event_ids),
            ).fetchall()
            events = {str(row["event_id"]): row for row in event_rows}
            if len(events) != len(event_ids):
                raise BridgeStateConflict("入站批次事件不完整")

            result: list[InboundBatchPart] = []
            for event_id in event_ids:
                row = events[event_id]
                try:
                    attachment_ids = [
                        str(item)
                        for item in json.loads(str(row["attachment_refs_json"] or "[]"))
                    ]
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise BridgeStateConflict("入站事件附件列表无效") from exc
                media: tuple[InboundMedia, ...] = ()
                if attachment_ids:
                    media_placeholders = ",".join("?" for _ in attachment_ids)
                    media_rows = connection.execute(
                        f"""
                        SELECT attachment.id, attachment.kind, attachment.storage_path,
                               attachment.mime_type, attachment.vision_summary
                        FROM attachments AS attachment
                        JOIN attachment_contact_links AS link
                          ON link.attachment_id=attachment.id
                        WHERE link.contact_id=?
                          AND attachment.id IN ({media_placeholders})
                        """,
                        (str(batch["contact_id"]), *attachment_ids),
                    ).fetchall()
                    by_id = {str(item["id"]): item for item in media_rows}
                    if len(by_id) != len(attachment_ids):
                        raise BridgeStateConflict("入站附件不存在或不属于当前联系人")
                    media = tuple(
                        InboundMedia(
                            attachment_id=attachment_id,
                            kind=str(by_id[attachment_id]["kind"]),
                            storage_path=str(by_id[attachment_id]["storage_path"]),
                            mime_type=str(by_id[attachment_id]["mime_type"]),
                            vision_summary=str(by_id[attachment_id]["vision_summary"] or ""),
                        )
                        for attachment_id in attachment_ids
                    )
                result.append(
                    InboundBatchPart(
                        event_id=event_id,
                        event_type=str(row["event_type"]),
                        text_content=str(row["text_content"] or "").strip(),
                        media=media,
                    )
                )
        return tuple(result)

    def resolve_attachment_path(self, media: InboundMedia) -> Path:
        data_root = self.database_path.parent.resolve()
        resolved = (data_root / media.storage_path).resolve()
        try:
            resolved.relative_to(data_root)
        except ValueError as exc:
            raise BridgeStateConflict("附件存储路径越界") from exc
        if not resolved.is_file():
            raise FileNotFoundError("附件文件不存在")
        return resolved

    def cache_attachment_vision_summary(self, attachment_id: str, summary: str) -> str:
        normalized = " ".join(str(summary or "").split())[:500]
        if not normalized:
            return ""
        with self._connection(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE attachments
                SET vision_summary=?
                WHERE id=? AND (vision_summary IS NULL OR vision_summary='')
                """,
                (normalized, str(attachment_id or "").strip()),
            )
            row = connection.execute(
                "SELECT vision_summary FROM attachments WHERE id=?",
                (str(attachment_id or "").strip(),),
            ).fetchone()
        if row is None:
            raise KeyError("附件不存在")
        return str(row["vision_summary"] or normalized if updated.rowcount else row["vision_summary"] or "")

    def claim_ready_inbound_batches(
        self,
        *,
        now: datetime | None = None,
        debounce_seconds: int | None = None,
        limit: int = 20,
        stale_after_seconds: int = 300,
    ) -> list[InboundBatch]:
        """在联系人静默 4–8 秒后原子形成稳定批次；测试可显式覆盖窗口。"""
        current = now or _utc_now()
        current_iso = _iso(current)
        stale_cutoff = _iso(current - timedelta(seconds=max(30, int(stale_after_seconds))))
        claimed: list[InboundBatch] = []
        with self._connection(immediate=True) as connection:
            stale_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT batch_id FROM inbound_batches WHERE status='processing' AND updated_at <= ?",
                    (stale_cutoff,),
                ).fetchall()
            ]
            for stale_id in stale_ids:
                connection.execute(
                    """
                    UPDATE inbound_events
                    SET status='pending', batch_id=NULL, updated_at=?
                    WHERE batch_id=? AND status='processing'
                    """,
                    (current_iso, stale_id),
                )
                connection.execute(
                    """
                    UPDATE inbound_batches
                    SET status='failed', error_summary='worker 中断，批次已安全回收', updated_at=?
                    WHERE batch_id=? AND status='processing'
                    """,
                    (current_iso, stale_id),
                )

            contact_rows = connection.execute(
                """
                SELECT ie.contact_id,
                       ie.context_mode,
                       MAX(ie.created_at) AS latest_received_at,
                       MIN(ie.created_at) AS earliest_received_at,
                       MAX(CASE WHEN ie.source_type IN ('voice', 'image', 'sticker') THEN 1 ELSE 0 END)
                           AS has_rich_media
                FROM inbound_events ie
                JOIN contacts c ON c.id=ie.contact_id AND c.enabled=1
                WHERE ie.status='pending'
                GROUP BY ie.contact_id, ie.context_mode
                ORDER BY earliest_received_at
                LIMIT ?
                """,
                (max(1, min(int(limit), 50)),),
            ).fetchall()
            for contact_row in contact_rows:
                contact_id = str(contact_row["contact_id"])
                context_mode = str(contact_row["context_mode"] or "live")
                try:
                    latest_at = datetime.fromisoformat(
                        str(contact_row["latest_received_at"])
                    ).astimezone(timezone.utc)
                except (TypeError, ValueError):
                    latest_at = current
                if debounce_seconds is None:
                    digest = hashlib.blake2b(contact_id.encode("utf-8"), digest_size=1).digest()[0]
                    try:
                        minimum = int(os.getenv("INBOUND_AGGREGATION_MIN_SECONDS", "8"))
                        maximum = int(os.getenv("INBOUND_AGGREGATION_MAX_SECONDS", "8"))
                    except ValueError:
                        minimum, maximum = 8, 8
                    minimum = max(0, min(minimum, 30))
                    maximum = max(minimum, min(maximum, 30))
                    quiet_seconds = minimum + digest % (maximum - minimum + 1)
                else:
                    quiet_seconds = max(0, min(int(debounce_seconds), 30))
                if debounce_seconds is None and bool(contact_row["has_rich_media"]):
                    try:
                        rich_media_quiet = int(os.getenv("INBOUND_RICH_MEDIA_AGGREGATION_SECONDS", "12"))
                    except ValueError:
                        rich_media_quiet = 12
                    quiet_seconds = max(quiet_seconds, max(4, min(rich_media_quiet, 60)))
                if latest_at > current - timedelta(seconds=quiet_seconds):
                    continue
                rows = connection.execute(
                    """
                    SELECT event_id, source_type AS event_type, text_content, attachment_refs_json
                    FROM inbound_events
                    WHERE contact_id=? AND context_mode=? AND status='pending'
                    ORDER BY occurred_at, created_at, event_id
                    LIMIT 50
                    """,
                    (contact_id, context_mode),
                ).fetchall()
                if not rows:
                    continue
                event_ids = tuple(str(row["event_id"]) for row in rows)
                parts: list[str] = []
                for row in rows:
                    text_content = str(row["text_content"] or "").strip()
                    if text_content:
                        parts.append(text_content)
                    elif row["event_type"] == "image":
                        parts.append("[图片]")
                    elif row["event_type"] == "sticker":
                        parts.append("[表情包]")
                    else:
                        parts.append("[消息]")
                combined = "\n".join(parts)
                stable_source = f"{contact_id}|{context_mode}|{'|'.join(event_ids)}"
                batch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, stable_source))
                existing = connection.execute(
                    "SELECT status, attempts FROM inbound_batches WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                if existing is not None and str(existing["status"]) == "completed":
                    connection.execute(
                        "UPDATE inbound_events SET status='completed', batch_id=?, updated_at=? WHERE event_id IN (%s)"
                        % ",".join("?" for _ in event_ids),
                        (batch_id, current_iso, *event_ids),
                    )
                    continue
                if existing is None:
                    attempts = 1
                    connection.execute(
                        """
                        INSERT INTO inbound_batches(
                            batch_id, contact_id, status, event_ids_json, combined_text,
                            attempts, created_at, updated_at, context_mode
                        ) VALUES (?, ?, 'processing', ?, ?, 1, ?, ?, ?)
                        """,
                        (
                            batch_id,
                            contact_id,
                            json.dumps(event_ids, ensure_ascii=False),
                            combined,
                            current_iso,
                            current_iso,
                            context_mode,
                        ),
                    )
                elif str(existing["status"]) == "failed":
                    attempts = int(existing["attempts"]) + 1
                    connection.execute(
                        """
                        UPDATE inbound_batches
                        SET status='processing', attempts=?, error_summary='', updated_at=?, completed_at=NULL
                        WHERE batch_id=? AND status='failed'
                        """,
                        (attempts, current_iso, batch_id),
                    )
                else:
                    continue
                placeholders = ",".join("?" for _ in event_ids)
                updated = connection.execute(
                    f"""
                    UPDATE inbound_events
                    SET status='processing', batch_id=?, updated_at=?
                    WHERE event_id IN ({placeholders}) AND status='pending'
                    """,
                    (batch_id, current_iso, *event_ids),
                )
                if updated.rowcount != len(event_ids):
                    raise BridgeStateConflict("入站批次申请时事件状态发生冲突")
                claimed.append(
                    InboundBatch(
                        batch_id=batch_id,
                        contact_id=contact_id,
                        event_ids=event_ids,
                        combined_text=combined,
                        attempts=attempts,
                        context_mode=context_mode,
                    )
                )
        return claimed

    def extend_processing_inbound_batch(self, batch_id: str) -> InboundBatch:
        """把媒体理解期间刚到达的同联系人消息并入当前批次。"""
        now = _iso(_utc_now())
        with self._connection(immediate=True) as connection:
            batch = connection.execute(
                "SELECT * FROM inbound_batches WHERE batch_id=? AND status='processing'",
                (str(batch_id or "").strip(),),
            ).fetchone()
            if batch is None:
                raise BridgeStateConflict("入站批次不在处理中")
            contact_id = str(batch["contact_id"])
            context_mode = str(batch["context_mode"] or "live")
            rows = connection.execute(
                """
                SELECT event_id, source_type AS event_type, text_content
                FROM inbound_events
                WHERE contact_id=? AND context_mode=?
                  AND (batch_id=? OR status='pending')
                ORDER BY occurred_at, created_at, event_id
                LIMIT 50
                """,
                (contact_id, context_mode, batch_id),
            ).fetchall()
            if not rows:
                raise BridgeStateConflict("入站批次事件不存在")
            event_ids = tuple(str(row["event_id"]) for row in rows)
            current_ids = tuple(str(item) for item in json.loads(str(batch["event_ids_json"])))
            if event_ids != current_ids:
                new_ids = [item for item in event_ids if item not in set(current_ids)]
                if new_ids:
                    placeholders = ",".join("?" for _ in new_ids)
                    updated = connection.execute(
                        f"""
                        UPDATE inbound_events
                        SET status='processing', batch_id=?, updated_at=?
                        WHERE event_id IN ({placeholders}) AND status='pending'
                        """,
                        (batch_id, now, *new_ids),
                    )
                    if updated.rowcount != len(new_ids):
                        raise BridgeStateConflict("扩展入站批次时事件状态发生冲突")
                parts: list[str] = []
                for row in rows:
                    text_content = str(row["text_content"] or "").strip()
                    if text_content:
                        parts.append(text_content)
                    elif row["event_type"] == "image":
                        parts.append("[图片]")
                    elif row["event_type"] == "sticker":
                        parts.append("[表情包]")
                    else:
                        parts.append("[消息]")
                combined = "\n".join(parts)
                connection.execute(
                    """
                    UPDATE inbound_batches
                    SET event_ids_json=?, combined_text=?, updated_at=?
                    WHERE batch_id=? AND status='processing'
                    """,
                    (json.dumps(event_ids, ensure_ascii=False), combined, now, batch_id),
                )
            else:
                combined = str(batch["combined_text"])
        return InboundBatch(
            batch_id=str(batch_id),
            contact_id=contact_id,
            event_ids=event_ids,
            combined_text=combined,
            attempts=int(batch["attempts"]),
            context_mode=context_mode,
        )

    def complete_inbound_batch(self, batch_id: str) -> None:
        now = _iso(_utc_now())
        with self._connection(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE inbound_batches
                SET status='completed', error_summary='', completed_at=?, updated_at=?
                WHERE batch_id=? AND status='processing'
                """,
                (now, now, batch_id),
            )
            if updated.rowcount != 1:
                existing = connection.execute(
                    "SELECT status FROM inbound_batches WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                if existing is not None and existing["status"] == "completed":
                    return
                raise BridgeStateConflict("入站批次完成状态冲突")
            connection.execute(
                """
                UPDATE inbound_events SET status='completed', error_summary='', updated_at=?
                WHERE batch_id=? AND status='processing'
                """,
                (now, batch_id),
            )

    def fail_inbound_batch(self, batch_id: str, error: BaseException | str, *, max_attempts: int = 3) -> None:
        now = _iso(_utc_now())
        summary = f"{type(error).__name__}" if isinstance(error, BaseException) else str(error)
        with self._connection(immediate=True) as connection:
            batch = connection.execute(
                "SELECT status, attempts FROM inbound_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise BridgeStateConflict("入站批次不存在")
            if batch["status"] != "processing":
                return
            retryable = int(batch["attempts"]) < max(1, int(max_attempts))
            connection.execute(
                """
                UPDATE inbound_batches SET status='failed', error_summary=?, updated_at=?
                WHERE batch_id=? AND status='processing'
                """,
                (summary[:200], now, batch_id),
            )
            if retryable:
                connection.execute(
                    """
                    UPDATE inbound_events
                    SET status='pending', batch_id=NULL, error_summary=?, updated_at=?
                    WHERE batch_id=? AND status='processing'
                    """,
                    (summary[:200], now, batch_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE inbound_events
                    SET status='failed', error_summary=?, updated_at=?
                    WHERE batch_id=? AND status='processing'
                    """,
                    (summary[:200], now, batch_id),
                )

    @staticmethod
    def _as_moment_post(row: sqlite3.Row) -> MomentPost:
        attachment_refs = json.loads(str(row["attachment_refs_json"] or "[]"))
        privacy_tags = json.loads(str(row["privacy_tags_json"] or "[]"))
        return MomentPost(
            id=str(row["id"]),
            status=str(row["status"]),
            text=str(row["text"]),
            attachment_refs=tuple(str(item) for item in attachment_refs),
            privacy_tags=tuple(str(item) for item in privacy_tags),
            source=str(row["source"]),
            dispatch_contact_id=str(row["dispatch_contact_id"]),
            outbound_action_id=str(row["outbound_action_id"]) if row["outbound_action_id"] else None,
            provider_post_id=str(row["provider_post_id"]) if row["provider_post_id"] else None,
            error_summary=str(row["error_summary"] or ""),
            created_at=_parse_time(row["created_at"]),  # type: ignore[arg-type]
            updated_at=_parse_time(row["updated_at"]),  # type: ignore[arg-type]
            published_at=_parse_time(row["published_at"]),
        )

    def prepare_moment(
        self,
        *,
        text: str,
        attachment_refs: list[str],
        privacy_tags: list[str],
        source: str,
        now: datetime | None = None,
        minimum_interval_hours: int = 48,
    ) -> MomentPost:
        normalized_text = str(text or "").strip()
        if len(normalized_text) > 1500:
            raise ValueError("朋友圈文字不能超过 1500 个字符")
        normalized_refs = list(dict.fromkeys(str(item).strip() for item in attachment_refs if str(item).strip()))
        if len(normalized_refs) > 9:
            raise ValueError("朋友圈最多包含 9 张图片")
        if not normalized_text and not normalized_refs:
            raise ValueError("朋友圈至少需要文字或图片")
        normalized_tags = list(dict.fromkeys(str(item).strip() for item in privacy_tags if str(item).strip()))
        if not normalized_tags or len(normalized_tags) > 50:
            raise ValueError("朋友圈必须配置 1–50 个可见微信标签")
        if source not in {"manual", "manual_test", "autonomous"}:
            raise ValueError("朋友圈来源无效")
        current = now or _utc_now()
        cutoff = _iso(current - timedelta(hours=max(1, int(minimum_interval_hours))))
        current_iso = _iso(current)
        post_id = str(uuid.uuid4())
        with self._connection(immediate=True) as connection:
            recent = connection.execute(
                """
                SELECT id FROM moment_posts
                WHERE status IN ('prepared','dispatching','published','delivery_unknown')
                  AND created_at>=?
                LIMIT 1
                """,
                (cutoff,),
            ).fetchone()
            if recent is not None:
                raise BridgeStateConflict("距离上一条朋友圈不足 48 小时")

            if normalized_refs:
                placeholders = ",".join("?" for _ in normalized_refs)
                attachment_count = connection.execute(
                    f"""
                    SELECT COUNT(*) FROM attachments
                    WHERE id IN ({placeholders}) AND kind='image' AND review_status='approved'
                    """,
                    tuple(normalized_refs),
                ).fetchone()
                if attachment_count is None or int(attachment_count[0]) != len(normalized_refs):
                    raise BridgeStateConflict("朋友圈图片必须全部来自已审核图片库")
                dispatch_contact = connection.execute(
                    f"""
                    SELECT link.contact_id
                    FROM attachment_contact_links AS link
                    JOIN contacts AS contact ON contact.id=link.contact_id AND contact.enabled=1
                    WHERE link.attachment_id IN ({placeholders})
                    GROUP BY link.contact_id
                    HAVING COUNT(DISTINCT link.attachment_id)=?
                    ORDER BY MIN(link.created_at)
                    LIMIT 1
                    """,
                    (*normalized_refs, len(normalized_refs)),
                ).fetchone()
            else:
                dispatch_contact = connection.execute(
                    "SELECT id AS contact_id FROM contacts WHERE enabled=1 ORDER BY created_at LIMIT 1"
                ).fetchone()
            if dispatch_contact is None:
                raise BridgeStateConflict("没有可用于朋友圈桥接的启用联系人")
            connection.execute(
                """
                INSERT INTO moment_posts(
                    id, status, text, attachment_refs_json, privacy_tags_json,
                    source, dispatch_contact_id, created_at, updated_at
                ) VALUES (?, 'prepared', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post_id,
                    normalized_text,
                    json.dumps(normalized_refs, ensure_ascii=False),
                    json.dumps(normalized_tags, ensure_ascii=False),
                    source,
                    str(dispatch_contact["contact_id"]),
                    current_iso,
                    current_iso,
                ),
            )
            row = connection.execute("SELECT * FROM moment_posts WHERE id=?", (post_id,)).fetchone()
        return self._as_moment_post(row)

    def get_moment(self, post_id: str) -> MomentPost:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM moment_posts WHERE id=?",
                (str(post_id or "").strip(),),
            ).fetchone()
        if row is None:
            raise KeyError("朋友圈记录不存在")
        return self._as_moment_post(row)

    def list_moments(self, *, limit: int = 50) -> list[MomentPost]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM moment_posts ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [self._as_moment_post(row) for row in rows]

    def dispatch_moment(self, post_id: str, *, scheduled_at: datetime | None = None) -> MomentPost:
        post = self.get_moment(post_id)
        if post.status in {"dispatching", "published", "delivery_unknown"}:
            return post
        if post.status != "prepared":
            raise BridgeStateConflict("只有 prepared 朋友圈可以发布")
        dedupe_key = f"moment:{post.id}"
        action = self.enqueue_action(
            contact_id=post.dispatch_contact_id,
            action_type="moment",
            payload={
                "text": post.text,
                "attachment_refs": list(post.attachment_refs),
                "privacy_tags": list(post.privacy_tags),
            },
            scheduled_at=scheduled_at or _utc_now(),
            dedupe_key=dedupe_key,
            max_attempts=3,
        )
        now = _iso(_utc_now())
        with self._connection(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE moment_posts
                SET status='dispatching', outbound_action_id=?, dispatched_at=?, updated_at=?
                WHERE id=? AND status='prepared'
                """,
                (action.action_id, now, now, post.id),
            )
            if updated.rowcount != 1:
                current = connection.execute("SELECT status FROM moment_posts WHERE id=?", (post.id,)).fetchone()
                if current is None or str(current["status"]) != "dispatching":
                    raise BridgeStateConflict("朋友圈发布状态冲突")
        return self.get_moment(post.id)

    def resolve_moment_unknown(self, post_id: str, *, published: bool, note: str) -> MomentPost:
        normalized_note = str(note or "").strip()
        if not normalized_note:
            raise ValueError("人工核验说明不能为空")
        next_status = "published" if published else "failed"
        now = _iso(_utc_now())
        with self._connection(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE moment_posts
                SET status=?, error_summary=?,
                    published_at=CASE WHEN ?='published' THEN ? ELSE published_at END,
                    updated_at=?
                WHERE id=? AND status='delivery_unknown'
                """,
                (next_status, normalized_note[:500], next_status, now, now, post_id),
            )
            if updated.rowcount != 1:
                raise BridgeStateConflict("只有 delivery_unknown 朋友圈可以人工核验")
        return self.get_moment(post_id)

    def reserve_contact_proactive_attempt(
        self,
        *,
        contact_id: str,
        dedupe_key: str,
        intent_type: str,
        reason: str,
        source: dict[str, Any],
        now: datetime,
        cooldown_seconds: int = 4 * 60 * 60,
        max_per_day: int = 4,
        stale_seconds: int = 10 * 60,
    ) -> str | None:
        """原子执行联系人级去重、冷却和日限额，并保留可恢复生成占位。"""
        if intent_type not in {"share", "loneliness", "check_in"}:
            raise ValueError("主动联系意图无效")
        normalized_dedupe = str(dedupe_key or "").strip()
        if not normalized_dedupe or len(normalized_dedupe) > 256:
            raise ValueError("主动联系幂等键无效")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("主动联系时间必须包含时区")
        now_iso = _iso(now)
        stale_cutoff = _iso(now - timedelta(seconds=max(60, int(stale_seconds))))
        cooldown_cutoff = _iso(now - timedelta(seconds=max(0, int(cooldown_seconds))))
        day_start = _iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
        source_json = json.dumps(source, ensure_ascii=False, separators=(",", ":"), default=str)
        attempt_id = str(uuid.uuid4())
        with self._connection(immediate=True) as connection:
            contact = connection.execute(
                "SELECT 1 FROM contacts WHERE id=? AND enabled=1",
                (contact_id,),
            ).fetchone()
            if contact is None:
                raise UnknownContact("联系人不存在或已停用")
            existing = connection.execute(
                """
                SELECT id, status, attempts, updated_at
                FROM contact_proactive_attempts WHERE dedupe_key=?
                """,
                (normalized_dedupe,),
            ).fetchone()
            if existing is not None:
                retryable = (
                    str(existing["status"]) == "failed"
                    or (
                        str(existing["status"]) == "generating"
                        and str(existing["updated_at"]) <= stale_cutoff
                    )
                ) and int(existing["attempts"]) < 3
                if not retryable:
                    return None
                connection.execute(
                    """
                    UPDATE contact_proactive_attempts
                    SET status='generating', attempts=attempts+1, error_summary='', updated_at=?
                    WHERE id=?
                    """,
                    (now_iso, str(existing["id"])),
                )
                return str(existing["id"])

            recent = connection.execute(
                """
                SELECT 1 FROM contact_proactive_attempts
                WHERE contact_id=? AND status='queued' AND created_at>=?
                LIMIT 1
                """,
                (contact_id, cooldown_cutoff),
            ).fetchone()
            daily_count = connection.execute(
                """
                SELECT COUNT(*) FROM contact_proactive_attempts
                WHERE contact_id=? AND status='queued' AND created_at>=?
                """,
                (contact_id, day_start),
            ).fetchone()
            if recent is not None or int(daily_count[0] if daily_count else 0) >= max(1, int(max_per_day)):
                return None
            connection.execute(
                """
                INSERT INTO contact_proactive_attempts(
                    id, contact_id, dedupe_key, intent_type, status, reason,
                    source_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'generating', ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    contact_id,
                    normalized_dedupe,
                    intent_type,
                    str(reason or "")[:200],
                    source_json[:4000],
                    now_iso,
                    now_iso,
                ),
            )
        return attempt_id

    def finish_contact_proactive_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        message_text: str = "",
        outbound_action_id: str | None = None,
        error_summary: str = "",
    ) -> None:
        if status not in {"queued", "suppressed", "failed"}:
            raise ValueError("主动联系完成状态无效")
        normalized_id = str(attempt_id or "").strip()
        with self._connection(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE contact_proactive_attempts
                SET status=?, message_text=?, outbound_action_id=?, error_summary=?, updated_at=?
                WHERE id=? AND status='generating'
                """,
                (
                    status,
                    str(message_text or "").strip()[:500],
                    outbound_action_id,
                    str(error_summary or "")[:200],
                    _iso(_utc_now()),
                    normalized_id,
                ),
            )
            if updated.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM contact_proactive_attempts WHERE id=?",
                    (normalized_id,),
                ).fetchone()
                if row is not None and str(row["status"]) == status:
                    return
                raise BridgeStateConflict("主动联系记录状态冲突")

    def get_contact_proactive_attempt(self, attempt_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM contact_proactive_attempts WHERE id=?",
                (str(attempt_id or "").strip(),),
            ).fetchone()
        if row is None:
            raise KeyError("主动联系记录不存在")
        result = dict(row)
        result["source"] = _json_object(result.pop("source_json", "{}"))
        return result

    def enqueue_action(
        self,
        *,
        contact_id: str,
        action_type: str,
        payload: dict[str, Any],
        scheduled_at: datetime,
        dedupe_key: str,
        max_attempts: int = 3,
    ) -> StoredAction:
        if action_type not in {"text", "image", "emoji", "sticker", "moment"}:
            raise ValueError("非法出站动作类型")
        if not isinstance(payload, dict):
            raise ValueError("出站载荷必须是对象")
        normalized_dedupe_key = str(dedupe_key or "").strip()
        if not normalized_dedupe_key or len(normalized_dedupe_key) > 256:
            raise ValueError("出站幂等键无效")
        encoded_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded_payload.encode("utf-8")) > 128 * 1024:
            raise ValueError("出站载荷过大")
        now = _iso(_utc_now())
        action_id = str(uuid.uuid4())
        with self._connection(immediate=True) as connection:
            contact = connection.execute(
                "SELECT 1 FROM contacts WHERE id=? AND enabled=1",
                (contact_id,),
            ).fetchone()
            if contact is None:
                raise UnknownContact("联系人不存在或已停用")
            connection.execute(
                """
                INSERT INTO outbound_actions(
                    action_id, contact_id, action_type, payload_json, dedupe_key,
                    scheduled_at, max_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (
                    action_id,
                    contact_id,
                    action_type,
                    encoded_payload,
                    normalized_dedupe_key,
                    _iso(scheduled_at),
                    max(1, min(int(max_attempts), 10)),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT oa.*, c.external_id
                FROM outbound_actions oa JOIN contacts c ON c.id=oa.contact_id
                WHERE oa.dedupe_key=?
                """,
                (normalized_dedupe_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("出站动作写入失败")
        return self._as_stored_action(row)

    def _as_stored_action(self, row: sqlite3.Row) -> StoredAction:
        return StoredAction(
            action_id=str(row["action_id"]),
            contact_id=str(row["contact_id"]),
            external_contact_id=str(row["external_id"]),
            action_type=str(row["action_type"]),
            payload=_json_object(row["payload_json"]),
            scheduled_at=_parse_time(row["scheduled_at"]),  # type: ignore[arg-type]
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            lease_token=str(row["lease_token"]) if row["lease_token"] else None,
            lease_expires_at=_parse_time(row["lease_expires_at"]),
        )

    def get_action(self, action_id: str) -> StoredAction:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT oa.*, c.external_id
                FROM outbound_actions oa JOIN contacts c ON c.id=oa.contact_id
                WHERE oa.action_id=?
                """,
                (action_id,),
            ).fetchone()
        if row is None:
            raise OutboundActionNotFound("出站动作不存在")
        return self._as_stored_action(row)

    def get_action_by_dedupe_key(self, dedupe_key: str) -> StoredAction:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT oa.*, c.external_id
                FROM outbound_actions oa JOIN contacts c ON c.id=oa.contact_id
                WHERE oa.dedupe_key=?
                """,
                (dedupe_key,),
            ).fetchone()
        if row is None:
            raise OutboundActionNotFound("出站动作不存在")
        return self._as_stored_action(row)

    def lease_actions(
        self,
        *,
        agent_id: str,
        limit: int = 10,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> list[LeasedAction]:
        normalized_agent = str(agent_id or "").strip()
        if not normalized_agent:
            raise ValueError("代理标识不能为空")
        current = now or _utc_now()
        current_iso = _iso(current)
        expires = current + timedelta(seconds=max(10, min(int(lease_seconds), 120)))
        result: list[LeasedAction] = []
        with self._connection(immediate=True) as connection:
            expired_moments = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT action_id FROM outbound_actions
                    WHERE status='leased' AND action_type='moment'
                      AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                    """,
                    (current_iso,),
                ).fetchall()
            ]
            connection.execute(
                """
                UPDATE outbound_actions
                SET status='delivery_unknown',
                    last_error='代理租约过期，发送结果未知，禁止自动重发',
                    updated_at=?
                WHERE status='leased' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (current_iso, current_iso),
            )
            if expired_moments:
                placeholders = ",".join("?" for _ in expired_moments)
                connection.execute(
                    f"""
                    UPDATE moment_posts
                    SET status='delivery_unknown',
                        error_summary='代理租约过期，发布结果需人工核验', updated_at=?
                    WHERE outbound_action_id IN ({placeholders}) AND status='dispatching'
                    """,
                    (current_iso, *expired_moments),
                )
            rows = connection.execute(
                """
                SELECT oa.*, c.external_id
                FROM outbound_actions oa
                JOIN contacts c ON c.id=oa.contact_id AND c.enabled=1
                WHERE oa.status IN ('queued', 'retry_wait') AND oa.scheduled_at <= ?
                ORDER BY oa.scheduled_at, oa.created_at
                LIMIT ?
                """,
                (current_iso, max(1, min(int(limit), 50))),
            ).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(32)
                updated = connection.execute(
                    """
                    UPDATE outbound_actions
                    SET status='leased', attempts=attempts+1, leased_by=?, lease_token=?,
                        lease_expires_at=?, updated_at=?
                    WHERE action_id=? AND status IN ('queued', 'retry_wait')
                    """,
                    (normalized_agent, token, _iso(expires), current_iso, row["action_id"]),
                )
                if updated.rowcount != 1:
                    continue
                result.append(
                    LeasedAction(
                        action_id=str(row["action_id"]),
                        external_contact_id=str(row["external_id"]),
                        action_type=str(row["action_type"]),
                        payload=_json_object(row["payload_json"]),
                        scheduled_at=_parse_time(row["scheduled_at"]),  # type: ignore[arg-type]
                        lease_token=token,
                        lease_expires_at=expires.astimezone(timezone.utc),
                        attempt=int(row["attempts"]) + 1,
                    )
                )
        return result

    def ack_action(
        self,
        *,
        action_id: str,
        agent_id: str,
        lease_token: str,
        provider_message_id: str | None = None,
    ) -> StoredAction:
        now = _iso(_utc_now())
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT status, leased_by, lease_token, action_type FROM outbound_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise OutboundActionNotFound("出站动作不存在")
            if row["status"] == "sent" and row["leased_by"] == agent_id and row["lease_token"] == lease_token:
                pass
            elif row["status"] != "leased" or row["leased_by"] != agent_id or row["lease_token"] != lease_token:
                raise BridgeStateConflict("ACK 与当前租约不匹配")
            else:
                connection.execute(
                    """
                    UPDATE outbound_actions
                    SET status='sent', provider_message_id=?, last_error='', sent_at=?, updated_at=?
                    WHERE action_id=? AND status='leased' AND leased_by=? AND lease_token=?
                    """,
                    ((provider_message_id or "")[:256] or None, now, now, action_id, agent_id, lease_token),
                )
                if str(row["action_type"]) == "moment":
                    connection.execute(
                        """
                        UPDATE moment_posts
                        SET status='published', provider_post_id=?, error_summary='',
                            published_at=?, updated_at=?
                        WHERE outbound_action_id=? AND status='dispatching'
                        """,
                        ((provider_message_id or "")[:256] or None, now, now, action_id),
                    )
        return self.get_action(action_id)

    def fail_action(
        self,
        *,
        action_id: str,
        agent_id: str,
        lease_token: str,
        reason: str,
        retryable: bool,
        delivery_unknown: bool,
        retry_delay_seconds: int = 5,
    ) -> StoredAction:
        now_dt = _utc_now()
        now = _iso(now_dt)
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT status, leased_by, lease_token, attempts, max_attempts, action_type
                FROM outbound_actions WHERE action_id=?
                """,
                (action_id,),
            ).fetchone()
            if row is None:
                raise OutboundActionNotFound("出站动作不存在")
            if row["status"] in {"retry_wait", "failed", "delivery_unknown"} and row["leased_by"] == agent_id and row["lease_token"] == lease_token:
                return self.get_action(action_id)
            if row["status"] != "leased" or row["leased_by"] != agent_id or row["lease_token"] != lease_token:
                raise BridgeStateConflict("NACK 与当前租约不匹配")
            if delivery_unknown:
                next_status = "delivery_unknown"
                next_scheduled = now
            elif retryable and int(row["attempts"]) < int(row["max_attempts"]):
                next_status = "retry_wait"
                next_scheduled = _iso(now_dt + timedelta(seconds=max(0, min(int(retry_delay_seconds), 300))))
            else:
                next_status = "failed"
                next_scheduled = now
            connection.execute(
                """
                UPDATE outbound_actions
                SET status=?, scheduled_at=?, last_error=?, updated_at=?
                WHERE action_id=? AND status='leased' AND leased_by=? AND lease_token=?
                """,
                (next_status, next_scheduled, str(reason)[:500], now, action_id, agent_id, lease_token),
            )
            if str(row["action_type"]) == "moment" and next_status in {"failed", "delivery_unknown"}:
                connection.execute(
                    """
                    UPDATE moment_posts SET status=?, error_summary=?, updated_at=?
                    WHERE outbound_action_id=? AND status='dispatching'
                    """,
                    (next_status, str(reason)[:500], now, action_id),
                )
        return self.get_action(action_id)

    def record_heartbeat(
        self,
        *,
        agent_id: str,
        bridge_version: str,
        wechat_version: str,
        plus_active: bool,
        window_ready: bool,
        capabilities: list[str],
    ) -> datetime:
        now = _utc_now()
        with self._connection(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO bridge_heartbeats(
                    agent_id, bridge_version, wechat_version, plus_active,
                    window_ready, capabilities_json, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    bridge_version=excluded.bridge_version,
                    wechat_version=excluded.wechat_version,
                    plus_active=excluded.plus_active,
                    window_ready=excluded.window_ready,
                    capabilities_json=excluded.capabilities_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    agent_id,
                    bridge_version,
                    wechat_version,
                    int(plus_active),
                    int(window_ready),
                    json.dumps(capabilities, ensure_ascii=False),
                    _iso(now),
                ),
            )
        return now
