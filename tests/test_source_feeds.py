"""A source records the playlists and channels it was found through (nagarkar/ytlm#61), named
`feeds` since schema version 4; each is one row dated by the row's own `created_at`. Version 4
also renames `videos` to `sources` with a `kind` (youtube or bee) and `url` to `locator`, and
upgrades a version 2 or 3 database in place after a verified backup (docs/design/storage-schema.md,
"Migrations")."""

from __future__ import annotations

import sqlite3

import pytest

from corpuslm import db
from corpuslm.store import Store

SOURCE = "vidAAAAAAA1"


@pytest.fixture
def database(tmp_path):
    d = db.Database(tmp_path / "transcript.sqlite")
    yield d
    d.close()


@pytest.fixture
def store(database):
    s = Store(database)
    s.add_source(SOURCE, "https://www.youtube.com/watch?v=" + SOURCE, created_by="cli")
    return s


def test_a_fresh_source_has_no_feeds_and_is_a_youtube_source_by_default(store):
    row = store.source(SOURCE)
    assert row["feeds"] == [] and row["kind"] == "youtube"
    assert row["locator"].endswith(SOURCE)


def test_a_bee_source_keeps_its_kind_and_workspace_relative_locator(store):
    store.add_source(
        "bee:me:2026-10-03T09:14:00-07:00",
        "transcripts/conversations/2026-10-03/abc.md",
        created_by="cli",
        kind="bee",
    )
    row = store.source("bee:me:2026-10-03T09:14:00-07:00")
    assert row["kind"] == "bee" and row["locator"].startswith("transcripts/")


def test_feeds_round_trip_in_order_and_each_row_carries_its_date(store, database):
    store.update_source(SOURCE, {"feeds": ["PL1", "UC2"]})
    assert store.source(SOURCE)["feeds"] == ["PL1", "UC2"]
    rows = database.all("SELECT feed_id, created_at FROM source_feeds ORDER BY position")
    assert [r["feed_id"] for r in rows] == ["PL1", "UC2"]
    assert all(r["created_at"] for r in rows)


def test_adding_a_feed_leaves_the_earlier_one_and_its_date_alone(store, database):
    store.update_source(SOURCE, {"feeds": ["PL1"]})
    first = database.one("SELECT id, created_at FROM source_feeds")
    store.update_source(SOURCE, {"feeds": ["PL1", "PL2"]})
    row = database.one("SELECT id, created_at FROM source_feeds WHERE feed_id = 'PL1'")
    assert (row["id"], row["created_at"]) == (first["id"], first["created_at"])


def test_feed_changes_are_in_the_change_log(store, database):
    store.update_source(SOURCE, {"feeds": ["PL1"]})
    rows = database.all(
        "SELECT old_value, new_value FROM change_log WHERE table_name = 'source_feeds'"
    )
    assert [(r["old_value"], r["new_value"]) for r in rows] == [(None, "PL1")]


