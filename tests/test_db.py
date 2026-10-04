"""Blind tests for the workspace database (modules corpuslm.db and corpuslm.store).

Written from the requirement and the public interface only (metalm
testing.md, "Anti-gaming" (c)); no implementation file was read.

Sources:
- docs/design/storage-schema.md: "Integrity lives in the database",
  "Lists are rows, not JSON", "Explicit names; JSON artifacts keep theirs",
  "Each fact once: membership status is derived", "One history pattern per
  kind of table", "A verdict carries the content hash of what it judged",
  "Migrations: user_version, a verified backup, one transaction", "Tables".
- metalm architecture.md, "History and identity" and "Relational design".
- metalm coding.md, "Failure" and "Storage and migrations".
- metalm operations.md, "Scheduled and unattended work" (verified backup).

Expected values are derived by hand from those documents.

The last section (the schema document, the change-log coverage, the
Repository boundary, a concurrent upgrade) was written with the code.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from corpuslm import db
from corpuslm.store import Store

REPO = Path(__file__).parent.parent
SCHEMA_V1 = REPO / "tests" / "fixtures" / "schema_v1.sql"

VID = "vidAAAAAAA1"
VID2 = "vidBBBBBBB2"
HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture
def dbpath(tmp_path):
    return tmp_path / "transcript.sqlite"


@pytest.fixture
def database(dbpath):
    d = db.Database(dbpath)
    yield d
    d.close()


@pytest.fixture
def store(database):
    return Store(database)


def _video(store: Store, vid: str = VID, **changes) -> str:
    assert store.add_video(vid, f"https://www.youtube.com/watch?v={vid}", created_by="cli")
    if changes:
        store.update_video(vid, changes)
    return vid


def _corpus(store: Store, name: str = "Dharma", **extra) -> str:
    fields = {
        "name": name,
        "person": "A Teacher",
        "subject": "Buddhism",
        "topic": [],
        "perspective": [],
        "sources": [],
        "auto_ingest": 0,
        "llm_cleanup": 1,
    }
    fields.update(extra)
    return store.add_corpus(fields, created_by="cli")


def _verdict(store: Store, vid=VID, key="amb-0001", verdict="Dharma", sha=HASH_A, kind="ambiguity"):
    return store.append_review_verdict(
        video_id=vid,
        key=key,
        kind=kind,
        verdict=verdict,
        span="darma",
        role="",
        source="review file",
        subject_sha256=sha,
        created_by="cli",
    )


# 1. Fresh file gets the schema ---------------------------------------------


def test_fresh_file_gets_user_version(database, dbpath):
    # storage-schema.md: "The schema version is PRAGMA user_version (db.USER_VERSION, now 2)"
    assert db.USER_VERSION == 2
    assert database.one("PRAGMA user_version")[0] == db.USER_VERSION
    raw = sqlite3.connect(dbpath)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == db.USER_VERSION
    finally:
        raw.close()


def test_every_table_is_strict(database):
    # storage-schema.md, "Integrity lives in the database": "Every table is STRICT."
    rows = database.all(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    checked = 0
    for r in rows:
        name, sql = r[0], r[1] or ""
        if sql.upper().startswith("CREATE VIRTUAL TABLE") or name.startswith("segments_fts"):
            continue  # FTS5 index and its shadow tables are owned by SQLite
        assert "STRICT" in sql.upper(), f"{name} is not STRICT"
        checked += 1
    # every table named in the Tables section (excluding segments_fts) exists
    names = {r[0] for r in rows}
    for t in (
        "videos",
        "video_tags",
        "video_topics",
        "video_people",
        "corpora",
        "corpus_topics",
        "corpus_perspectives",
        "corpus_sources",
        "corpus_members",
        "fetch_attempts",
        "artifacts",
        "review_verdicts",
        "speaker_verdicts",
        "corrections",
        "runs",
        "change_log",
        "locks",
        "segments",
    ):
        assert t in names, t
    assert checked >= 18


def test_foreign_keys_on(database):
    # storage-schema.md: "PRAGMA foreign_keys = ON on every connection"
    assert database.one("PRAGMA foreign_keys")[0] == 1


# 2. CHECK constraints --------------------------------------------------------


def test_video_status_outside_enum_is_rejected(store, database):
    # storage-schema.md videos.status: "one of added, ingested, no_captions, unavailable, failed"
    _video(store)
    with pytest.raises(sqlite3.IntegrityError):
        database.update("videos", VID, {"status": "bogus"})
    assert store.video(VID)["status"] == "added"


def test_member_reason_outside_enum_is_rejected(store):
    # corpus_members.reason: "one of author, discovered, consumer"
    _video(store)
    cid = _corpus(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.add_member(cid, VID, "bogus", created_by="cli")
    assert not store.is_member(cid, VID)


def test_corrupt_json_metadata_is_rejected(store, database):
    # "JSON columns carry CHECK (json_valid(...))"
    _video(store)
    with pytest.raises(sqlite3.IntegrityError):
        database.update("videos", VID, {"metadata": "{not json"})


# 3. FOREIGN KEYs -------------------------------------------------------------


def test_fetch_attempt_for_unknown_video_is_rejected(store):
    # "every reference has a FOREIGN KEY"
    with pytest.raises(sqlite3.IntegrityError):
        store.append_fetch_attempt("noSuchVideo", "supadata", "ok", "", created_by="runner")


def test_review_verdict_for_unknown_video_is_rejected(store):
    with pytest.raises(sqlite3.IntegrityError):
        _verdict(store, vid="noSuchVideo")


# 4. db.loads -----------------------------------------------------------------


def test_loads_null_is_default_and_valid_parses():
    # db.loads: "NULL (None) is an honest absence and reads as default"
    assert db.loads(None, []) == []
    assert db.loads(None, {"x": 1}) == {"x": 1}
    assert db.loads('["a", "b"]', []) == ["a", "b"]


def test_loads_corrupt_raises_not_default():
    # coding.md "Failure": no default on bad data; storage-schema.md: "raises CorruptValue"
    with pytest.raises(db.CorruptValue):
        db.loads("{not json", {})
    with pytest.raises(db.CorruptValue):
        db.loads("", [])
    assert issubclass(db.CorruptValue, ValueError)


# 5. Decisions append-only ----------------------------------------------------


def test_two_verdicts_for_one_slot_keep_both_rows_newest_in_force(store):
    # "an answer given again or changed is a new row; the newest row per slot is in force"
    _video(store)
    first = _verdict(store, verdict="Dharma")
    second = _verdict(store, verdict="Dhamma")
    assert first != second
    history = store.review_verdict_history(VID, "amb-0001")
    assert len(history) == 2
    assert [h["verdict"] for h in history] == ["Dharma", "Dhamma"]  # oldest first
    current = store.current_review_verdicts(VID)
    assert set(current) == {"amb-0001"}
    assert current["amb-0001"]["verdict"] == "Dhamma"
    assert current["amb-0001"]["id"] == second


def test_newest_in_force_with_many_rewrites(store):
    # same rule, several rewrites in quick succession (same timestamp second)
    _video(store)
    for v in ["one", "two", "three", "four", "five"]:
        _verdict(store, verdict=v)
    assert store.current_review_verdicts(VID)["amb-0001"]["verdict"] == "five"
    assert len(store.review_verdict_history(VID, "amb-0001")) == 5


def test_previous_subject_sha256_lineage(store):
    # "When a new verdict for the same key judged different content,
    #  previous_subject_sha256 holds the old hash (lineage: new hash -> old hash)"
    _video(store)
    _verdict(store, verdict="first", sha=HASH_A)
    _verdict(store, verdict="second", sha=HASH_A)  # same content, changed answer
    _verdict(store, verdict="third", sha=HASH_B)  # different content
    h = store.review_verdict_history(VID, "amb-0001")
    assert [r["subject_sha256"] for r in h] == [HASH_A, HASH_A, HASH_B]
    assert h[0]["previous_subject_sha256"] is None  # first in slot
    assert h[1]["previous_subject_sha256"] is None  # same hash as the slot's current
    assert h[2]["previous_subject_sha256"] == HASH_A
    assert store.current_review_verdicts(VID)["amb-0001"]["previous_subject_sha256"] == HASH_A


def test_lineage_is_per_slot(store):
    _video(store)
    _verdict(store, key="amb-0001", sha=HASH_A)
    _verdict(store, key="amb-0002", sha=HASH_B)
    h = store.review_verdict_history(VID, "amb-0002")
    assert h[0]["previous_subject_sha256"] is None


def _fill_append_only(store):
    _video(store)
    _verdict(store)
    store.append_speaker_verdict(video_id=VID, role="host", name="Ajahn", created_by="cli")
    store.append_correction(
        span_norm="darma",
        verdict="Dharma",
        kind="term",
        origin_video=VID,
        origin_key="amb-0001",
        created_by="cli",
    )
    store.append_fetch_attempt(VID, "supadata", "ok", "", created_by="runner")
    store.update_video(VID, {"title": "A talk"})  # writes change_log


@pytest.mark.parametrize(
    "table", ["review_verdicts", "speaker_verdicts", "corrections", "fetch_attempts", "change_log"]
)
def test_append_only_tables_abort_update_and_delete(store, database, table):
    # "append-only (a trigger aborts UPDATE and DELETE)"; evidence too (fetch_attempts, change_log)
    _fill_append_only(store)
    before = database.one(f"SELECT COUNT(*) FROM {table}")[0]
    assert before >= 1
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(f"UPDATE {table} SET created_by = 'tamper'")
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(f"DELETE FROM {table}")
    assert database.one(f"SELECT COUNT(*) FROM {table}")[0] == before
    assert database.one(f"SELECT COUNT(*) FROM {table} WHERE created_by = 'tamper'")[0] == 0


def test_append_only_constant_matches_spec():
    assert set(db.APPEND_ONLY) == {
        "review_verdicts",
        "speaker_verdicts",
        "corrections",
        "fetch_attempts",
        "change_log",
    }


# 6. Current speaker names and corrections ------------------------------------


def test_current_speaker_names_newest_per_role(store):
    # vw_current_speaker_verdicts: "the newest row per (video_id, role)"
    _video(store)
    _video(store, VID2)
    store.append_speaker_verdict(video_id=VID, role="host", name="Old", created_by="cli")
    store.append_speaker_verdict(video_id=VID, role="guest", name="Guest", created_by="cli")
    store.append_speaker_verdict(video_id=VID, role="host", name="New", created_by="cli")
    store.append_speaker_verdict(video_id=VID2, role="host", name="Other", created_by="cli")
    assert store.current_speaker_names(VID) == {"host": "New", "guest": "Guest"}
    assert store.current_speaker_names(VID2) == {"host": "Other"}


def test_current_corrections_newest_per_span(store):
    # vw_current_corrections: "the newest row per span_norm"; correction_count:
    # "Rules in force (distinct normalized spans)"
    _video(store)
    store.append_correction(
        span_norm="darma",
        verdict="Dharma",
        kind="term",
        origin_video=VID,
        origin_key="amb-0001",
        created_by="cli",
    )
    store.append_correction(
        span_norm="sutta",
        verdict="Sutta",
        kind="term",
        origin_video=VID,
        origin_key="amb-0002",
        created_by="cli",
    )
    store.append_correction(
        span_norm="darma",
        verdict="Dhamma",
        kind="reading",
        origin_video=VID,
        origin_key="amb-0003",
        created_by="cli",
    )
    cur = store.current_corrections()
    assert set(cur) == {"darma", "sutta"}
    assert cur["darma"]["verdict"] == "Dhamma"
    assert cur["darma"]["kind"] == "reading"
    assert store.correction_count() == 2
    # kind filters the rules IN FORCE: the superseded 'term' row for darma is not in force
    assert set(store.current_corrections(kind="term")) == {"sutta"}
    assert set(store.current_corrections(kind="reading")) == {"darma"}


def test_correction_kind_outside_enum_is_rejected(store):
    # corrections.kind: "one of term, reading"
    _video(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.append_correction(
            span_norm="x",
            verdict="X",
            kind="bogus",
            origin_video=VID,
            origin_key="amb-0001",
            created_by="cli",
        )


# 7. Master data change log ---------------------------------------------------


def _list_changes(store, vid):
    # The spec leaves open whether a list change is filed under the master table or
    # the list table ("table_name: the master or list table"); accept either.
    def tagrows(table):
        return [
            r
            for r in store.changes_of(table, vid)
            if r.get("column_name") in ("tags", "tag", "video_tags")
        ]

    a, b = tagrows("videos"), tagrows("video_tags")
    return a if len(a) >= len(b) else b


def test_title_change_is_logged_with_old_and_new(store):
    # "a trigger writes every changed column to change_log (old value, new value, row version)"
    _video(store)
    store.update_video(VID, {"title": "First title"})
    v1 = store.video(VID)["version"]
    store.update_video(VID, {"title": "Second title"})
    v2 = store.video(VID)["version"]
    assert v2 == v1 + 1  # "version bumps on every update"
    rows = [r for r in store.changes_of("videos", VID) if r["column_name"] == "title"]
    match = [
        r for r in rows if r["old_value"] == "First title" and r["new_value"] == "Second title"
    ]
    assert len(match) == 1
    # changes_of('videos', VID) already scopes table and row; row_version when exposed
    if "row_version" in match[0]:
        assert match[0]["row_version"] == v2  # "the master row's version after the change"


def test_unchanged_column_is_not_logged(store):
    _video(store)
    store.update_video(VID, {"title": "T", "channel_title": "C"})
    store.update_video(VID, {"title": "T2"})
    cols = [
        r["column_name"]
        for r in store.changes_of("videos", VID)
        if r["new_value"] == "C" or r["old_value"] == "C"
    ]
    assert cols == ["channel_title"]  # only the update that changed it


def test_tags_change_logs_removed_and_added_elements(store):
    # "list tables log each element added or removed"; old_value NULL for added,
    # new_value NULL for removed; row_id is the parent's id
    _video(store)
    store.update_video(VID, {"tags": ["alpha", "beta"]})
    n0 = len(_list_changes(store, VID))
    store.update_video(VID, {"tags": ["alpha", "gamma"]})
    new = _list_changes(store, VID)[n0:]
    removed = sorted(r["old_value"] for r in new if r["new_value"] is None)
    added = sorted(r["new_value"] for r in new if r["old_value"] is None)
    assert removed == ["beta"]
    assert added == ["gamma"]


def test_unchanged_list_write_adds_no_change_rows(store, database):
    # storage-schema.md (task): an unchanged list write adds no change-log rows
    _video(store)
    store.update_video(VID, {"tags": ["alpha", "beta"], "topics": ["t"]})
    before = database.one("SELECT COUNT(*) FROM change_log")[0]
    store.update_video(VID, {"tags": ["alpha", "beta"], "topics": ["t"]})
    assert database.one("SELECT COUNT(*) FROM change_log")[0] == before


# 8. Lists round-trip ---------------------------------------------------------


def test_video_lists_round_trip_in_order(store):
    # "The Repository reads them back as Python lists under the old names"
    _video(store)
    tags = ["zeta", "alpha", "mu", "alpha2"]
    topics = ["impermanence", "attachment"]
    people = [{"name": "Ajahn X", "role": "host"}, {"name": "Guest Y", "role": ""}]
    store.update_video(VID, {"tags": tags, "topics": topics, "people": people})
    v = store.video(VID)
    assert v["tags"] == tags
    assert v["topics"] == topics
    assert v["people"] == people
    store.update_video(VID, {"tags": ["only"]})
    assert store.video(VID)["tags"] == ["only"]
    assert store.video(VID)["topics"] == topics


def test_fresh_video_lists_are_empty(store):
    _video(store)
    v = store.video(VID)
    assert v["tags"] == [] and v["topics"] == [] and v["people"] == []
    assert v["status"] == "added"
    assert v["version"] == 1


def test_add_video_twice_returns_false(store):
    _video(store)
    assert store.add_video(VID, "https://www.youtube.com/watch?v=" + VID, created_by="cli") is False


def test_corpus_lists_round_trip(store):
    cid = _corpus(
        store, topic=["meditation", "ethics"], perspective=["Theravada"], sources=["PL123", "UC456"]
    )
    c = store.corpus(cid)
    assert c["name"] == "Dharma"
    assert c["topic"] == ["meditation", "ethics"]
    assert c["perspective"] == ["Theravada"]
    assert c["sources"] == ["PL123", "UC456"]
    store.update_corpus(cid, {"topic": ["ethics", "meditation", "wisdom"], "sources": []})
    c = store.corpus(cid)
    assert c["topic"] == ["ethics", "meditation", "wisdom"]
    assert c["sources"] == []
    assert c["perspective"] == ["Theravada"]


# 9. Membership status derived ------------------------------------------------


def test_member_status_derived_from_video_status(store):
    # "added -> queued, ingested -> ingested, anything else -> failed"
    cid = _corpus(store)
    statuses = {
        "vid00000001": "added",
        "vid00000002": "ingested",
        "vid00000003": "failed",
        "vid00000004": "no_captions",
        "vid00000005": "unavailable",
    }
    for vid, status in statuses.items():
        _video(store, vid)
        if status != "added":
            store.update_video(vid, {"status": status})
        store.add_member(cid, vid, "author", created_by="cli")
    counts = {k: v for k, v in store.member_status_counts(cid).items() if v}
    assert counts == {"queued": 1, "ingested": 1, "failed": 3}
    by_vid = {m["id"]: m for m in store.member_videos(cid)}
    expected = {
        "vid00000001": "queued",
        "vid00000002": "ingested",
        "vid00000003": "failed",
        "vid00000004": "failed",
        "vid00000005": "failed",
    }
    assert {k: by_vid[k]["member_status"] for k in expected} == expected
    assert all(m["member_reason"] == "author" for m in by_vid.values())
    ingested = store.member_videos(cid, ingested_only=True)
    assert [m["id"] for m in ingested] == ["vid00000002"]
    # status follows the video: one fact, one place
    store.update_video("vid00000001", {"status": "ingested"})
    counts = {k: v for k, v in store.member_status_counts(cid).items() if v}
    assert counts == {"ingested": 2, "failed": 3}


# 10. Artifacts ---------------------------------------------------------------


def test_artifacts_for_video_and_corpus_owner(store):
    # artifacts: "exactly one of video_id and corpus_id is set"; "owner: generated"
    _video(store)
    cid = _corpus(store)
    store.record_artifact(
        VID,
        "transcript",
        path="videos/x/transcript.json",
        sha256=HASH_A,
        state="built",
        inputs={"captions": HASH_B},
        provenance={"by": "t"},
        created_by="runner",
    )
    store.record_artifact(
        cid,
        "corpus_note",
        path="corpora/x/note.md",
        sha256=HASH_B,
        state="built",
        inputs={},
        provenance={},
        created_by="runner",
    )
    a = store.artifact(VID, "transcript")
    assert a is not None and a["state"] == "built" and a["sha256"] == HASH_A
    assert a["path"] == "videos/x/transcript.json"
    c = store.artifact(cid, "corpus_note")
    assert c is not None and c["sha256"] == HASH_B
    assert store.artifact(VID, "corpus_note") is None
    assert store.mark_stale(VID, "transcript") is True
    assert store.artifact(VID, "transcript")["state"] == "stale"
    assert store.mark_stale(VID, "no_such_node") is False


def test_record_artifact_rebuilds_in_place(store):
    # derived state: "rebuilt from files; no history"; node "unique per owner"
    _video(store)
    for sha in (HASH_A, HASH_B):
        store.record_artifact(
            VID,
            "transcript",
            path="p",
            sha256=sha,
            state="built",
            inputs={},
            provenance={},
            created_by="runner",
        )
    assert store.artifact(VID, "transcript")["sha256"] == HASH_B


def test_artifact_owner_neither_video_nor_corpus_raises_keyerror(store):
    with pytest.raises(KeyError):
        store.record_artifact(
            "nobody",
            "transcript",
            path="p",
            sha256=HASH_A,
            state="built",
            inputs={},
            provenance={},
            created_by="runner",
        )


def test_artifact_state_outside_enum_is_rejected(store):
    # artifacts.state: "one of built, stale, failed, skipped"
    _video(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.record_artifact(
            VID,
            "transcript",
            path="p",
            sha256=HASH_A,
            state="bogus",
            inputs={},
            provenance={},
            created_by="runner",
        )


# 11. Search ------------------------------------------------------------------


def _segments():
    return [
        {"idx": 0, "t0": 0.0, "t1": 4.5, "speaker": "Host", "text": "welcome to the talk"},
        {"idx": 1, "t0": 4.5, "t1": 9.25, "speaker": "Host", "text": "the noble eightfold path"},
    ]


def test_search_finds_text_with_start_seconds(store):
    # "Store.replace_segments maps" idx/t0/t1 to position/start_seconds/end_seconds
    _video(store, title="Talk one")
    store.replace_segments(VID, _segments())
    assert store.segment_count() == 2
    hits = store.search("eightfold", corpus_id=None, limit=10)
    assert len(hits) == 1
    h = hits[0]
    assert h["video_id"] == VID
    assert h["start_seconds"] == 4.5
    assert h["end_seconds"] == 9.25
    assert h["position"] == 1
    assert h["title"] == "Talk one"


def test_replace_segments_rebuilds_whole(store):
    _video(store)
    store.replace_segments(VID, _segments())
    store.replace_segments(
        VID, [{"idx": 0, "t0": 1.0, "t1": 2.0, "speaker": "", "text": "only mindfulness"}]
    )
    assert store.segment_count() == 1
    assert store.search("eightfold", corpus_id=None, limit=10) == []
    assert len(store.search("mindfulness", corpus_id=None, limit=10)) == 1


def test_search_corpus_filter_excludes_non_members(store):
    _video(store, VID)
    _video(store, VID2)
    store.replace_segments(VID, _segments())
    store.replace_segments(
        VID2, [{"idx": 0, "t0": 7.0, "t1": 8.0, "speaker": "", "text": "eightfold again"}]
    )
    cid = _corpus(store)
    store.add_member(cid, VID2, "author", created_by="cli")
    assert {h["video_id"] for h in store.search("eightfold", corpus_id=None, limit=10)} == {
        VID,
        VID2,
    }
    hits = store.search("eightfold", corpus_id=cid, limit=10)
    assert [h["video_id"] for h in hits] == [VID2]
    assert hits[0]["start_seconds"] == 7.0


def test_search_bad_fts_syntax_raises(store):
    _video(store)
    store.replace_segments(VID, _segments())
    with pytest.raises(sqlite3.OperationalError):
        store.search('"unterminated', corpus_id=None, limit=10)


# 12. Runs --------------------------------------------------------------------


def test_run_lifecycle(store):
    # runs: "opened running and closed once as ok or error"; finished_at "NULL exactly while
    # running"
    rid = store.open_run("workspace", created_by="cli")
    r = store.run(rid)
    assert r["status"] == "running"
    assert r["finished_at"] is None
    assert r["scope"] == "workspace"
    store.close_run(rid, status="ok", videos=3, failed_items=0)
    r = store.run(rid)
    assert r["status"] == "ok"
    assert r["finished_at"] is not None
    assert r["videos"] == 3 and r["failed_items"] == 0


def test_close_run_with_running_status_raises(store):
    rid = store.open_run("workspace", created_by="cli")
    with pytest.raises(ValueError):
        store.close_run(rid, status="running", videos=0, failed_items=0)


def test_last_successful_run_ignores_errors_and_takes_newest(store):
    assert store.last_successful_run() is None
    r1 = store.open_run("workspace", created_by="cli")
    store.close_run(r1, status="ok", videos=1, failed_items=0)
    time.sleep(1.1)
    r2 = store.open_run("corpus:c1", created_by="cli")
    store.close_run(r2, status="error", videos=2, failed_items=1, detail="boom")
    assert store.last_successful_run()["id"] == r1
    time.sleep(1.1)
    r3 = store.open_run("videos:2", created_by="cli")
    store.close_run(r3, status="ok", videos=2, failed_items=0)
    r4 = store.open_run("workspace", created_by="cli")  # still running: not successful
    assert store.last_successful_run()["id"] == r3
    assert store.run(r4)["status"] == "running"


# 13. Migration of a v1 database ----------------------------------------------

V1_TS = "2026-05-01T10:00:00Z"


def _build_v1(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_V1.read_text(encoding="utf-8"))
    base = dict(version=1, created_at=V1_TS, created_by="cli", schema_version="1", metadata="{}")

    def ins(table, row):
        row = {**base, **row}
        cols = ", ".join(row)
        conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values())
        )

    ins(
        "videos",
        dict(
            id=VID,
            url="https://www.youtube.com/watch?v=" + VID,
            title="Old talk",
            channel_id="UC1",
            channel_title="Chan",
            published_at="2024-01-02",
            duration_seconds=3600,
            tags='["zen", "breath"]',
            status="ingested",
            caption_kind="manual",
            metadata_source="dataapi",
            has_caption_track=1,
            fetcher="supadata",
            transcript_state="LLM-CLEANED",
            topics='["impermanence"]',
            people='[{"name": "Ajahn X", "role": "host"}]',
            perspective="",
        ),
    )
    ins(
        "videos",
        dict(
            id=VID2,
            url="https://www.youtube.com/watch?v=" + VID2,
            title="Second",
            status="added",
            perspective="Theravada",
        ),
    )
    ins(
        "corpora",
        dict(
            id="crp_1",
            name="Dharma",
            person="A",
            subject="B",
            topic='["meditation", "ethics"]',
            perspective='["Theravada"]',
            sources='["PL123"]',
            auto_ingest=1,
            llm_cleanup=1,
        ),
    )
    ins(
        "corpus_members",
        dict(id="mem_1", corpus_id="crp_1", video_id=VID, reason="author", status="ingested"),
    )
    ins(
        "corpus_members",
        dict(id="mem_2", corpus_id="crp_1", video_id=VID2, reason="discovered", status="queued"),
    )
    ins(
        "fetch_attempts", dict(id="fa_1", video_id=VID, fetcher="supadata", outcome="ok", detail="")
    )
    ins(
        "artifacts",
        dict(
            id="art_1",
            owner=VID,
            node="transcript",
            path="videos/x/t.json",
            sha256=HASH_A,
            state="built",
            inputs="{}",
            provenance="{}",
        ),
    )
    ins(
        "review_verdicts",
        dict(
            id="rv_1",
            created_at="2026-05-02T09:00:00Z",
            video_id=VID,
            key="amb-0001",
            kind="ambiguity",
            verdict="Dharma",
            span="darma",
            source="review file",
            applied_at="2026-05-02T09:00:00Z",
        ),
    )
    ins(
        "review_verdicts",
        dict(
            id="rv_2",
            created_at="2026-05-02T09:01:00Z",
            video_id=VID,
            key="spk-1",
            kind="speaker",
            verdict="Ajahn X",
            role="host",
            source="phone page",
            applied_at="2026-05-02T09:01:00Z",
        ),
    )
    ins("speaker_verdicts", dict(id="sv_1", video_id=VID, role="host", name="Ajahn X"))
    ins(
        "corrections",
        dict(
            id="cor_1",
            created_at="2026-05-02T09:00:00Z",
            span_norm="darma",
            verdict="Dharma",
            kind="term",
            origin_video=VID,
            origin_key="amb-0001",
        ),
    )
    ins(
        "corrections",
        dict(
            id="cor_2",
            created_at="2026-05-02T09:02:00Z",
            span_norm="sutra",
            verdict="sutta",
            kind="reading",
            origin_video=VID,
            origin_key="amb-0002",
        ),
    )
    for idx, t0, t1, text in [
        (0, 0.0, 3.0, "welcome everyone"),
        (1, 3.0, 7.5, "the noble eightfold path"),
    ]:
        conn.execute(
            "INSERT INTO segments (video_id, idx, t0, t1, speaker, text) VALUES (?,?,?,?,?,?)",
            (VID, idx, t0, t1, "Host", text),
        )
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()


def _rows(path: Path, sql: str):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


RV_SQL = "SELECT id, video_id, key, kind, verdict, created_at FROM review_verdicts ORDER BY id"


@pytest.fixture
def migrated(dbpath):
    _build_v1(dbpath)
    original_rv = _rows(dbpath, RV_SQL)
    d = db.Database(dbpath)
    yield d, Store(d), original_rv
    d.close()


def test_v1_upgrades_to_user_version(migrated, dbpath):
    # "upgrades an older database in place"
    d, _, _ = migrated
    assert d.one("PRAGMA user_version")[0] == db.USER_VERSION
    assert d.one("PRAGMA integrity_check")[0] == "ok"
    assert d.all("PRAGMA foreign_key_check") == []


def test_v1_backup_is_verified_and_complete(migrated, dbpath):
    # "first a backup ... to <workspace>/backups/<db file stem>-<UTC timestamp>-before-v2.sqlite ...
    #  verified with PRAGMA integrity_check and a row count of every author table"
    _, _, original_rv = migrated
    bdir = dbpath.parent / db.BACKUPS_DIRNAME
    assert db.BACKUPS_DIRNAME == "backups"
    files = [p for p in bdir.iterdir() if p.is_file() and p.suffix == ".sqlite"]
    assert len(files) == 1
    b = files[0]
    assert b.name.startswith("transcript-") and b.name.endswith("-before-v2.sqlite")
    assert _rows(b, "PRAGMA integrity_check")[0][0] == "ok"
    assert _rows(b, RV_SQL) == original_rv
    assert _rows(b, "SELECT COUNT(*) FROM speaker_verdicts")[0][0] == 1
    assert _rows(b, "SELECT COUNT(*) FROM corrections")[0][0] == 2
    assert _rows(b, "PRAGMA user_version")[0][0] == 0  # the copy is from before


def test_v1_author_decisions_survive(migrated):
    # every review verdict and correction survives with the same id, verdict, created_at
    _, store, _ = migrated
    cur = store.current_review_verdicts(VID)
    assert set(cur) == {"amb-0001", "spk-1"}
    assert (cur["amb-0001"]["id"], cur["amb-0001"]["verdict"], cur["amb-0001"]["created_at"]) == (
        "rv_1",
        "Dharma",
        "2026-05-02T09:00:00Z",
    )
    assert (cur["spk-1"]["id"], cur["spk-1"]["verdict"], cur["spk-1"]["created_at"]) == (
        "rv_2",
        "Ajahn X",
        "2026-05-02T09:01:00Z",
    )
    assert cur["spk-1"]["source"] == "phone page"
    assert cur["amb-0001"]["span"] == "darma"
    cors = store.current_corrections()
    assert {k: (v["id"], v["verdict"], v["kind"], v["created_at"]) for k, v in cors.items()} == {
        "darma": ("cor_1", "Dharma", "term", "2026-05-02T09:00:00Z"),
        "sutra": ("cor_2", "sutta", "reading", "2026-05-02T09:02:00Z"),
    }
    assert store.correction_count() == 2
    assert store.current_speaker_names(VID) == {"host": "Ajahn X"}


def test_v1_migration_writes_no_change_log(migrated):
    # "creates triggers and views (so the copy writes no change-log rows)"
    d, _, _ = migrated
    assert d.one("SELECT COUNT(*) FROM change_log")[0] == 0


def test_v1_lists_read_back_through_store(migrated):
    _, store, _ = migrated
    v = store.video(VID)
    assert v["tags"] == ["zen", "breath"]
    assert v["topics"] == ["impermanence"]
    assert v["people"] == [{"name": "Ajahn X", "role": "host"}]
    assert v["title"] == "Old talk"
    assert v["duration_seconds"] == 3600
    c = store.corpus("crp_1")
    assert c["topic"] == ["meditation", "ethics"]
    assert c["perspective"] == ["Theravada"]
    assert c["sources"] == ["PL123"]


def test_v1_empty_perspective_reads_as_none(migrated):
    # "A v1 perspective of '' becomes NULL"
    _, store, _ = migrated
    assert store.video(VID)["perspective"] is None
    assert store.video(VID2)["perspective"] == "Theravada"


def test_v1_members_status_derived_and_search_works(migrated):
    _, store, _ = migrated
    by_vid = {m["id"]: m for m in store.member_videos("crp_1")}
    assert by_vid[VID]["member_status"] == "ingested"
    assert by_vid[VID2]["member_status"] == "queued"
    assert by_vid[VID2]["member_reason"] == "discovered"
    hits = store.search("eightfold", corpus_id=None, limit=10)
    assert len(hits) == 1
    assert hits[0]["video_id"] == VID
    assert hits[0]["start_seconds"] == 3.0
    assert hits[0]["position"] == 1
    assert len(store.search("eightfold", corpus_id="crp_1", limit=10)) == 1
    a = store.artifact(VID, "transcript")
    assert a is not None and a["sha256"] == HASH_A


def test_v1_reopen_is_a_noop(migrated, dbpath):
    d, _, _ = migrated
    d.close()
    d2 = db.Database(dbpath)
    try:
        assert d2.one("PRAGMA user_version")[0] == db.USER_VERSION
        assert d2.one("SELECT COUNT(*) FROM review_verdicts")[0] == 2
    finally:
        d2.close()
    files = [p for p in (dbpath.parent / "backups").iterdir() if p.suffix == ".sqlite"]
    assert len(files) == 1  # no second backup for a current database


# 14. Newer database declined ------------------------------------------------


def test_newer_database_is_declined_untouched(dbpath):
    # "declines a database whose version is newer than this code (NewerDatabase; nothing is
    # touched)"
    d = db.Database(dbpath)
    s = Store(d)
    _video(s, title="Keep me")
    d.close()
    conn = sqlite3.connect(dbpath)
    conn.execute(f"PRAGMA user_version = {db.USER_VERSION + 1}")
    conn.commit()
    dump_before = list(conn.iterdump())
    conn.close()
    with pytest.raises(db.NewerDatabase):
        db.Database(dbpath)
    conn = sqlite3.connect(dbpath)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.USER_VERSION + 1
        assert list(conn.iterdump()) == dump_before
    finally:
        conn.close()
    bdir = dbpath.parent / "backups"
    assert not bdir.exists() or not any(bdir.iterdir())


# 15. canonical_sha256 --------------------------------------------------------


def test_canonical_sha256_matches_hand_computation():
    # "SHA-256 of canonical JSON (keys sorted, no whitespace, UTF-8)"
    obj = {"type": "ambiguity", "video_id": VID, "span": "dhárma é", "n": [1, 2, {"z": 1, "a": 2}]}
    reordered = {
        "span": "dhárma é",
        "n": [1, 2, {"a": 2, "z": 1}],
        "video_id": VID,
        "type": "ambiguity",
    }
    expected = hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert db.canonical_sha256(obj) == expected
    assert db.canonical_sha256(reordered) == expected
    assert len(expected) == 64
    assert db.canonical_sha256({"type": "speaker", "video_id": VID, "role": "host"}) != expected


_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=6), children, max_size=4)
    ),
    max_leaves=12,
)


# A hash of a small JSON object takes microseconds; two seconds absorbs a
# loaded machine without
# hiding a hash that has really become slow.
HASH_DEADLINE = timedelta(seconds=2)


@settings(deadline=HASH_DEADLINE)
@given(obj=st.dictionaries(st.text(max_size=6), _json, max_size=6), data=st.data())
def test_canonical_sha256_ignores_key_order(obj, data):
    """Property (written with the code; docs/design/storage-schema.md, "A
    verdict carries the content hash of what it judged"): the content hash
    of a JSON object does not depend on the order its keys were written in.
    (That ytlm's runner hash is the same function is tested in ytlm.)"""
    keys = data.draw(st.permutations(list(obj)))
    shuffled = {k: obj[k] for k in keys}
    assert db.canonical_sha256(shuffled) == db.canonical_sha256(obj)
    assert re.fullmatch(r"[0-9a-f]{64}", db.canonical_sha256(obj))


@settings(deadline=HASH_DEADLINE)
@given(a=_json, b=_json)
def test_different_json_values_get_different_hashes(a, b):
    if json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True):
        assert db.canonical_sha256(a) != db.canonical_sha256(b)


def test_the_hash_properties_carry_an_explicit_deadline():
    """Incident 2026-10-03: the key-order property hit hypothesis's default
    200 ms deadline once (302 ms) in a full run and passed on rerun. The
    hash of a small JSON object takes microseconds; what the deadline
    caught was the machine, so each hash property states its own."""
    for prop in (
        test_canonical_sha256_ignores_key_order,
        test_different_json_values_get_different_hashes,
    ):
        deadline = prop._hypothesis_internal_use_settings.deadline  # type: ignore[attr-defined]
        assert deadline == HASH_DEADLINE, prop.__name__


# ---- the schema document and the Repository boundary (written with the code) ----

DOC = Path(__file__).parent.parent / "docs" / "design" / "storage-schema.md"
SRC = Path(__file__).parent.parent / "src" / "corpuslm"
BASE = ("id", "version", "created_at", "created_by", "schema_version", "metadata")


def _doc_sections() -> dict[str, dict]:
    """`### \\`name\\`` headings of storage-schema.md: kind, history line,
    column rows (name, type, null, meaning), and any "every column of" line."""
    out: dict[str, dict] = {}
    current = None
    for line in DOC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^### `([a-z_]+)`( \(view\))?$", line)
        if m:
            current = out[m.group(1)] = {
                "view": bool(m.group(2)),
                "history": None,
                "columns": [],
                "same_as": None,
            }
            continue
        if line.startswith("## "):
            current = None
        if current is None:
            continue
        h = re.match(r"^History: (.+)\.$", line)
        if h:
            current["history"] = h.group(1)
        s = re.match(r"^Every column of `([a-z_]+)`", line)
        if s:
            current["same_as"] = s.group(1)
        if line.startswith("| ") and not line.startswith(("| column |", "|---")):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            current["columns"].append(tuple(cells))
    return out


def test_the_schema_document_mirrors_the_code():
    """coding.md, "Storage and migrations": the stored schema is defined in
    code; its doc mirrors it and a test fails if they differ."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    objects = {
        name: kind
        for name, kind in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'segments_fts%'"
        )
    }
    doc = _doc_sections()
    assert set(doc) == set(objects)
    history = {t: "master" for t in db.MASTER_COLUMNS}
    history |= {t: "append-only" for t in db.APPEND_ONLY}
    history |= {
        t: f"list `{name}` of `{p}`"
        for t, p, name in (
            (t, "videos" if parent == "video_id" else "corpora", name)
            for t, parent, name, _ in db.LIST_TABLES
        )
    }
    history |= {
        "runs": "runs",
        "artifacts": "derived",
        "segments": "derived",
        "locks": "coordination",
    }
    for name, kind in objects.items():
        section = doc[name]
        assert section["view"] == (kind == "view"), name
        cols = conn.execute(f"PRAGMA table_xinfo({name})").fetchall()
        if section["same_as"]:
            same = [c[1] for c in conn.execute(f"PRAGMA table_xinfo({section['same_as']})")]
            assert [c[1] for c in cols] == same, name
            continue
        if kind == "table":
            assert section["history"] == history[name], name
        want = []
        for _, col, typ, notnull, _, pk, _ in cols:
            if (
                kind == "table"
                and col in BASE
                and section["columns"][:1] == [("(base)", "", "", "")]
            ):
                continue
            null = "" if kind == "view" else ("no" if notnull or pk else "yes")
            want.append((col, typ, null))
        got = [(c, t, n) for c, t, n, _ in section["columns"] if c != "(base)"]
        assert got == want, name
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
        checks = dict(re.findall(r"CHECK \((\w+) IN \(([^)]*)\)\)", sql))
        for col, _type, _null, meaning in section["columns"]:
            if kind == "table" and col in checks:
                for value in re.findall(r"'([^']*)'", checks[col]):
                    assert f"`{value}`" in meaning, (name, col, value)


def test_master_data_logs_every_column_but_the_identity_ones():
    """architecture.md, "History and identity": master data keeps a
    field-level change log by trigger; a column left out would change
    without a trace."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    for table, logged in db.MASTER_COLUMNS.items():
        columns = [c[1] for c in conn.execute(f"PRAGMA table_xinfo({table})")]
        expected = [c for c in columns if c not in BASE or c == "metadata"]
        if table == "corpus_members":
            expected = [c for c in expected if c not in ("corpus_id", "video_id")]
        assert sorted(logged) == sorted(expected), table


# A module may reach the workspace database only through store.Store.
SQL = re.compile(
    r"""["'](SELECT\s|INSERT INTO|UPDATE \w+ SET|DELETE FROM|CREATE (TABLE|VIEW|INDEX|TRIGGER)"""
    r"""|ALTER TABLE|DROP (TABLE|VIEW|INDEX|TRIGGER))"""
)
RAW_ACCESS = re.compile(r"\bdb\.(one|all|execute|insert|update)\(")
NOT_YET: set[str] = set()


def test_no_module_but_the_repository_speaks_sql():
    """architecture.md, "OOAD patterns": Repository, one module owns reads
    and writes of a store (storage-schema.md, "One module owns the store")."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in ("db.py", "store.py") or rel in NOT_YET:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if SQL.search(line) or RAW_ACCESS.search(line):
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert offenders == []


def test_a_database_another_process_upgraded_meanwhile_is_left_to_it(tmp_path, monkeypatch):
    """coding.md, "Storage and migrations": every migration runs in one
    explicit transaction; the version is re-read once the write lock is held,
    so a second process never rebuilds an already upgraded database."""
    path = tmp_path / "transcript.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript((Path(__file__).parent / "fixtures" / "schema_v1.sql").read_text())
    conn.close()
    real_backup = db.backup
    others = []

    def backup_while_another_process_migrates(c, p, *, label):
        saved = real_backup(c, p, label=label)
        if not others:  # the other process opens and upgrades first
            others.append(p)
            db.Database(p).close()
        return saved

    monkeypatch.setattr(db, "backup", backup_while_another_process_migrates)
    first = sqlite3.connect(path)
    done = db.migrate(first, path)
    assert done["by"] == "another process"
    assert first.execute("PRAGMA user_version").fetchone()[0] == db.USER_VERSION
    assert first.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# 16. Backup retention: the newest three (owner ruling 2026-10-03) ----------


def _old_backups(dbpath, n: int) -> list[str]:
    bdir = dbpath.parent / db.BACKUPS_DIRNAME
    bdir.mkdir(exist_ok=True)
    names = [f"{dbpath.stem}-2026010{i}T000000Z-before-v2.sqlite" for i in range(1, n + 1)]
    for name in names:
        (bdir / name).write_bytes(b"old")
    (bdir / "notes.txt").write_text("the owner's")
    (bdir / "other-20260101T000000Z-x.sqlite").write_bytes(b"not ours")
    return names


def test_a_verified_backup_leaves_the_newest_three(dbpath):
    old = _old_backups(dbpath, 4)
    _build_v1(dbpath)
    d = db.Database(dbpath)
    d.close()
    assert sorted(d.migration["backups_deleted"]) == old[:2]
    bdir = dbpath.parent / db.BACKUPS_DIRNAME
    ours = sorted(p.name for p in bdir.iterdir() if p.name.startswith(f"{dbpath.stem}-"))
    assert len(ours) == 3 and ours[:2] == old[2:]
    assert ours[2] == Path(d.migration["backup"]).name
    assert (bdir / "notes.txt").exists() and (bdir / "other-20260101T000000Z-x.sqlite").exists()
