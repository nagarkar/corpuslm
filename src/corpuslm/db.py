"""Storage layer: the SQLite connection, the schema and its migrations
(pipeline-and-surfaces.md, "Storage"; every table, column and rule is
mirrored in docs/design/storage-schema.md, and a test fails when the two
differ).

Every catalog table carries the authorlm KnowledgeObject base: id, version,
created_at, created_by, schema_version, metadata. Four kinds of history,
one per kind of table (architecture.md, "History and identity"):

- master data (sources, corpora, memberships and their lists, and the review answers not yet
  landed) is mutable; every changed field is written to `change_log` by trigger;
- decisions and evidence (review and speaker verdicts, the correction
  memory, fetch attempts, the change log itself) are append-only: a
  trigger aborts any UPDATE or DELETE, and the newest row for a slot is
  the one in force (the `vw_current_*` views);
- `runs` holds one row per pipeline run (`ytlm run`), opened and closed once;
- derived state (artifacts, segments and their FTS5 index, the model's suggestions) and
  coordination (locks) are rebuilt or deleted freely and carry no history.

Nothing outside this module and `store.py` (the Repository) speaks SQL.
The schema version is `PRAGMA user_version`; an older database is backed
up, verified, and upgraded in place in one transaction on open.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION, clock
from .identity import canonical_sha256  # noqa: F401  (re-exported: db.canonical_sha256)

# PRAGMA user_version of the schema below. 0 is a database made before the
# version was tracked (the v1 schema, frozen in tests/fixtures/schema_v1.sql).
USER_VERSION = 8

BACKUPS_DIRNAME = "backups"
# How many verified backups `<workspace>/backups/` keeps (owner ruling
# 2026-10-03: "keep the newest three"); older ones made here are deleted
# after a new one passes integrity_check. Other files there are left alone.
BACKUPS_KEPT = 3

KNOWLEDGE_OBJECT_COLUMNS = """
    id TEXT PRIMARY KEY NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata))
"""

REVIEW_ANSWERS_TABLE = f"""CREATE TABLE IF NOT EXISTS review_answers (
    {KNOWLEDGE_OBJECT_COLUMNS},
    corpus_id TEXT NOT NULL REFERENCES corpora(id),
    card_id TEXT NOT NULL CHECK (card_id <> ''),
    verdict TEXT NOT NULL,
    answered_at TEXT NOT NULL CHECK (answered_at <> ''),
    UNIQUE (corpus_id, card_id)
) STRICT;
"""

# Derived state: the clean-up model's top suggestion for each ambiguity, read from the model
# clean-up artifact (`Store.replace_model_suggestions`). One row per (source, review key); the
# candidates are rows of their own, in the model's order.
MODEL_SUGGESTIONS_TABLES = f"""CREATE TABLE IF NOT EXISTS model_suggestions (
    {KNOWLEDGE_OBJECT_COLUMNS},
    source_id TEXT NOT NULL REFERENCES sources(id),
    key TEXT NOT NULL CHECK (key <> ''),
    span_norm TEXT NOT NULL,
    suggestion TEXT NOT NULL,
    suggestion_probability REAL
        CHECK (suggestion_probability IS NULL OR suggestion_probability BETWEEN 0 AND 1),
    model TEXT NOT NULL DEFAULT '',
    artifact_sha256 TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
    UNIQUE (source_id, key)
) STRICT;

