-- The ytlm schema as it shipped before PRAGMA user_version (v1, legacy).
-- Frozen: migration tests build an old workspace database from it.
CREATE TABLE IF NOT EXISTS videos (
    
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    channel_title TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT 'UNKNOWN',   -- ISO date or UNKNOWN (D4)
    duration_seconds INTEGER,
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',                -- JSON list
    thumbnail_url TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',              -- Data API defaultAudioLanguage (D13)
    caption_language TEXT NOT NULL DEFAULT '',      -- track actually fetched (D13)
    caption_kind TEXT NOT NULL DEFAULT '',          -- manual | generated | none
                                                    -- | unknown (Supadata road, no Data API)
    metadata_source TEXT NOT NULL DEFAULT '',       -- dataapi | oembed
    has_caption_track INTEGER,                      -- Data API contentDetails.caption
    status TEXT NOT NULL DEFAULT 'added',           -- added | ingested | no_captions
                                                    -- | unavailable | failed
    fetcher TEXT NOT NULL DEFAULT '',               -- provenance of the transcript (D2)
    transcript_state TEXT NOT NULL DEFAULT '',      -- RAW | DETERMINISTIC-ONLY | LLM-CLEANED (D7)
    last_error TEXT NOT NULL DEFAULT '',
    topics TEXT NOT NULL DEFAULT '[]',              -- JSON list; the digest fills it,
    people TEXT NOT NULL DEFAULT '[]',              -- JSON list of name and role; the author's
    perspective TEXT NOT NULL DEFAULT ''            -- word wins (design/corpus-model.md)
);

CREATE TABLE IF NOT EXISTS corpora (
    
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
,
    name TEXT NOT NULL UNIQUE,
    person TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '[]',               -- JSON list (D16)
    perspective TEXT NOT NULL DEFAULT '[]',         -- JSON list
    sources TEXT NOT NULL DEFAULT '[]',             -- JSON list of channel/playlist ids
    auto_ingest INTEGER NOT NULL DEFAULT 0,
    llm_cleanup INTEGER NOT NULL DEFAULT 1          -- per-corpus off switch (D7)
);

CREATE TABLE IF NOT EXISTS corpus_members (
    
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
,
    corpus_id TEXT NOT NULL REFERENCES corpora(id),
    video_id TEXT NOT NULL REFERENCES videos(id),
    reason TEXT NOT NULL,                           -- author | discovered | consumer
    status TEXT NOT NULL DEFAULT 'queued',          -- discovered | queued | ingested | failed
    detail TEXT NOT NULL DEFAULT '',
    UNIQUE (corpus_id, video_id)
);

CREATE TABLE IF NOT EXISTS fetch_attempts (
    
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
,
    video_id TEXT NOT NULL,
    fetcher TEXT NOT NULL,
    outcome TEXT NOT NULL,                          -- ok | skipped | none | error
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS artifacts (
    
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
,
    owner TEXT NOT NULL,                            -- video id or corpus id
    node TEXT NOT NULL,                             -- graph node name (D17)
    path TEXT NOT NULL DEFAULT '',                  -- relative to workspace root, '' if in-db
    sha256 TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'built',            -- built | stale | failed | skipped
    inputs TEXT NOT NULL DEFAULT '{}',            -- JSON: upstream node -> sha256
    provenance TEXT NOT NULL DEFAULT '{}',        -- JSON
    UNIQUE (owner, node)
);

CREATE TABLE IF NOT EXISTS review_verdicts (
    
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
,
    video_id TEXT NOT NULL,
    key TEXT NOT NULL,                              -- amb-NNNN | spk-N (D15)
    kind TEXT NOT NULL,                             -- ambiguity | speaker
    verdict TEXT NOT NULL,                          -- the reading / the name / skip
    span TEXT NOT NULL DEFAULT '',                  -- the ambiguous span, for re-anchoring
    role TEXT NOT NULL DEFAULT '',                  -- for speaker verdicts
    source TEXT NOT NULL DEFAULT 'review file',     -- AUTHOR-RATIFIED provenance
    applied_at TEXT NOT NULL DEFAULT '',
    UNIQUE (video_id, key)
);

CREATE TABLE IF NOT EXISTS speaker_verdicts (
    
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
,
    video_id TEXT NOT NULL,
    role TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (video_id, role)
);

CREATE TABLE IF NOT EXISTS corrections (
    
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
,
    span_norm TEXT NOT NULL UNIQUE,                 -- correction memory key (D26 item 4)
    verdict TEXT NOT NULL,
    kind TEXT NOT NULL,                             -- term | reading
    origin_video TEXT NOT NULL,
    origin_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locks (
    owner TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    rowid INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    t0 REAL NOT NULL,
    t1 REAL NOT NULL,
    speaker TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    UNIQUE (video_id, idx)
);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text, video_id UNINDEXED,
    content='segments', content_rowid='rowid',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text, video_id) VALUES (new.rowid, new.text, new.video_id);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text, video_id)
        VALUES ('delete', old.rowid, old.text, old.video_id);
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text, video_id)
        VALUES ('delete', old.rowid, old.text, old.video_id);
    INSERT INTO segments_fts(rowid, text, video_id) VALUES (new.rowid, new.text, new.video_id);
END;
