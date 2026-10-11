"""Tracked feeds, skipped videos and membership removal (schema 7, nagarkar/ytlm#21): a feed is
a playlist or channel the owner follows as an inbox, never a corpus; a skip is recorded per video
with a reason and reversed by date, never deleted; removing a membership leaves its trail in the
change log (docs/design/storage-schema.md, `feeds`, `source_skips`, `corpus_members`)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from corpuslm import db
from corpuslm.store import Store

FEED = "PLfixtures000000000000000000000"
VIDEO = "vidAAAAAAA1"
OTHER = "vidBBBBBBB2"


@pytest.fixture
def database(tmp_path):
    d = db.Database(tmp_path / "transcript.sqlite")
    yield d
    d.close()


@pytest.fixture
def store(database):
    return Store(database)


def test_a_feed_is_tracked_once_and_a_second_add_only_refreshes_the_title(store):
    assert store.add_feed(FEED, "playlist", "Inbox", created_by="cli") is True
    assert store.add_feed(FEED, "playlist", "", created_by="cli") is False
    assert store.add_feed(FEED, "playlist", "Inbox renamed", created_by="cli") is False
    feed = store.feed(FEED)
    assert feed is not None
    assert feed["kind"] == "playlist" and feed["title"] == "Inbox renamed"
    assert feed["last_scanned_at"] is None and feed["created_at"]
    assert [row["feed_id"] for row in store.feeds()] == [FEED]
    assert [c["column_name"] for c in store.changes_of("feeds", feed["id"])] == ["title"]


def test_a_feed_is_a_playlist_or_a_channel(store, database):
    with pytest.raises(sqlite3.IntegrityError):
        store.add_feed(FEED, "corpus", "x", created_by="cli")


def test_a_scan_stamps_the_feed_and_the_change_log_keeps_the_time(store):
    store.add_feed(FEED, "playlist", "Inbox", created_by="cli")
    store.mark_feed_scanned(FEED, when="2026-10-10T12:00:00Z")
    feed = store.feed(FEED)
    assert feed is not None and feed["last_scanned_at"] == "2026-10-10T12:00:00Z"
    changes = store.changes_of("feeds", feed["id"])
    assert changes[-1]["column_name"] == "last_scanned_at"
    assert changes[-1]["old_value"] is None
    with pytest.raises(KeyError):
        store.mark_feed_scanned("PLnone")


def test_sources_found_through_a_feed_come_from_their_own_feeds(store):
    store.add_source(VIDEO, "u", created_by="cli")
    store.add_source(OTHER, "u", created_by="cli")
    store.update_source(VIDEO, {"feeds": [FEED]})
    store.update_source(OTHER, {"feeds": ["PLelse"]})
    assert store.sources_found_through(FEED) == [VIDEO]


def test_a_skip_names_a_reason_and_a_video_not_in_the_catalog_can_be_skipped(store):
    assert store.skip(VIDEO) is None
    assert store.skip_source(VIDEO, "off topic", title="A talk", created_by="cli") is True
    assert store.skip_source(VIDEO, "again", created_by="cli") is False
    row = store.skip(VIDEO)
    assert row is not None and row["reason"] == "off topic" and row["title"] == "A talk"
    assert row["feed_id"] is None and row["skipped_at"] and row["lifted_at"] is None
    assert not store.has_source(VIDEO)
    with pytest.raises(sqlite3.IntegrityError):
        store.skip_source(OTHER, "", created_by="cli")


def test_a_skip_made_through_an_untracked_feed_is_stopped_where_it_is_made(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.skip_source(VIDEO, "x", feed_id="PLnotTracked", created_by="cli")


def test_unskip_lifts_the_skip_without_deleting_the_row_and_a_new_skip_reuses_it(store, database):
    store.add_feed(FEED, "playlist", "Inbox", created_by="cli")
    store.skip_source(VIDEO, "off topic", feed_id=FEED, created_by="cli")
    assert [r["source_id"] for r in store.skips(feed_id=FEED)] == [VIDEO]
    assert store.lift_skip(VIDEO) is True
    assert store.lift_skip(VIDEO) is False
    assert store.skip(VIDEO) is None and store.skips() == []
    assert database.one("SELECT COUNT(*) FROM source_skips")[0] == 1
    assert store.skip_source(VIDEO, "still off topic", created_by="cli") is True
    row = store.skip(VIDEO)
    assert row is not None and row["reason"] == "still off topic" and row["feed_id"] is None
    columns = [c["column_name"] for c in store.changes_of("source_skips", row["id"])]
    assert "lifted_at" in columns and columns.count("reason") == 1


def test_remove_member_deletes_one_membership_and_logs_who_removed_it_and_what_it_was(store):
    corpus = store.add_corpus({"name": "Physics"}, created_by="cli")
    other = store.add_corpus({"name": "Maths"}, created_by="cli")
    for video_id in (VIDEO, OTHER):
        store.add_source(video_id, "u", created_by="cli")
        store.add_member(corpus, video_id, "author", created_by="cli")
    store.add_member(other, VIDEO, "author", created_by="cli")
    member = store.member_rows(corpus)[0]
    assert member["source_id"] == VIDEO

    assert store.remove_member(corpus, VIDEO, created_by="repair") is True
    assert store.remove_member(corpus, VIDEO, created_by="repair") is False

    assert store.member_source_ids(corpus) == [OTHER]
    assert store.member_source_ids(other) == [VIDEO]  # the other corpus keeps it
    assert store.has_source(VIDEO)  # the video itself stays
    (trail,) = store.changes_of("corpus_members", member["id"])
    assert trail["column_name"] == "membership" and trail["new_value"] is None
    old = json.loads(trail["old_value"])
    assert old["corpus_id"] == corpus and old["source_id"] == VIDEO and old["reason"] == "author"
    assert old["added_by"] == "cli" and old["added_at"] == member["created_at"]
    who = store.db.one("SELECT created_by FROM change_log WHERE row_id = ?", (member["id"],))
    assert who is not None and who["created_by"] == "repair"


def test_a_version_6_database_gains_the_feed_tables_after_a_verified_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    db.Database(path).close()
    conn = sqlite3.connect(path)
    for statement in (
        "DROP TRIGGER feeds_change_log",
        "DROP TRIGGER source_skips_change_log",
        "DROP TABLE source_skips",
        "DROP TABLE feeds",
        "PRAGMA user_version = 6",
    ):
        conn.execute(statement)
    conn.execute(
        "INSERT INTO corpora (id, version, created_at, created_by, schema_version, name) "
        "VALUES ('cor-1', 1, 't', 'test', '6', 'Kept')"
    )
    conn.commit()
    conn.close()
    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 6 and opened.migration["to"] == db.USER_VERSION == 8
        assert opened.migration["backup"].endswith("-before-v8.sqlite")
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        assert opened.one("SELECT COUNT(*) FROM feeds")[0] == 0
        assert opened.one("SELECT COUNT(*) FROM source_skips")[0] == 0
        assert opened.one("SELECT COUNT(*) FROM corpora")[0] == 1
        store = Store(opened)
        store.add_feed(FEED, "playlist", "Inbox", created_by="cli")  # the trigger came with it
        store.mark_feed_scanned(FEED, when="2026-10-10T12:00:00Z")
        feed = store.feed(FEED)
        assert feed is not None and store.changes_of("feeds", feed["id"])
    finally:
        opened.close()
    copy = sqlite3.connect(opened.migration["backup"])
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 6
    copy.close()