CREATE TABLE IF NOT EXISTS model_suggestion_candidates (
    {KNOWLEDGE_OBJECT_COLUMNS},
    suggestion_id TEXT NOT NULL REFERENCES model_suggestions(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    candidate TEXT NOT NULL,
    probability REAL CHECK (probability IS NULL OR probability BETWEEN 0 AND 1),
    UNIQUE (suggestion_id, position)
) STRICT;
"""

# Tracked feeds: the playlists and channels the owner follows as inboxes (nagarkar/ytlm#21), and
# the videos skipped from a scan. A feed is never a corpus; a video's own `source_feeds` rows say
# which feeds it was found through.
FEEDS_TABLES = f"""CREATE TABLE IF NOT EXISTS feeds (
    {KNOWLEDGE_OBJECT_COLUMNS},
    feed_id TEXT NOT NULL UNIQUE CHECK (feed_id <> ''),
    kind TEXT NOT NULL CHECK (kind IN ('playlist', 'channel')),
    title TEXT NOT NULL DEFAULT '',
    last_scanned_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS source_skips (
    {KNOWLEDGE_OBJECT_COLUMNS},
    source_id TEXT NOT NULL UNIQUE CHECK (source_id <> ''),
    feed_id TEXT REFERENCES feeds(feed_id),
    title TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL CHECK (reason <> ''),
    skipped_at TEXT NOT NULL CHECK (skipped_at <> ''),
    lifted_at TEXT
) STRICT;
"""

# The cleaned transcripts' segments and their full-text index (schema 8): the best transcript of a
# source whose raw utterances live in `segments` (Bee's), searched without ever overwriting them.
CLEANED_SEGMENTS_TABLES = """CREATE TABLE IF NOT EXISTS cleaned_segments (
    rowid INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    start_seconds REAL NOT NULL CHECK (start_seconds >= 0),
    end_seconds REAL NOT NULL CHECK (end_seconds >= start_seconds),
    speaker TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    UNIQUE (source_id, position)
) STRICT;

CREATE VIRTUAL TABLE IF NOT EXISTS cleaned_segments_fts USING fts5(
    text, source_id UNINDEXED,
    content='cleaned_segments', content_rowid='rowid',
    tokenize='unicode61'
);
"""

CLEANED_SEGMENTS_TRIGGERS = """CREATE TRIGGER IF NOT EXISTS cleaned_segments_ai
AFTER INSERT ON cleaned_segments BEGIN
    INSERT INTO cleaned_segments_fts(rowid, text, source_id)
        VALUES (new.rowid, new.text, new.source_id);
END;
CREATE TRIGGER IF NOT EXISTS cleaned_segments_ad
AFTER DELETE ON cleaned_segments BEGIN
    INSERT INTO cleaned_segments_fts(cleaned_segments_fts, rowid, text, source_id)
        VALUES ('delete', old.rowid, old.text, old.source_id);
END;
CREATE TRIGGER IF NOT EXISTS cleaned_segments_au
AFTER UPDATE ON cleaned_segments BEGIN
    INSERT INTO cleaned_segments_fts(cleaned_segments_fts, rowid, text, source_id)
        VALUES ('delete', old.rowid, old.text, old.source_id);
    INSERT INTO cleaned_segments_fts(rowid, text, source_id)
        VALUES (new.rowid, new.text, new.source_id);
END;"""

TABLES = f"""
CREATE TABLE IF NOT EXISTS sources (
    {KNOWLEDGE_OBJECT_COLUMNS},
    kind TEXT NOT NULL DEFAULT 'youtube' CHECK (kind IN ('youtube', 'bee')),
    locator TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    channel_title TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT 'UNKNOWN',
    duration_seconds INTEGER CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    description TEXT NOT NULL DEFAULT '',
    thumbnail_url TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    caption_language TEXT NOT NULL DEFAULT '',
    caption_kind TEXT NOT NULL DEFAULT ''
        CHECK (caption_kind IN ('', 'manual', 'generated', 'none', 'unknown')),
    metadata_source TEXT NOT NULL DEFAULT ''
        CHECK (metadata_source IN ('', 'dataapi', 'oembed')),
    has_caption_track INTEGER CHECK (has_caption_track IS NULL OR has_caption_track IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'added'
        CHECK (status IN ('added', 'ingested', 'no_captions', 'unavailable', 'failed')),
    fetcher TEXT NOT NULL DEFAULT '',
    transcript_state TEXT NOT NULL DEFAULT ''
        CHECK (transcript_state IN ('', 'RAW', 'DETERMINISTIC-ONLY', 'LLM-CLEANED')),
    last_error TEXT NOT NULL DEFAULT '',
    perspective TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS source_tags (
    {KNOWLEDGE_OBJECT_COLUMNS},
    source_id TEXT NOT NULL REFERENCES sources(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    tag TEXT NOT NULL,
    UNIQUE (source_id, position)
) STRICT;

CREATE TABLE IF NOT EXISTS source_topics (
    {KNOWLEDGE_OBJECT_COLUMNS},
    source_id TEXT NOT NULL REFERENCES sources(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    topic TEXT NOT NULL,
    UNIQUE (source_id, position)
) STRICT;

CREATE TABLE IF NOT EXISTS source_people (
    {KNOWLEDGE_OBJECT_COLUMNS},
    source_id TEXT NOT NULL REFERENCES sources(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    UNIQUE (source_id, position)
) STRICT;

CREATE TABLE IF NOT EXISTS source_feeds (
    {KNOWLEDGE_OBJECT_COLUMNS},
    source_id TEXT NOT NULL REFERENCES sources(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    feed_id TEXT NOT NULL,
    UNIQUE (source_id, position)
) STRICT;

CREATE TABLE IF NOT EXISTS corpora (
    {KNOWLEDGE_OBJECT_COLUMNS},
    name TEXT NOT NULL UNIQUE,
    person TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    auto_ingest INTEGER NOT NULL DEFAULT 0 CHECK (auto_ingest IN (0, 1)),
    llm_cleanup INTEGER NOT NULL DEFAULT 1 CHECK (llm_cleanup IN (0, 1))
) STRICT;

CREATE TABLE IF NOT EXISTS corpus_topics (
    {KNOWLEDGE_OBJECT_COLUMNS},
    corpus_id TEXT NOT NULL REFERENCES corpora(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    topic TEXT NOT NULL,
    UNIQUE (corpus_id, position)
) STRICT;

CREATE TABLE IF NOT EXISTS corpus_perspectives (
    {KNOWLEDGE_OBJECT_COLUMNS},
    corpus_id TEXT NOT NULL REFERENCES corpora(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    perspective TEXT NOT NULL,
    UNIQUE (corpus_id, position)
) STRICT;

CREATE TABLE IF NOT EXISTS corpus_members (
    {KNOWLEDGE_OBJECT_COLUMNS},
    corpus_id TEXT NOT NULL REFERENCES corpora(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    reason TEXT NOT NULL CHECK (reason IN ('author', 'discovered', 'consumer')),
    UNIQUE (corpus_id, source_id)
) STRICT;

CREATE TABLE IF NOT EXISTS fetch_attempts (
    {KNOWLEDGE_OBJECT_COLUMNS},
    source_id TEXT NOT NULL REFERENCES sources(id),
    fetcher TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'skipped', 'none', 'error')),
    detail TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE IF NOT EXISTS artifacts (
    {KNOWLEDGE_OBJECT_COLUMNS},
    source_id TEXT REFERENCES sources(id),
    corpus_id TEXT REFERENCES corpora(id),
    owner TEXT GENERATED ALWAYS AS (coalesce(source_id, corpus_id)) VIRTUAL,
    node TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'built'
        CHECK (state IN ('built', 'stale', 'failed', 'skipped')),
    inputs TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(inputs)),
    provenance TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(provenance)),
    CHECK ((source_id IS NULL) <> (corpus_id IS NULL)),
    UNIQUE (source_id, node),
    UNIQUE (corpus_id, node)
) STRICT;

CREATE TABLE IF NOT EXISTS review_verdicts (
    {KNOWLEDGE_OBJECT_COLUMNS},
    source_id TEXT NOT NULL REFERENCES sources(id),
    key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('ambiguity', 'speaker')),
    verdict TEXT NOT NULL,
    span TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'review file' CHECK (source IN ('review file', 'phone page')),
    applied_at TEXT NOT NULL,
    subject_sha256 TEXT NOT NULL CHECK (length(subject_sha256) = 64),
    previous_subject_sha256 TEXT
        CHECK (previous_subject_sha256 IS NULL OR length(previous_subject_sha256) = 64)
) STRICT;

CREATE TABLE IF NOT EXISTS speaker_verdicts (
    {KNOWLEDGE_OBJECT_COLUMNS},
    source_id TEXT NOT NULL REFERENCES sources(id),
    role TEXT NOT NULL,
    name TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS corrections (
    {KNOWLEDGE_OBJECT_COLUMNS},
    span_norm TEXT NOT NULL CHECK (span_norm <> ''),
    verdict TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('term', 'reading')),
    origin_source TEXT NOT NULL REFERENCES sources(id),
    origin_key TEXT NOT NULL
) STRICT;

{REVIEW_ANSWERS_TABLE}
{MODEL_SUGGESTIONS_TABLES}
{FEEDS_TABLES}
{CLEANED_SEGMENTS_TABLES}
CREATE TABLE IF NOT EXISTS runs (
    {KNOWLEDGE_OBJECT_COLUMNS},
    scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'error')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sources INTEGER CHECK (sources IS NULL OR sources >= 0),
    failed_items INTEGER CHECK (failed_items IS NULL OR failed_items >= 0),
    detail TEXT NOT NULL DEFAULT '',
    CHECK ((status = 'running') = (finished_at IS NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS change_log (
    {KNOWLEDGE_OBJECT_COLUMNS},
    table_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    column_name TEXT NOT NULL,
    old_value ANY,
    new_value ANY,
    row_version INTEGER
) STRICT;

CREATE TABLE IF NOT EXISTS locks (
    owner TEXT PRIMARY KEY NOT NULL,
    holder TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS segments (
    rowid INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    start_seconds REAL NOT NULL CHECK (start_seconds >= 0),
    end_seconds REAL NOT NULL CHECK (end_seconds >= start_seconds),
    speaker TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    UNIQUE (source_id, position)
) STRICT;

CREATE INDEX IF NOT EXISTS review_verdicts_slot ON review_verdicts (source_id, key);
CREATE INDEX IF NOT EXISTS speaker_verdicts_slot ON speaker_verdicts (source_id, role);
CREATE INDEX IF NOT EXISTS corrections_slot ON corrections (span_norm);
CREATE INDEX IF NOT EXISTS corpus_members_source ON corpus_members (source_id);
CREATE INDEX IF NOT EXISTS change_log_row ON change_log (table_name, row_id);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text, source_id UNINDEXED,
    content='segments', content_rowid='rowid',
    tokenize='unicode61'
);
"""

# Master data: every column whose change is written to change_log by trigger.
MASTER_COLUMNS: dict[str, tuple[str, ...]] = {
    "sources": (
        "kind", "locator", "title", "channel_id", "channel_title", "published_at",
        "duration_seconds",
        "description", "thumbnail_url", "language", "caption_language", "caption_kind",
        "metadata_source", "has_caption_track", "status", "fetcher", "transcript_state",
        "last_error", "perspective", "metadata",
    ),
    "corpora": ("name", "person", "subject", "auto_ingest", "llm_cleanup", "metadata"),
    "corpus_members": ("reason", "metadata"),
    "review_answers": ("verdict", "answered_at", "metadata"),
    "feeds": ("kind", "title", "last_scanned_at", "metadata"),
    "source_skips": ("feed_id", "title", "reason", "skipped_at", "lifted_at", "metadata"),
}  # fmt: skip

# Lists of a master row, one row per element: (table, parent column, the
# list's name in change_log, the element column(s)).
LIST_TABLES: tuple[tuple[str, str, str, str], ...] = (
    ("source_tags", "source_id", "tags", "{r}.tag"),
    ("source_topics", "source_id", "topics", "{r}.topic"),
    ("source_people", "source_id", "people", "json_object('name', {r}.name, 'role', {r}.role)"),
    ("source_feeds", "source_id", "feeds", "{r}.feed_id"),
    ("corpus_topics", "corpus_id", "topic", "{r}.topic"),
    ("corpus_perspectives", "corpus_id", "perspective", "{r}.perspective"),
)

APPEND_ONLY = ("review_verdicts", "speaker_verdicts", "corrections", "fetch_attempts", "change_log")


_LOG_COLUMNS = (
    "id, version, created_at, created_by, schema_version, metadata, "
    "table_name, row_id, column_name, old_value, new_value, row_version"
)


def _log_values(table: str, row_id: str, column: str, old: str, new: str, version: str) -> str:
    return (
        "'chg-' || lower(hex(randomblob(6))), 1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
        f"'trigger', '{SCHEMA_VERSION}', '{{}}', '{table}', {row_id}, '{column}', "
        f"{old}, {new}, {version}"
    )


def _master_trigger(table: str, columns: tuple[str, ...]) -> str:
    body = "\n".join(
        f"    INSERT INTO change_log ({_LOG_COLUMNS})\n"
        f"    SELECT {_log_values(table, 'new.id', c, f'old.{c}', f'new.{c}', 'new.version')}"
        f"\n    WHERE old.{c} IS NOT new.{c};"
        for c in columns
    )
    return (
        f"CREATE TRIGGER IF NOT EXISTS {table}_change_log AFTER UPDATE ON {table} BEGIN\n"
        f"{body}\nEND;"
    )


def _triggers() -> str:
    out = [_master_trigger(table, columns) for table, columns in MASTER_COLUMNS.items()]
    for table, parent, name, element in LIST_TABLES:
        new_el = element.format(r="new")
        old_el = element.format(r="old")
        out.append(
            f"CREATE TRIGGER IF NOT EXISTS {table}_log_add AFTER INSERT ON {table} BEGIN\n"
            f"    INSERT INTO change_log ({_LOG_COLUMNS}) VALUES "
            f"({_log_values(table, f'new.{parent}', name, 'NULL', new_el, 'NULL')});\nEND;"
        )
        out.append(
            f"CREATE TRIGGER IF NOT EXISTS {table}_log_remove AFTER DELETE ON {table} BEGIN\n"
            f"    INSERT INTO change_log ({_LOG_COLUMNS}) VALUES "
            f"({_log_values(table, f'old.{parent}', name, old_el, 'NULL', 'NULL')});\nEND;"
        )
    for table in APPEND_ONLY:
        for event in ("UPDATE", "DELETE"):
            out.append(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{event.lower()} BEFORE {event} "
                f"ON {table} BEGIN\n    SELECT RAISE(ABORT, '{table} is append-only: "
                f"write a new row instead of {event}');\nEND;"
            )
    out.append(
        """CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text, source_id) VALUES (new.rowid, new.text, new.source_id);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text, source_id)
        VALUES ('delete', old.rowid, old.text, old.source_id);
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text, source_id)
        VALUES ('delete', old.rowid, old.text, old.source_id);
    INSERT INTO segments_fts(rowid, text, source_id) VALUES (new.rowid, new.text, new.source_id);
END;"""
    )
    out.append(CLEANED_SEGMENTS_TRIGGERS)
    return "\n".join(out)


VIEWS = """
CREATE VIEW IF NOT EXISTS vw_corpus_members AS
SELECT m.id, m.corpus_id, m.source_id, m.reason, m.created_at,
       CASE v.status WHEN 'added' THEN 'queued' WHEN 'ingested' THEN 'ingested'
                     ELSE 'failed' END AS member_status
FROM corpus_members m JOIN sources v ON v.id = m.source_id;

CREATE VIEW IF NOT EXISTS vw_search_segments AS
SELECT s.rowid AS segment_rowid, s.source_id, s.position, s.start_seconds, s.end_seconds,
       s.speaker, s.text, v.title
FROM segments s JOIN sources v ON v.id = s.source_id;

CREATE VIEW IF NOT EXISTS vw_current_review_verdicts AS
SELECT * FROM review_verdicts
WHERE rowid IN (SELECT max(rowid) FROM review_verdicts GROUP BY source_id, key);

CREATE VIEW IF NOT EXISTS vw_current_speaker_verdicts AS
SELECT * FROM speaker_verdicts
WHERE rowid IN (SELECT max(rowid) FROM speaker_verdicts GROUP BY source_id, role);

CREATE VIEW IF NOT EXISTS vw_current_corrections AS
SELECT * FROM corrections
WHERE rowid IN (SELECT max(rowid) FROM corrections GROUP BY span_norm);
"""

TRIGGERS = _triggers()
SCHEMA = TABLES + "\n" + TRIGGERS + "\n" + VIEWS


class CorruptValue(ValueError):
    """A stored JSON value that does not parse: bad data is never read as a default."""


class NewerDatabase(RuntimeError):
    """The database was written by a newer corpuslm (its user_version is higher)."""


class MigrationFailed(RuntimeError):
    """A migration or its backup did not verify; the database is left as it was."""


def now_iso(now: datetime | None = None) -> str:
    """`now` (default: the clock) as the stamp every row carries."""
    return clock.iso(now or clock.utc_now())


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def ko_fields(
    prefix: str | None = None, *, id: str | None = None, created_by: str = "cli"
) -> dict[str, Any]:
    """Fresh KnowledgeObject base fields; the single constructor for every
    table. `created_by` is the client id (cli | podlm | authorlm | runner) (D16)."""
    if id is None:
        if prefix is None:
            raise ValueError("ko_fields needs a prefix or an id")
        id = new_id(prefix)
    return {
        "id": id,
        "version": 1,
        "created_at": now_iso(),
        "created_by": created_by,
        "schema_version": SCHEMA_VERSION,
        "metadata": json.dumps({"user": getpass.getuser()}),
    }


def loads(value: str | None, default: Any) -> Any:
    """A stored JSON value. NULL (None) is an honest absence and reads as
    `default`; anything else must parse, and a value that does not raises
    CorruptValue naming it (coding.md, "Failure": no default on bad data)."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as e:
        raise CorruptValue(f"stored JSON does not parse ({e}): {str(value)[:80]!r}") from e


# ---- migrations -------------------------------------------------------------

# Columns the v1 schema gained after it first shipped; a v1 database made
# before them gets them before the v1 → v2 rebuild reads it.
V1_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "videos": (
        ("topics", "TEXT NOT NULL DEFAULT '[]'"),
        ("people", "TEXT NOT NULL DEFAULT '[]'"),
        ("perspective", "TEXT NOT NULL DEFAULT ''"),
    ),
}

V1_TABLES = (
    "videos", "corpora", "corpus_members", "fetch_attempts", "artifacts",
    "review_verdicts", "speaker_verdicts", "corrections", "locks", "segments",
)  # fmt: skip

# The tables whose rows are irreplaceable author work; a backup must carry
# every one of their rows before a migration may start.
AUTHOR_TABLES = ("review_verdicts", "speaker_verdicts", "corrections", "review_answers")


def _ko_copy(prefix: str, created_at: str = "p.created_at") -> str:
    return (
        f"'{prefix}-' || lower(hex(randomblob(6))), 1, {created_at}, 'migration', "
        f"'{SCHEMA_VERSION}', '{{}}'"
    )


KO = "id, version, created_at, created_by, schema_version, metadata"

# v1 → v2: STRICT tables with CHECK and FOREIGN KEY constraints, JSON lists
# into one row per element, segment columns renamed, membership status
# derived from the source's, decision tables append-only with a content hash.
# The v1 tables are renamed to legacy_v1_* first, read, then dropped.
V1_TO_V2_COPY = f"""
INSERT INTO sources ({KO}, kind, locator, title, channel_id, channel_title, published_at,
    duration_seconds, description, thumbnail_url, language, caption_language, caption_kind,
    metadata_source, has_caption_track, status, fetcher, transcript_state, last_error,
    perspective)
SELECT {KO}, 'youtube', url, title, channel_id, channel_title, published_at,
    duration_seconds, description, thumbnail_url, language, caption_language, caption_kind,
    metadata_source, has_caption_track, status, fetcher, transcript_state, last_error,
    nullif(perspective, '')
FROM legacy_v1_videos ORDER BY rowid;

INSERT INTO source_tags ({KO}, source_id, position, tag)
SELECT {_ko_copy("vtg")}, p.id, CAST(j.key AS INTEGER), j.value
FROM legacy_v1_videos p, json_each(p.tags) j ORDER BY p.rowid, j.key;

INSERT INTO source_topics ({KO}, source_id, position, topic)
SELECT {_ko_copy("vtp")}, p.id, CAST(j.key AS INTEGER), j.value
FROM legacy_v1_videos p, json_each(p.topics) j ORDER BY p.rowid, j.key;

INSERT INTO source_people ({KO}, source_id, position, name, role)
SELECT {_ko_copy("vpp")}, p.id, CAST(j.key AS INTEGER),
       json_extract(j.value, '$.name'), coalesce(json_extract(j.value, '$.role'), '')
FROM legacy_v1_videos p, json_each(p.people) j ORDER BY p.rowid, j.key;

INSERT INTO corpora ({KO}, name, person, subject, auto_ingest, llm_cleanup)
SELECT {KO}, name, person, subject, auto_ingest, llm_cleanup
FROM legacy_v1_corpora ORDER BY rowid;

INSERT INTO corpus_topics ({KO}, corpus_id, position, topic)
SELECT {_ko_copy("ctp")}, p.id, CAST(j.key AS INTEGER), j.value
FROM legacy_v1_corpora p, json_each(p.topic) j ORDER BY p.rowid, j.key;

INSERT INTO corpus_perspectives ({KO}, corpus_id, position, perspective)
SELECT {_ko_copy("cps")}, p.id, CAST(j.key AS INTEGER), j.value
FROM legacy_v1_corpora p, json_each(p.perspective) j ORDER BY p.rowid, j.key;

INSERT INTO corpus_members ({KO}, corpus_id, source_id, reason)
SELECT {KO}, corpus_id, video_id, reason FROM legacy_v1_corpus_members ORDER BY rowid;

INSERT INTO fetch_attempts ({KO}, source_id, fetcher, outcome, detail)
SELECT {KO}, video_id, fetcher, outcome, detail FROM legacy_v1_fetch_attempts ORDER BY rowid;

INSERT INTO artifacts ({KO}, source_id, corpus_id, node, path, sha256, state, inputs, provenance)
SELECT {KO},
       CASE WHEN owner IN (SELECT id FROM sources) THEN owner END,
       CASE WHEN owner IN (SELECT id FROM corpora) THEN owner END,
       node, path, sha256, state, inputs, provenance
FROM legacy_v1_artifacts ORDER BY rowid;

INSERT INTO review_verdicts ({KO}, source_id, key, kind, verdict, span, role, source,
    applied_at, subject_sha256, previous_subject_sha256)
SELECT {KO}, video_id, key, kind, verdict, span, role, source,
       CASE WHEN applied_at = '' THEN created_at ELSE applied_at END,
       corpuslm_subject_sha256(kind, video_id, span, role), NULL
FROM legacy_v1_review_verdicts ORDER BY rowid;

INSERT INTO speaker_verdicts ({KO}, source_id, role, name)
SELECT {KO}, video_id, role, name FROM legacy_v1_speaker_verdicts ORDER BY rowid;

INSERT INTO corrections ({KO}, span_norm, verdict, kind, origin_source, origin_key)
SELECT {KO}, span_norm, verdict, kind, origin_video, origin_key
FROM legacy_v1_corrections ORDER BY rowid;

INSERT INTO locks (owner, holder, acquired_at, expires_at)
SELECT owner, holder, acquired_at, expires_at FROM legacy_v1_locks;

INSERT INTO segments (rowid, source_id, position, start_seconds, end_seconds, speaker, text)
SELECT rowid, video_id, idx, t0, t1, speaker, text FROM legacy_v1_segments ORDER BY rowid;
"""


# v2 -> v3 added the playlist/channel list under its first names; v3 -> v4 renames it. A v2
# database is brought to v3 first so one script handles both.
V2_TO_V3_TABLE = """
CREATE TABLE IF NOT EXISTS video_sources (
    id TEXT PRIMARY KEY NOT NULL, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    created_by TEXT NOT NULL, schema_version TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata)),
    video_id TEXT NOT NULL REFERENCES videos(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    source_id TEXT NOT NULL,
    UNIQUE (video_id, position)
) STRICT;
"""

# v3 -> v4: `videos` becomes `sources` (a YouTube video or a Bee conversation) with a `kind`;
# `url` becomes `locator`; every `video_id` becomes `source_id`; the playlist/channel list
# becomes `source_feeds`; the corpus-level list is dropped (a corpus records no feed). Triggers
# and views are dropped and rebuilt from SCHEMA because their text names tables.
V3_TO_V4_RENAMES = [
    "ALTER TABLE video_sources RENAME COLUMN source_id TO feed_id;",
    "ALTER TABLE videos RENAME TO sources;",
    "ALTER TABLE sources RENAME COLUMN url TO locator;",
    "ALTER TABLE sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'youtube' "
    "CHECK (kind IN ('youtube', 'bee'));",
    "ALTER TABLE video_tags RENAME TO source_tags;",
    "ALTER TABLE video_topics RENAME TO source_topics;",
    "ALTER TABLE video_people RENAME TO source_people;",
    "ALTER TABLE video_sources RENAME TO source_feeds;",
    *(
        f"ALTER TABLE {t} RENAME COLUMN video_id TO source_id;"
        for t in (
            "source_tags", "source_topics", "source_people", "source_feeds", "corpus_members",
            "fetch_attempts", "artifacts", "review_verdicts", "speaker_verdicts", "segments",
        )
    ),
    "ALTER TABLE corrections RENAME COLUMN origin_video TO origin_source;",
    "ALTER TABLE runs RENAME COLUMN videos TO sources;",
    "DROP INDEX IF EXISTS corpus_members_video;",
]  # fmt: skip


def _v3_to_v4_script(conn: sqlite3.Connection) -> list[str]:
    drops = [
        f"DROP TRIGGER IF EXISTS {name};"
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    ]
    drops += [
        f"DROP VIEW IF EXISTS {name};"
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
    ]
    drops += ["DROP TABLE IF EXISTS segments_fts;"]
    tail = ["DROP TABLE IF EXISTS corpus_sources;"]
    rebuild = ["INSERT INTO segments_fts(segments_fts) VALUES ('rebuild');"]
    return [*drops, *V3_TO_V4_RENAMES, *tail, *_statements(SCHEMA), *rebuild]


def _v4_to_v5_script() -> list[str]:
    """Additive: the review answers table and its change-log trigger."""
    return [
        *_statements(REVIEW_ANSWERS_TABLE),
        _master_trigger("review_answers", MASTER_COLUMNS["review_answers"]),
    ]


def _v5_to_v6_script() -> list[str]:
    """Additive: the model suggestions table and its candidates table."""
    return _statements(MODEL_SUGGESTIONS_TABLES)


def _v6_to_v7_script() -> list[str]:
    """Additive: the tracked feeds table and the skipped sources table, each with its
    change-log trigger."""
    return [
        *_statements(FEEDS_TABLES),
        _master_trigger("feeds", MASTER_COLUMNS["feeds"]),
        _master_trigger("source_skips", MASTER_COLUMNS["source_skips"]),
    ]


def _v7_to_v8_script() -> list[str]:
    """Additive: the cleaned segments table, its full-text index and its triggers (both empty;
    `beelm corpus index` fills them from the transcripts already built)."""
    return [*_statements(CLEANED_SEGMENTS_TABLES), *_statements(CLEANED_SEGMENTS_TRIGGERS)]


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _counts(conn: sqlite3.Connection, tables) -> dict[str, int]:
    have = _tables(conn)
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables if t in have}


def backup(
    conn: sqlite3.Connection, db_path: Path, *, label: str, now: datetime | None = None
) -> Path:
    """A verified copy of the database under `<workspace>/backups/`, named
    with a UTC timestamp: written through SQLite's backup API to a temp
    file, checked with PRAGMA integrity_check and a row count of every
    author table, then renamed into place. Raises MigrationFailed when the
    copy does not verify (the partial file is removed)."""
    directory = Path(db_path).parent / BACKUPS_DIRNAME
    directory.mkdir(exist_ok=True)
    stamp = (now or clock.utc_now()).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{Path(db_path).stem}-{stamp}-{label}.sqlite"
    n = 1
    while target.exists():
        n += 1
        target = directory / f"{Path(db_path).stem}-{stamp}-{label}-{n}.sqlite"
    partial = target.with_name(target.name + ".partial")
    dest = sqlite3.connect(str(partial))
    try:
        conn.backup(dest)
        check = dest.execute("PRAGMA integrity_check").fetchone()[0]
        want = _counts(conn, AUTHOR_TABLES)
        got = _counts(dest, AUTHOR_TABLES)
    finally:
        dest.close()
    if check != "ok" or want != got:
        partial.unlink(missing_ok=True)
        raise MigrationFailed(
            f"backup of {db_path} did not verify (integrity_check: {check}; "
            f"author rows {want} vs copy {got}); nothing was migrated"
        )
    os.replace(partial, target)
    return target


def _backup_pattern(stem: str) -> re.Pattern[str]:
    """The names `backup` gives: `<stem>-<YYYYMMDDTHHMMSSZ>-<label>[-<n>].sqlite`."""
    return re.compile(rf"^{re.escape(stem)}-(\d{{8}}T\d{{6}}Z)-(.+?)(?:-(\d+))?\.sqlite$")


def prune_backups(db_path: Path, *, keep: int = BACKUPS_KEPT) -> list[str]:
    """Delete all but the `keep` newest backups of `db_path` that match the
    backup name pattern; return the names deleted, oldest first. Newest is
    by the UTC stamp in the name, then the `-n` suffix."""
    directory = Path(db_path).parent / BACKUPS_DIRNAME
    if not directory.is_dir():
        return []
    pattern = _backup_pattern(Path(db_path).stem)
    ours = []
    for p in directory.iterdir():
        m = pattern.match(p.name)
        if m and p.is_file():
            ours.append(((m.group(1), int(m.group(3) or 1)), p))
    ours.sort()
    doomed = [p for _, p in ours[: max(0, len(ours) - keep)]]
    for p in doomed:
        p.unlink()
    return [p.name for p in doomed]


def _subject_sha256(kind: str, source_id: str, span: str, role: str) -> str:
    from .identity import subject_sha256

    return subject_sha256(kind=kind, source_id=source_id, span=span, role=role)


def _run_in_transaction(
    conn: sqlite3.Connection, build, *, from_version: int, to_version: int
) -> bool:
    """Run the statements `build(conn)` returns in one explicit transaction,
    check foreign keys before COMMIT, and set user_version inside the same
    transaction. False when the database is no longer at `from_version`
    once the write lock is held (another process migrated it first)."""
    previous = conn.isolation_level
    conn.isolation_level = None  # explicit BEGIN/COMMIT below, nothing implicit
    try:
        # A rebuild renames tables; SQLite asks for foreign keys off meanwhile.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _user_version(conn) != from_version:
                conn.execute("ROLLBACK")
                return False
            for statement in build(conn):
                conn.execute(statement)
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise MigrationFailed(f"foreign key check failed: {[tuple(r) for r in broken][:5]}")
            conn.execute(f"PRAGMA user_version = {int(to_version)}")
            conn.execute("COMMIT")
            return True
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.isolation_level = previous


def _statements(script: str) -> list[str]:
    """Split a script into complete statements (trigger bodies included)."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise MigrationFailed(f"incomplete SQL statement in migration: {buf.strip()[:80]!r}")
    return out


def _v1_to_v2_script(conn: sqlite3.Connection) -> str:
    present = [t for t in V1_TABLES if t in _tables(conn)]
    drops = [
        "DROP TRIGGER IF EXISTS segments_ai;",
        "DROP TRIGGER IF EXISTS segments_ad;",
        "DROP TRIGGER IF EXISTS segments_au;",
        "DROP TABLE IF EXISTS segments_fts;",
    ]
    renames = [f"ALTER TABLE {t} RENAME TO legacy_v1_{t};" for t in present]
    # A v1 table that never existed has no rows to copy.
    absent = [f"legacy_v1_{t} " for t in V1_TABLES if t not in present]
    copies = [s for s in _statements(V1_TO_V2_COPY) if not any(a in s + " " for a in absent)]
    return "\n".join(
        [
            *drops,
            *renames,
            TABLES,
            *copies,
            *(f"DROP TABLE legacy_v1_{t};" for t in present),
            "INSERT INTO segments_fts(segments_fts) VALUES ('rebuild');",
            TRIGGERS,
            VIEWS,
        ]
    )


def _complete_v1(conn: sqlite3.Connection) -> None:
    """Bring a v1 database to the last v1 shape (the columns added after
    v1 first shipped), so the v2 copy can read every column."""
    for table, columns in V1_ADDED_COLUMNS.items():
        if table not in _tables(conn):
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def migrate(conn: sqlite3.Connection, db_path: Path) -> dict:
    """Bring the database at `db_path` to USER_VERSION. A new, empty file
    gets the schema; a v1 database is backed up (verified) and rebuilt in
    one transaction, then checked with PRAGMA integrity_check. A newer
    database is declined. Returns what was done."""
    version = _user_version(conn)
    if version > USER_VERSION:
        raise NewerDatabase(
            f"{db_path} has schema version {version}; this corpuslm reads up to "
            f"{USER_VERSION}. Upgrade corpuslm; the database was not touched."
        )
    if version == USER_VERSION:
        return {"from": version, "to": version, "backup": None}
    if not ({"videos", "sources"} & _tables(conn)):
        _run_in_transaction(
            conn, lambda c: _statements(SCHEMA), from_version=version, to_version=USER_VERSION
        )
        return {"from": version, "to": USER_VERSION, "backup": None, "created": True}
    saved = backup(conn, db_path, label=f"before-v{USER_VERSION}")
    deleted = prune_backups(db_path)  # only after the new one verified
    if version in (4, 5, 6, 7):
        before = _counts(conn, AUTHOR_TABLES)

        def additive(c: sqlite3.Connection) -> list[str]:
            return [
                *(_v4_to_v5_script() if version == 4 else []),
                *(_v5_to_v6_script() if version <= 5 else []),
                *(_v6_to_v7_script() if version <= 6 else []),
                *_v7_to_v8_script(),
            ]

        if not _run_in_transaction(conn, additive, from_version=version, to_version=USER_VERSION):
            return {
                "from": version,
                "to": USER_VERSION,
                "backup": str(saved),
                "backups_deleted": deleted,
                "by": "another process",
            }
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        after = _counts(conn, AUTHOR_TABLES)
        if check != "ok" or any(before[t] != after[t] for t in before):
            raise MigrationFailed(
                f"{db_path} after migration: integrity_check {check}; author rows {before} -> "
                f"{after}. The verified backup is {saved}."
            )
        return {
            "from": version,
            "to": USER_VERSION,
            "backup": str(saved),
            "backups_deleted": deleted,
        }
    if version in (2, 3):
        dropped = (
            [r[0] for r in conn.execute("SELECT source_id FROM corpus_sources ORDER BY rowid")]
            if "corpus_sources" in _tables(conn)
            else []
        )
        before = _counts(conn, AUTHOR_TABLES)

        def to_v4(c: sqlite3.Connection) -> list[str]:
            return [*(_statements(V2_TO_V3_TABLE) if version == 2 else []), *_v3_to_v4_script(c)]

        if not _run_in_transaction(conn, to_v4, from_version=version, to_version=USER_VERSION):
            return {
                "from": version,
                "to": USER_VERSION,
                "backup": str(saved),
                "by": "another process",
            }
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        after = _counts(conn, AUTHOR_TABLES)
        if check != "ok" or any(before[t] != after[t] for t in before):
            raise MigrationFailed(
                f"{db_path} after migration: integrity_check {check}; author rows {before} -> "
                f"{after}. The verified backup is {saved}."
            )
        return {
            "from": version,
            "to": USER_VERSION,
            "backup": str(saved),
            "backups_deleted": deleted,
            "dropped_corpus_feeds": dropped,
        }
    conn.create_function("corpuslm_subject_sha256", 4, _subject_sha256, deterministic=True)
    before = _counts(conn, AUTHOR_TABLES)

    def v1_to_v2(c: sqlite3.Connection) -> list[str]:
        _complete_v1(c)
        return _statements(_v1_to_v2_script(c))

    if not _run_in_transaction(conn, v1_to_v2, from_version=version, to_version=USER_VERSION):
        return {
            "from": version,
            "to": USER_VERSION,
            "backup": str(saved),
            "backups_deleted": deleted,
            "by": "another process",
        }
    check = conn.execute("PRAGMA integrity_check").fetchone()[0]
    after = _counts(conn, AUTHOR_TABLES)
    if check != "ok" or any(before[t] != after[t] for t in before):
        raise MigrationFailed(
            f"{db_path} after migration: integrity_check {check}; author rows {before} → "
            f"{after}. The verified backup is {saved}."
        )
    return {"from": version, "to": USER_VERSION, "backup": str(saved), "backups_deleted": deleted}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.migration = migrate(self.conn, Path(path))
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._transaction_depth = 0

    def insert(self, table: str, row: dict[str, Any]) -> str:
        cols = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        self.conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", list(row.values())
        )
        self._maybe_commit()
        return row.get("id", "")

    def update(self, table: str, obj_id: str, changes: dict[str, Any]) -> None:
        """Evolve a mutable knowledge object, bumping its version."""
        sets = ", ".join(f"{c} = ?" for c in changes)
        self.conn.execute(
            f"UPDATE {table} SET {sets}, version = version + 1 WHERE id = ?",
            [*changes.values(), obj_id],
        )
        self._maybe_commit()

    def execute(self, sql: str, args: tuple | list = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, args)
        self._maybe_commit()
        return cur

    def one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def _maybe_commit(self) -> None:
        if not self._transaction_depth:
            self.conn.commit()

    @contextmanager
    def transaction(self):
        outermost = self._transaction_depth == 0
        if outermost:
            self.conn.execute("BEGIN IMMEDIATE")
        self._transaction_depth += 1
        try:
            yield
        except BaseException:
            self._transaction_depth -= 1
            if outermost:
                self.conn.rollback()
            raise
        else:
            self._transaction_depth -= 1
            if outermost:
                self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def inspect(db_path: Path) -> dict:
    """Read-only facts about an existing database file, for `corpuslm doctor`:
    its user_version, journal mode, PRAGMA integrity_check, and a row count
    of every table. Opens it read-only and never creates, migrates or
    writes it; the caller checks that the file exists first (a `mode=ro`
    URI of a missing file is not an error everywhere)."""
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'segments_fts_%' "
                "AND name NOT LIKE 'cleaned_segments_fts_%' ORDER BY name"
            )
        ]
        return {
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "tables": {t: _count(conn, t) for t in tables},
        }
    finally:
        conn.close()
