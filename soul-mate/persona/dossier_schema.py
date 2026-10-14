"""角色档案编译层的 SQLite 结构。

每个角色拥有独立数据库，但仍显式保存 character_id，避免导出、审计和跨库迁移时
丢失主体归属。该模块不承载业务逻辑，只定义可重复执行的建表语句。
"""

DOSSIER_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_schema_migration (
    name        TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now')),
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS source_material (
    id                TEXT PRIMARY KEY,
    character_id      TEXT NOT NULL,
    source_type       TEXT NOT NULL CHECK (source_type IN (
        'canonical_setup', 'chat', 'moments', 'douyin_reference',
        'shared_history', 'manual', 'legacy'
    )),
    content           TEXT NOT NULL,
    source_time       TEXT,
    import_meta_json  TEXT NOT NULL DEFAULT '{}',
    reliability       REAL NOT NULL DEFAULT 0.5 CHECK (reliability >= 0 AND reliability <= 1),
    scope             TEXT NOT NULL DEFAULT 'persona',
    legacy_corpus_id  INTEGER UNIQUE,
    content_hash      TEXT NOT NULL,
    is_deleted        INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_material_character_created
    ON source_material(character_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_material_type
    ON source_material(source_type, is_deleted);

CREATE TABLE IF NOT EXISTS persona_claim (
    id                  TEXT PRIMARY KEY,
    character_id        TEXT NOT NULL,
    facet               TEXT NOT NULL CHECK (facet IN (
        'identity', 'value', 'preference', 'boundary', 'habit',
        'communication', 'life_context'
    )),
    claim_key           TEXT NOT NULL,
    claim_value         TEXT NOT NULL,
    context_json        TEXT NOT NULL DEFAULT '{}',
    polarity            TEXT NOT NULL DEFAULT 'supports' CHECK (polarity IN (
        'supports', 'contradicts', 'uncertain'
    )),
    confidence          REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'approved', 'rejected', 'superseded'
    )),
    origin              TEXT NOT NULL DEFAULT 'inference',
    supersedes_claim_id TEXT REFERENCES persona_claim(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at         TEXT,
    reviewed_by         TEXT,
    review_reason       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_persona_claim_character_status
    ON persona_claim(character_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_persona_claim_key
    ON persona_claim(character_id, claim_key, status);

CREATE TABLE IF NOT EXISTS claim_evidence (
    id                 TEXT PRIMARY KEY,
    claim_id           TEXT NOT NULL REFERENCES persona_claim(id) ON DELETE CASCADE,
    source_material_id TEXT NOT NULL REFERENCES source_material(id),
    quote_span_json    TEXT NOT NULL DEFAULT '{}',
    support_kind       TEXT NOT NULL DEFAULT 'supports' CHECK (support_kind IN (
        'supports', 'contradicts', 'context'
    )),
    evidence_weight    REAL NOT NULL DEFAULT 0.5 CHECK (evidence_weight >= 0 AND evidence_weight <= 1),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(claim_id, source_material_id, support_kind)
);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_claim
    ON claim_evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_material
    ON claim_evidence(source_material_id);

CREATE TABLE IF NOT EXISTS persona_snapshot (
    id                   TEXT PRIMARY KEY,
    character_id         TEXT NOT NULL,
    version              INTEGER NOT NULL,
    snapshot_kind        TEXT NOT NULL CHECK (snapshot_kind IN (
        'legacy_migration', 'reviewed'
    )),
    approved_claims_hash TEXT NOT NULL,
    stable_profile_json  TEXT NOT NULL DEFAULT '{}',
    channel_styles_json  TEXT NOT NULL DEFAULT '{}',
    claim_ids_json       TEXT NOT NULL DEFAULT '[]',
    note                 TEXT NOT NULL DEFAULT '',
    is_active            INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    is_read_only         INTEGER NOT NULL DEFAULT 0 CHECK (is_read_only IN (0, 1)),
    is_published         INTEGER NOT NULL DEFAULT 0 CHECK (is_published IN (0, 1)),
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    published_at         TEXT,
    activated_at         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_snapshot_character_version
    ON persona_snapshot(character_id, version);
CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_snapshot_single_active
    ON persona_snapshot(character_id) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS persona_audit_log (
    id          TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    action       TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    actor         TEXT NOT NULL DEFAULT 'system',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_persona_audit_character_created
    ON persona_audit_log(character_id, created_at DESC);
"""

PERSONA_DOSSIER_MIGRATION = "persona_dossier_v1"
PERSONA_SNAPSHOT_LIFECYCLE_MIGRATION = "persona_snapshot_lifecycle_v2"