def _downgrade_to_version_3(path):
    """Put a fresh database back into its version 3 shape: old names, the corpus feed list."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        conn.execute(f"DROP TRIGGER {name}")
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall():
        conn.execute(f"DROP VIEW {name}")
    conn.execute("DROP TABLE segments_fts")
    conn.execute("ALTER TABLE source_feeds RENAME COLUMN source_id TO video_id")
    conn.execute("ALTER TABLE source_feeds RENAME COLUMN feed_id TO source_id")
    conn.execute("ALTER TABLE source_feeds RENAME TO video_sources")
    for old, new in (("tags", "tags"), ("topics", "topics"), ("people", "people")):
        conn.execute(f"ALTER TABLE source_{old} RENAME TO video_{new}")
    for t in (
        "video_tags", "video_topics", "video_people", "corpus_members",
        "fetch_attempts", "artifacts", "review_verdicts", "speaker_verdicts", "segments",
    ):  # fmt: skip
        conn.execute(f"ALTER TABLE {t} RENAME COLUMN source_id TO video_id")
    conn.execute("ALTER TABLE corrections RENAME COLUMN origin_source TO origin_video")
    conn.execute("ALTER TABLE runs RENAME COLUMN sources TO videos")
    conn.execute("ALTER TABLE sources DROP COLUMN kind")
    conn.execute("ALTER TABLE sources RENAME COLUMN locator TO url")
    conn.execute("ALTER TABLE sources RENAME TO videos")
    conn.execute(
        "CREATE TABLE corpus_sources (id TEXT PRIMARY KEY, version INTEGER, created_at TEXT, "
        "created_by TEXT, schema_version TEXT, metadata TEXT, corpus_id TEXT, position INTEGER, "
        "source_id TEXT)"
    )
    conn.execute(
        "INSERT INTO corpus_sources VALUES ('csr-1', 1, 't', 'cli', '3', '{}', 'cor-1', 0, 'PLold')"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()


def test_a_version_3_database_is_renamed_in_place_after_a_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    d = db.Database(path)
    s = Store(d)
    s.add_source(SOURCE, "https://www.youtube.com/watch?v=" + SOURCE, created_by="cli")
    s.replace_segments(SOURCE, [{"idx": 0, "t0": 0.0, "t1": 1.0, "speaker": "A", "text": "hello"}])
    d.close()
    _downgrade_to_version_3(path)

    d = db.Database(path)
    try:
        assert d.migration["from"] == 3 and d.migration["to"] == db.USER_VERSION == 5
        assert d.migration["backup"] and (tmp_path / "backups").exists()
        assert d.migration["dropped_corpus_feeds"] == ["PLold"]
        s = Store(d)
        row = s.source(SOURCE)
        assert row["kind"] == "youtube" and row["locator"].endswith(SOURCE)
        s.update_source(SOURCE, {"feeds": ["PL1"]})
        assert s.source(SOURCE)["feeds"] == ["PL1"]
        assert s.segment_count(SOURCE) == 1
        assert d.one("SELECT COUNT(*) FROM segments_fts WHERE segments_fts MATCH 'hello'")[0] == 1
        assert d.one("PRAGMA integrity_check")[0] == "ok"
        assert d.all("PRAGMA foreign_key_check") == []
        names = {r[0] for r in d.all("SELECT name FROM sqlite_master")}
        assert "corpus_sources" not in names and "videos" not in names
    finally:
        d.close()


def test_every_reader_can_ask_for_one_kind_of_source(store):
    """A consumer that only understands one kind (ytlm: youtube) asks for it by name."""
    bee = "bee:me:2026-10-03T09:14:00.000-07:00"
    store.add_source(bee, "beelm/conversations/d/1.json", created_by="cli", kind="bee")
    for source_id in (SOURCE, bee):
        store.replace_segments(
            source_id, [{"idx": 0, "t0": 0.0, "t1": 1.0, "speaker": "A", "text": "shared words"}]
        )
    assert store.source_ids("youtube") == [SOURCE] and store.source_ids("bee") == [bee]
    assert len(store.source_ids()) == 2
    assert store.source_counts_by("status", "bee") == {"added": 1}
    assert store.segment_count(kind="youtube") == 1 and store.segment_count() == 2
    hits = store.search("shared", corpus_id=None, limit=10, kind="youtube")
    assert [h["source_id"] for h in hits] == [SOURCE]
    assert len(store.search("shared", corpus_id=None, limit=10)) == 2


def test_corpus_count_can_ask_for_the_corpora_one_kind_reads(store):
    """A corpus made only of Bee sources is not a corpus ytlm reads (corpus_count(kind))."""
    bee = "bee:1"
    store.add_source(bee, "beelm/conversations/d/1.json", created_by="cli", kind="bee")
    only_bee = store.add_corpus({"name": "Bee conversations"}, created_by="cli")
    mixed = store.add_corpus({"name": "Mixed"}, created_by="cli")
    store.add_member(only_bee, bee, "discovered", created_by="cli")
    store.add_member(mixed, SOURCE, "author", created_by="cli")
    assert store.corpus_count() == 2 and store.corpus_count("youtube") == 1
    assert store.corpus_count("bee") == 1
