"""A video records the playlists and channels it was found through
(nagarkar/ytlm#61): `sources` is a list of a video, one row per source,
dated by the row's own `created_at`. Schema version 3 adds the table to a
version-2 database in place, after a verified backup."""

from __future__ import annotations

import sqlite3

import pytest

from corpuslm import db
from corpuslm.store import Store

VID = "vidAAAAAAA1"


@pytest.fixture
def database(tmp_path):
    d = db.Database(tmp_path / "transcript.sqlite")
    yield d
    d.close()


@pytest.fixture
def store(database):
    s = Store(database)
    s.add_video(VID, "https://www.youtube.com/watch?v=" + VID, created_by="cli")
    return s


def test_a_fresh_video_has_no_sources(store):
    assert store.video(VID)["sources"] == []


def test_sources_round_trip_in_order_and_each_row_carries_its_date(store, database):
    store.update_video(VID, {"sources": ["PL1", "UC2"]})
    assert store.video(VID)["sources"] == ["PL1", "UC2"]
    rows = database.all("SELECT source_id, created_at FROM video_sources ORDER BY position")
    assert [r["source_id"] for r in rows] == ["PL1", "UC2"]
    assert all(r["created_at"] for r in rows)


def test_adding_a_source_leaves_the_earlier_one_and_its_date_alone(store, database):
    store.update_video(VID, {"sources": ["PL1"]})
    first = database.one("SELECT id, created_at FROM video_sources").__getitem__
    store.update_video(VID, {"sources": ["PL1", "PL2"]})
    row = database.one("SELECT id, created_at FROM video_sources WHERE source_id = 'PL1'")
    assert (row["id"], row["created_at"]) == (first("id"), first("created_at"))


def test_source_changes_are_in_the_change_log(store, database):
    store.update_video(VID, {"sources": ["PL1"]})
    rows = database.all(
        "SELECT old_value, new_value FROM change_log WHERE table_name = 'video_sources'"
    )
    assert [(r["old_value"], r["new_value"]) for r in rows] == [(None, "PL1")]


def test_a_version_2_database_gains_the_table_after_a_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    d = db.Database(path)
    d.close()
    conn = sqlite3.connect(path)
    for suffix in ("log_add", "log_remove"):
        conn.execute(f"DROP TRIGGER video_sources_{suffix}")
    conn.execute("DROP TABLE video_sources")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    d = db.Database(path)
    try:
        assert d.migration["from"] == 2 and d.migration["to"] == db.USER_VERSION == 3
        assert d.migration["backup"] and (tmp_path / "backups").exists()
        s = Store(d)
        s.add_video(VID, "u", created_by="cli")
        s.update_video(VID, {"sources": ["PL1"]})
        assert s.video(VID)["sources"] == ["PL1"]
        assert d.one("PRAGMA integrity_check")[0] == "ok"
    finally:
        d.close()
