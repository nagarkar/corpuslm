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
from hypothesis import given, settings, strategies

from corpuslm import db
from corpuslm.store import Store

REPO = Path(__file__).parent.parent
SCHEMA_V1 = REPO / "tests" / "fixtures" / "schema_v1.sql"

VIDEO_ID = "vidAAAAAAA1"
SECOND_VIDEO_ID = "vidBBBBBBB2"
HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture
def dbpath(tmp_path):
    return tmp_path / "transcript.sqlite"


@pytest.fixture
def database(dbpath):
    database_handle = db.Database(dbpath)
    yield database_handle
    database_handle.close()


@pytest.fixture
def store(database):
    return Store(database)


def _video(store: Store, video_id: str = VIDEO_ID, **changes) -> str:
    assert store.add_source(
        video_id, f"https://www.youtube.com/watch?v={video_id}", created_by="cli"
    )
    if changes:
        store.update_source(video_id, changes)
    return video_id


def _corpus(store: Store, name: str = "Dharma", **extra) -> str:
    fields = {
        "name": name,
        "person": "A Teacher",
        "subject": "Buddhism",
        "topic": [],
        "perspective": [],
        "auto_ingest": 0,
        "llm_cleanup": 1,
    }
    fields.update(extra)
    return store.add_corpus(fields, created_by="cli")


def _verdict(
    store: Store,
    video_id=VIDEO_ID,
    key="amb-0001",
    verdict="Dharma",
    sha256=HASH_A,
    kind="ambiguity",
):
    return store.append_review_verdict(
        source_id=video_id,
        key=key,
        kind=kind,
        verdict=verdict,
        span="darma",
        role="",
        answered_through="review file",
        subject_sha256=sha256,
        created_by="cli",
    )


# 1. Fresh file gets the schema ---------------------------------------------


def test_fresh_file_gets_user_version(database, dbpath):
    # storage-schema.md: "The schema version is PRAGMA user_version (db.USER_VERSION, now 10)"
    assert db.USER_VERSION == 10
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
    for table_row in rows:
        name, sql = table_row[0], table_row[1] or ""
        if sql.upper().startswith("CREATE VIRTUAL TABLE") or "segments_fts" in name:
            continue  # FTS5 index and its shadow tables are owned by SQLite
        assert "STRICT" in sql.upper(), f"{name} is not STRICT"
        checked += 1
    # every table named in the Tables section (excluding segments_fts) exists
    names = {row[0] for row in rows}
    for table_name in (
        "sources",
        "source_tags",
        "source_topics",
        "source_people",
        "corpora",
        "corpus_topics",
        "corpus_perspectives",
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
        "cleaned_segments",
    ):
        assert table_name in names, table_name
    assert checked >= 18


def test_foreign_keys_on(database):
    # storage-schema.md: "PRAGMA foreign_keys = ON on every connection"
    assert database.one("PRAGMA foreign_keys")[0] == 1


# 2. CHECK constraints --------------------------------------------------------


def test_video_status_outside_enum_is_rejected(store, database):
    # storage-schema.md videos.status: "one of added, ingested, no_captions, unavailable, failed"
    _video(store)
    with pytest.raises(sqlite3.IntegrityError):
        database.update("sources", VIDEO_ID, {"status": "bogus"})
    assert store.source(VIDEO_ID)["status"] == "added"


def test_member_reason_outside_enum_is_rejected(store):
    # corpus_members.reason: "one of author, discovered, consumer"
    _video(store)
    corpus_id = _corpus(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.add_member(corpus_id, VIDEO_ID, "bogus", created_by="cli")
    assert not store.is_member(corpus_id, VIDEO_ID)


def test_corrupt_json_metadata_is_rejected(store, database):
    # "JSON columns carry CHECK (json_valid(...))"
    _video(store)
    with pytest.raises(sqlite3.IntegrityError):
        database.update("sources", VIDEO_ID, {"metadata": "{not json"})


# 3. FOREIGN KEYs -------------------------------------------------------------


def test_fetch_attempt_for_unknown_video_is_rejected(store):
    # "every reference has a FOREIGN KEY"
    with pytest.raises(sqlite3.IntegrityError):
        store.append_fetch_attempt("noSuchVideo", "supadata", "ok", "", created_by="runner")


def test_review_verdict_for_unknown_video_is_rejected(store):
    with pytest.raises(sqlite3.IntegrityError):
        _verdict(store, video_id="noSuchVideo")


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
    history = store.review_verdict_history(VIDEO_ID, "amb-0001")
    assert len(history) == 2
    assert [verdict_entry["verdict"] for verdict_entry in history] == [
        "Dharma",
        "Dhamma",
    ]  # oldest first
    current = store.current_review_verdicts(VIDEO_ID)
    assert set(current) == {"amb-0001"}
    assert current["amb-0001"]["verdict"] == "Dhamma"
    assert current["amb-0001"]["id"] == second


def test_newest_in_force_with_many_rewrites(store):
    # same rule, several rewrites in quick succession (same timestamp second)
    _video(store)
    for text in ["one", "two", "three", "four", "five"]:
        _verdict(store, verdict=text)
    assert store.current_review_verdicts(VIDEO_ID)["amb-0001"]["verdict"] == "five"
    assert len(store.review_verdict_history(VIDEO_ID, "amb-0001")) == 5


def test_previous_subject_sha256_lineage(store):
    # "When a new verdict for the same key judged different content,
    #  previous_subject_sha256 holds the old hash (lineage: new hash -> old hash)"
    _video(store)
    _verdict(store, verdict="first", sha256=HASH_A)
    _verdict(store, verdict="second", sha256=HASH_A)  # same content, changed answer
    _verdict(store, verdict="third", sha256=HASH_B)  # different content
    verdict_history = store.review_verdict_history(VIDEO_ID, "amb-0001")
    assert [verdict_entry["subject_sha256"] for verdict_entry in verdict_history] == [
        HASH_A,
        HASH_A,
        HASH_B,
    ]
    assert verdict_history[0]["previous_subject_sha256"] is None  # first in slot
    assert verdict_history[1]["previous_subject_sha256"] is None  # same hash as the slot's current
    assert verdict_history[2]["previous_subject_sha256"] == HASH_A
    assert store.current_review_verdicts(VIDEO_ID)["amb-0001"]["previous_subject_sha256"] == HASH_A


def test_lineage_is_per_slot(store):
    _video(store)
    _verdict(store, key="amb-0001", sha256=HASH_A)
    _verdict(store, key="amb-0002", sha256=HASH_B)
    verdict_history = store.review_verdict_history(VIDEO_ID, "amb-0002")
    assert verdict_history[0]["previous_subject_sha256"] is None


def _fill_append_only(store):
    _video(store)
    _verdict(store)
    store.append_speaker_verdict(source_id=VIDEO_ID, role="host", name="Ajahn", created_by="cli")
    store.append_correction(
        span_norm="darma",
        verdict="Dharma",
        kind="term",
        origin_source=VIDEO_ID,
        origin_key="amb-0001",
        created_by="cli",
    )
    store.append_fetch_attempt(VIDEO_ID, "supadata", "ok", "", created_by="runner")
    store.update_source(VIDEO_ID, {"title": "A talk"})  # writes change_log


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
    # vw_current_speaker_verdicts: "the newest row per (source_id, role)"
    _video(store)
    _video(store, SECOND_VIDEO_ID)
    store.append_speaker_verdict(source_id=VIDEO_ID, role="host", name="Old", created_by="cli")
    store.append_speaker_verdict(source_id=VIDEO_ID, role="guest", name="Guest", created_by="cli")
    store.append_speaker_verdict(source_id=VIDEO_ID, role="host", name="New", created_by="cli")
    store.append_speaker_verdict(
        source_id=SECOND_VIDEO_ID, role="host", name="Other", created_by="cli"
    )
    assert store.current_speaker_names(VIDEO_ID) == {"host": "New", "guest": "Guest"}
    assert store.current_speaker_names(SECOND_VIDEO_ID) == {"host": "Other"}


def test_current_corrections_newest_per_span(store):
    # vw_current_corrections: "the newest row per span_norm"; correction_count:
    # "Rules in force (distinct normalized spans)"
    _video(store)
    store.append_correction(
        span_norm="darma",
        verdict="Dharma",
        kind="term",
        origin_source=VIDEO_ID,
        origin_key="amb-0001",
        created_by="cli",
    )
    store.append_correction(
        span_norm="sutta",
        verdict="Sutta",
        kind="term",
        origin_source=VIDEO_ID,
        origin_key="amb-0002",
        created_by="cli",
    )
    store.append_correction(
        span_norm="darma",
        verdict="Dhamma",
        kind="reading",
        origin_source=VIDEO_ID,
        origin_key="amb-0003",
        created_by="cli",
    )
    corrections = store.current_corrections()
    assert set(corrections) == {"darma", "sutta"}
    assert corrections["darma"]["verdict"] == "Dhamma"
    assert corrections["darma"]["kind"] == "reading"
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
            origin_source=VIDEO_ID,
            origin_key="amb-0001",
            created_by="cli",
        )


# 7. Master data change log ---------------------------------------------------


def _list_changes(store, video_id):
    # The spec leaves open whether a list change is filed under the master table or
    # the list table ("table_name: the master or list table"); accept either.
    def tagrows(table):
        return [
            row
            for row in store.changes_of(table, video_id)
            if row.get("column_name") in ("tags", "tag", "source_tags")
        ]

    first_changes, second_changes = tagrows("sources"), tagrows("source_tags")
    return first_changes if len(first_changes) >= len(second_changes) else second_changes


def test_title_change_is_logged_with_old_and_new(store):
    # "a trigger writes every changed column to change_log (old value, new value, row version)"
    _video(store)
    store.update_source(VIDEO_ID, {"title": "First title"})
    first_version = store.source(VIDEO_ID)["version"]
    store.update_source(VIDEO_ID, {"title": "Second title"})
    second_version = store.source(VIDEO_ID)["version"]
    assert second_version == first_version + 1  # "version bumps on every update"
    rows = [row for row in store.changes_of("sources", VIDEO_ID) if row["column_name"] == "title"]
    match = [
        row
        for row in rows
        if row["old_value"] == "First title" and row["new_value"] == "Second title"
    ]
    assert len(match) == 1
    # changes_of('sources', VID) already scopes table and row; row_version when exposed
    if "row_version" in match[0]:
        assert (
            match[0]["row_version"] == second_version
        )  # "the master row's version after the change"


def test_unchanged_column_is_not_logged(store):
    _video(store)
    store.update_source(VIDEO_ID, {"title": "T", "channel_title": "C"})
    store.update_source(VIDEO_ID, {"title": "T2"})
    column_names = [
        row["column_name"]
        for row in store.changes_of("sources", VIDEO_ID)
        if row["new_value"] == "C" or row["old_value"] == "C"
    ]
    assert column_names == ["channel_title"]  # only the update that changed it


def test_tags_change_logs_removed_and_added_elements(store):
    # "list tables log each element added or removed"; old_value NULL for added,
    # new_value NULL for removed; row_id is the parent's id
    _video(store)
    store.update_source(VIDEO_ID, {"tags": ["alpha", "beta"]})
    change_count_before = len(_list_changes(store, VIDEO_ID))
    store.update_source(VIDEO_ID, {"tags": ["alpha", "gamma"]})
    new = _list_changes(store, VIDEO_ID)[change_count_before:]
    removed = sorted(row["old_value"] for row in new if row["new_value"] is None)
    added = sorted(row["new_value"] for row in new if row["old_value"] is None)
    assert removed == ["beta"]
    assert added == ["gamma"]


def test_unchanged_list_write_adds_no_change_rows(store, database):
    # storage-schema.md (task): an unchanged list write adds no change-log rows
    _video(store)
    store.update_source(VIDEO_ID, {"tags": ["alpha", "beta"], "topics": ["t"]})
    before = database.one("SELECT COUNT(*) FROM change_log")[0]
    store.update_source(VIDEO_ID, {"tags": ["alpha", "beta"], "topics": ["t"]})
    assert database.one("SELECT COUNT(*) FROM change_log")[0] == before


# 8. Lists round-trip ---------------------------------------------------------


def test_video_lists_round_trip_in_order(store):
    # "The Repository reads them back as Python lists under the old names"
    _video(store)
    tags = ["zeta", "alpha", "mu", "alpha2"]
    topics = ["impermanence", "attachment"]
    people = [{"name": "Ajahn X", "role": "host"}, {"name": "Guest Y", "role": ""}]
    store.update_source(VIDEO_ID, {"tags": tags, "topics": topics, "people": people})
    source = store.source(VIDEO_ID)
    assert source["tags"] == tags
    assert source["topics"] == topics
    assert source["people"] == people
    store.update_source(VIDEO_ID, {"tags": ["only"]})
    assert store.source(VIDEO_ID)["tags"] == ["only"]
    assert store.source(VIDEO_ID)["topics"] == topics


def test_fresh_video_lists_are_empty(store):
    _video(store)
    source = store.source(VIDEO_ID)
    assert source["tags"] == [] and source["topics"] == [] and source["people"] == []
    assert source["status"] == "added"
    assert source["version"] == 1


def test_add_source_twice_returns_false(store):
    _video(store)
    assert (
        store.add_source(VIDEO_ID, "https://www.youtube.com/watch?v=" + VIDEO_ID, created_by="cli")
        is False
    )


def test_corpus_lists_round_trip(store):
    corpus_id = _corpus(store, topic=["meditation", "ethics"], perspective=["Theravada"])
    corpus = store.corpus(corpus_id)
    assert corpus["name"] == "Dharma"
    assert corpus["topic"] == ["meditation", "ethics"]
    assert corpus["perspective"] == ["Theravada"]
    store.update_corpus(corpus_id, {"topic": ["ethics", "meditation", "wisdom"]})
    corpus = store.corpus(corpus_id)
    assert corpus["topic"] == ["ethics", "meditation", "wisdom"]
    assert corpus["perspective"] == ["Theravada"]


# 9. Membership status derived ------------------------------------------------


def test_member_status_derived_from_video_status(store):
    # "added -> queued, ingested -> ingested, anything else -> failed"
    corpus_id = _corpus(store)
    statuses = {
        "vid00000001": "added",
        "vid00000002": "ingested",
        "vid00000003": "failed",
        "vid00000004": "no_captions",
        "vid00000005": "unavailable",
    }
    for video_id, status in statuses.items():
        _video(store, video_id)
        if status != "added":
            store.update_source(video_id, {"status": status})
        store.add_member(corpus_id, video_id, "author", created_by="cli")
    counts = {
        status: count for status, count in store.member_status_counts(corpus_id).items() if count
    }
    assert counts == {"queued": 1, "ingested": 1, "failed": 3}
    by_vid = {member["id"]: member for member in store.member_sources(corpus_id)}
    expected = {
        "vid00000001": "queued",
        "vid00000002": "ingested",
        "vid00000003": "failed",
        "vid00000004": "failed",
        "vid00000005": "failed",
    }
    assert {video_id: by_vid[video_id]["member_status"] for video_id in expected} == expected
    assert all(member["member_reason"] == "author" for member in by_vid.values())
    ingested = store.member_sources(corpus_id, ingested_only=True)
    assert [member["id"] for member in ingested] == ["vid00000002"]
    # status follows the video: one fact, one place
    store.update_source("vid00000001", {"status": "ingested"})
    counts = {
        status: count for status, count in store.member_status_counts(corpus_id).items() if count
    }
    assert counts == {"ingested": 2, "failed": 3}


# 10. Artifacts ---------------------------------------------------------------


def test_artifacts_for_video_and_corpus_owner(store):
    # artifacts: "exactly one of source_id and corpus_id is set"; "owner: generated"
    _video(store)
    corpus_id = _corpus(store)
    store.record_artifact(
        VIDEO_ID,
        "transcript",
        path="videos/x/transcript.json",
        sha256=HASH_A,
        state="built",
        inputs={"captions": HASH_B},
        provenance={"by": "t"},
        created_by="runner",
    )
    store.record_artifact(
        corpus_id,
        "corpus_note",
        path="corpora/x/note.md",
        sha256=HASH_B,
        state="built",
        inputs={},
        provenance={},
        created_by="runner",
    )
    transcript_artifact = store.artifact(VIDEO_ID, "transcript")
    assert (
        transcript_artifact is not None
        and transcript_artifact["state"] == "built"
        and transcript_artifact["sha256"] == HASH_A
    )
    assert transcript_artifact["path"] == "videos/x/transcript.json"
    corpus_note = store.artifact(corpus_id, "corpus_note")
    assert corpus_note is not None and corpus_note["sha256"] == HASH_B
    assert store.artifact(VIDEO_ID, "corpus_note") is None
    assert store.mark_stale(VIDEO_ID, "transcript") is True
    assert store.artifact(VIDEO_ID, "transcript")["state"] == "stale"
    assert store.mark_stale(VIDEO_ID, "no_such_node") is False


def test_record_artifact_rebuilds_in_place(store):
    # derived state: "rebuilt from files; no history"; node "unique per owner"
    _video(store)
    for sha256 in (HASH_A, HASH_B):
        store.record_artifact(
            VIDEO_ID,
            "transcript",
            path="p",
            sha256=sha256,
            state="built",
            inputs={},
            provenance={},
            created_by="runner",
        )
    assert store.artifact(VIDEO_ID, "transcript")["sha256"] == HASH_B


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
            VIDEO_ID,
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
    store.replace_segments(VIDEO_ID, _segments())
    assert store.segment_count() == 2
    hits = store.search("eightfold", corpus_id=None, limit=10)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["source_id"] == VIDEO_ID
    assert hit["start_seconds"] == 4.5
    assert hit["end_seconds"] == 9.25
    assert hit["position"] == 1
    assert hit["title"] == "Talk one"


def test_replace_segments_rebuilds_whole(store):
    _video(store)
    store.replace_segments(VIDEO_ID, _segments())
    store.replace_segments(
        VIDEO_ID, [{"idx": 0, "t0": 1.0, "t1": 2.0, "speaker": "", "text": "only mindfulness"}]
    )
    assert store.segment_count() == 1
    assert store.search("eightfold", corpus_id=None, limit=10) == []
    assert len(store.search("mindfulness", corpus_id=None, limit=10)) == 1


def test_segments_reads_back_what_replace_segments_wrote_in_order(store):
    _video(store)
    _video(store, SECOND_VIDEO_ID)
    store.replace_segments(VIDEO_ID, list(reversed(_segments())))
    store.replace_segments(SECOND_VIDEO_ID, [{"idx": 0, "t0": 0.0, "t1": 1.0, "text": "other"}])
    assert store.segments(VIDEO_ID) == _segments()
    assert store.segments(SECOND_VIDEO_ID) == [
        {"idx": 0, "t0": 0.0, "t1": 1.0, "speaker": "", "text": "other"}
    ]
    assert store.segments("nobody") == []


def test_search_corpus_filter_excludes_non_members(store):
    _video(store, VIDEO_ID)
    _video(store, SECOND_VIDEO_ID)
    store.replace_segments(VIDEO_ID, _segments())
    store.replace_segments(
        SECOND_VIDEO_ID,
        [{"idx": 0, "t0": 7.0, "t1": 8.0, "speaker": "", "text": "eightfold again"}],
    )
    corpus_id = _corpus(store)
    store.add_member(corpus_id, SECOND_VIDEO_ID, "author", created_by="cli")
    assert {hit["source_id"] for hit in store.search("eightfold", corpus_id=None, limit=10)} == {
        VIDEO_ID,
        SECOND_VIDEO_ID,
    }
    hits = store.search("eightfold", corpus_id=corpus_id, limit=10)
    assert [hit["source_id"] for hit in hits] == [SECOND_VIDEO_ID]
    assert hits[0]["start_seconds"] == 7.0


def test_search_bad_fts_syntax_raises(store):
    _video(store)
    store.replace_segments(VIDEO_ID, _segments())
    with pytest.raises(sqlite3.OperationalError):
        store.search('"unterminated', corpus_id=None, limit=10)


# 12. Runs --------------------------------------------------------------------


def test_run_lifecycle(store):
    # runs: "opened running and closed once as ok or error"; finished_at "NULL exactly while
    # running"
    rid = store.open_run("workspace", created_by="cli")
    run = store.run(rid)
    assert run["status"] == "running"
    assert run["finished_at"] is None
    assert run["scope"] == "workspace"
    store.close_run(rid, status="ok", sources=3, failed_items=0)
    run = store.run(rid)
    assert run["status"] == "ok"
    assert run["finished_at"] is not None
    assert run["sources"] == 3 and run["failed_items"] == 0


def test_close_run_with_running_status_raises(store):
    rid = store.open_run("workspace", created_by="cli")
    with pytest.raises(ValueError):
        store.close_run(rid, status="running", sources=0, failed_items=0)


def test_last_successful_run_ignores_errors_and_takes_newest(store):
    assert store.last_successful_run() is None
    first_run = store.open_run("workspace", created_by="cli")
    store.close_run(first_run, status="ok", sources=1, failed_items=0)
    time.sleep(1.1)
    second_run = store.open_run("corpus:c1", created_by="cli")
    store.close_run(second_run, status="error", sources=2, failed_items=1, detail="boom")
    assert store.last_successful_run()["id"] == first_run
    time.sleep(1.1)
    third_run = store.open_run("videos:2", created_by="cli")
    store.close_run(third_run, status="ok", sources=2, failed_items=0)
    fourth_run = store.open_run("workspace", created_by="cli")  # still running: not successful
    assert store.last_successful_run()["id"] == third_run
    assert store.run(fourth_run)["status"] == "running"


# 13. Migration of a v1 database ----------------------------------------------

V1_TS = "2026-05-01T10:00:00Z"


def _build_v1(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V1.read_text(encoding="utf-8"))
    base = dict(version=1, created_at=V1_TS, created_by="cli", schema_version="1", metadata="{}")

    def ins(table, row):
        row = {**base, **row}
        column_list = ", ".join(row)
        connection.execute(
            f"INSERT INTO {table} ({column_list}) VALUES ({', '.join('?' * len(row))})",
            list(row.values()),
        )

    ins(
        "videos",
        dict(
            id=VIDEO_ID,
            url="https://www.youtube.com/watch?v=" + VIDEO_ID,
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
            id=SECOND_VIDEO_ID,
            url="https://www.youtube.com/watch?v=" + SECOND_VIDEO_ID,
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
        dict(id="mem_1", corpus_id="crp_1", video_id=VIDEO_ID, reason="author", status="ingested"),
    )
    ins(
        "corpus_members",
        dict(
            id="mem_2",
            corpus_id="crp_1",
            video_id=SECOND_VIDEO_ID,
            reason="discovered",
            status="queued",
        ),
    )
    ins(
        "fetch_attempts",
        dict(id="fa_1", video_id=VIDEO_ID, fetcher="supadata", outcome="ok", detail=""),
    )
    ins(
        "artifacts",
        dict(
            id="art_1",
            owner=VIDEO_ID,
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
            video_id=VIDEO_ID,
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
            video_id=VIDEO_ID,
            key="spk-1",
            kind="speaker",
            verdict="Ajahn X",
            role="host",
            source="phone page",
            applied_at="2026-05-02T09:01:00Z",
        ),
    )
    ins("speaker_verdicts", dict(id="sv_1", video_id=VIDEO_ID, role="host", name="Ajahn X"))
    ins(
        "corrections",
        dict(
            id="cor_1",
            created_at="2026-05-02T09:00:00Z",
            span_norm="darma",
            verdict="Dharma",
            kind="term",
            origin_video=VIDEO_ID,
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
            origin_video=VIDEO_ID,
            origin_key="amb-0002",
        ),
    )
    for index, start_seconds, end_seconds, text in [
        (0, 0.0, 3.0, "welcome everyone"),
        (1, 3.0, 7.5, "the noble eightfold path"),
    ]:
        connection.execute(
            "INSERT INTO segments (video_id, idx, t0, t1, speaker, text) VALUES (?,?,?,?,?,?)",
            (VIDEO_ID, index, start_seconds, end_seconds, "Host", text),
        )
    connection.commit()
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    connection.close()


def _rows(path: Path, sql: str):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


RV_SQL = "SELECT id, source_id, key, kind, verdict, created_at FROM review_verdicts ORDER BY id"
RV_SQL_V1 = RV_SQL.replace("source_id", "video_id")


@pytest.fixture
def migrated(dbpath):
    _build_v1(dbpath)
    original_rv = _rows(dbpath, RV_SQL_V1)
    database_handle = db.Database(dbpath)
    yield database_handle, Store(database_handle), original_rv
    database_handle.close()


def test_v1_upgrades_to_user_version(migrated, dbpath):
    # "upgrades an older database in place"
    database_handle, _, _ = migrated
    assert database_handle.one("PRAGMA user_version")[0] == db.USER_VERSION
    assert database_handle.one("PRAGMA integrity_check")[0] == "ok"
    assert database_handle.all("PRAGMA foreign_key_check") == []


def test_v1_backup_is_verified_and_complete(migrated, dbpath):
    # "first a backup ... to <workspace>/backups/<db file stem>-<UTC timestamp>-before-v4.sqlite ...
    #  verified with PRAGMA integrity_check and a row count of every owner table"
    _, _, original_rv = migrated
    backups_directory = dbpath.parent / db.BACKUPS_DIRNAME
    assert db.BACKUPS_DIRNAME == "backups"
    files = [
        path for path in backups_directory.iterdir() if path.is_file() and path.suffix == ".sqlite"
    ]
    assert len(files) == 1
    backup_path = files[0]
    assert backup_path.name.startswith("transcript-") and backup_path.name.endswith(
        "-before-v10.sqlite"
    )
    assert _rows(backup_path, "PRAGMA integrity_check")[0][0] == "ok"
    assert _rows(backup_path, RV_SQL_V1) == original_rv
    assert _rows(backup_path, "SELECT COUNT(*) FROM speaker_verdicts")[0][0] == 1
    assert _rows(backup_path, "SELECT COUNT(*) FROM corrections")[0][0] == 2
    assert _rows(backup_path, "PRAGMA user_version")[0][0] == 0  # the copy is from before


def test_v1_owner_decisions_survive(migrated):
    # every review verdict and correction survives with the same id, verdict, created_at
    _, store, _ = migrated
    verdicts = store.current_review_verdicts(VIDEO_ID)
    assert set(verdicts) == {"amb-0001", "spk-1"}
    assert (
        verdicts["amb-0001"]["id"],
        verdicts["amb-0001"]["verdict"],
        verdicts["amb-0001"]["created_at"],
    ) == (
        "rv_1",
        "Dharma",
        "2026-05-02T09:00:00Z",
    )
    assert (
        verdicts["spk-1"]["id"],
        verdicts["spk-1"]["verdict"],
        verdicts["spk-1"]["created_at"],
    ) == (
        "rv_2",
        "Ajahn X",
        "2026-05-02T09:01:00Z",
    )
    assert verdicts["spk-1"]["source"] == "phone page"
    assert verdicts["amb-0001"]["span"] == "darma"
    cors = store.current_corrections()
    assert {
        key: (row["id"], row["verdict"], row["kind"], row["created_at"])
        for key, row in cors.items()
    } == {
        "darma": ("cor_1", "Dharma", "term", "2026-05-02T09:00:00Z"),
        "sutra": ("cor_2", "sutta", "reading", "2026-05-02T09:02:00Z"),
    }
    assert store.correction_count() == 2
    assert store.current_speaker_names(VIDEO_ID) == {"host": "Ajahn X"}


def test_v1_migration_writes_no_change_log(migrated):
    # "creates triggers and views (so the copy writes no change-log rows)"
    database_handle, _, _ = migrated
    assert database_handle.one("SELECT COUNT(*) FROM change_log")[0] == 0


def test_v1_lists_read_back_through_store(migrated):
    _, store, _ = migrated
    source = store.source(VIDEO_ID)
    assert source["tags"] == ["zen", "breath"]
    assert source["topics"] == ["impermanence"]
    assert source["people"] == [{"name": "Ajahn X", "role": "host"}]
    assert source["title"] == "Old talk"
    assert source["duration_seconds"] == 3600
    corpus = store.corpus("crp_1")
    assert corpus["topic"] == ["meditation", "ethics"]
    assert corpus["perspective"] == ["Theravada"]


def test_v1_empty_perspective_reads_as_none(migrated):
    # "A v1 perspective of '' becomes NULL"
    _, store, _ = migrated
    assert store.source(VIDEO_ID)["perspective"] is None
    assert store.source(SECOND_VIDEO_ID)["perspective"] == "Theravada"


def test_v1_members_status_derived_and_search_works(migrated):
    _, store, _ = migrated
    by_vid = {member["id"]: member for member in store.member_sources("crp_1")}
    assert by_vid[VIDEO_ID]["member_status"] == "ingested"
    assert by_vid[SECOND_VIDEO_ID]["member_status"] == "queued"
    assert by_vid[SECOND_VIDEO_ID]["member_reason"] == "discovered"
    hits = store.search("eightfold", corpus_id=None, limit=10)
    assert len(hits) == 1
    assert hits[0]["source_id"] == VIDEO_ID
    assert hits[0]["start_seconds"] == 3.0
    assert hits[0]["position"] == 1
    assert len(store.search("eightfold", corpus_id="crp_1", limit=10)) == 1
    transcript_artifact = store.artifact(VIDEO_ID, "transcript")
    assert transcript_artifact is not None and transcript_artifact["sha256"] == HASH_A


def test_v1_reopen_is_a_noop(migrated, dbpath):
    database_handle, _, _ = migrated
    database_handle.close()
    reopened_database = db.Database(dbpath)
    try:
        assert reopened_database.one("PRAGMA user_version")[0] == db.USER_VERSION
        assert reopened_database.one("SELECT COUNT(*) FROM review_verdicts")[0] == 2
    finally:
        reopened_database.close()
    files = [path for path in (dbpath.parent / "backups").iterdir() if path.suffix == ".sqlite"]
    assert len(files) == 1  # no second backup for a current database


# 14. Newer database declined ------------------------------------------------


def test_newer_database_is_declined_untouched(dbpath):
    # "declines a database whose version is newer than this code (NewerDatabase; nothing is
    # touched)"
    database_handle = db.Database(dbpath)
    store = Store(database_handle)
    _video(store, title="Keep me")
    database_handle.close()
    connection = sqlite3.connect(dbpath)
    connection.execute(f"PRAGMA user_version = {db.USER_VERSION + 1}")
    connection.commit()
    dump_before = list(connection.iterdump())
    connection.close()
    with pytest.raises(db.NewerDatabase):
        db.Database(dbpath)
    connection = sqlite3.connect(dbpath)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.USER_VERSION + 1
        assert list(connection.iterdump()) == dump_before
    finally:
        connection.close()
    backups_directory = dbpath.parent / "backups"
    assert not backups_directory.exists() or not any(backups_directory.iterdir())


# 15. canonical_sha256 --------------------------------------------------------


def test_canonical_sha256_matches_hand_computation():
    # "SHA-256 of canonical JSON (keys sorted, no whitespace, UTF-8)"
    json_object = {
        "type": "ambiguity",
        "source_id": VIDEO_ID,
        "span": "dhárma é",
        "n": [1, 2, {"z": 1, "a": 2}],
    }
    reordered = {
        "span": "dhárma é",
        "n": [1, 2, {"a": 2, "z": 1}],
        "source_id": VIDEO_ID,
        "type": "ambiguity",
    }
    expected = hashlib.sha256(
        json.dumps(json_object, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    assert db.canonical_sha256(json_object) == expected
    assert db.canonical_sha256(reordered) == expected
    assert len(expected) == 64
    assert (
        db.canonical_sha256({"type": "speaker", "source_id": VIDEO_ID, "role": "host"}) != expected
    )


_json = strategies.recursive(
    strategies.none() | strategies.booleans() | strategies.integers() | strategies.text(max_size=8),
    lambda children: (
        strategies.lists(children, max_size=4)
        | strategies.dictionaries(strategies.text(max_size=6), children, max_size=4)
    ),
    max_leaves=12,
)


# A hash of a small JSON object takes microseconds; two seconds absorbs a
# loaded machine without
# hiding a hash that has really become slow.
HASH_DEADLINE = timedelta(seconds=2)


@settings(deadline=HASH_DEADLINE)
@given(
    json_object=strategies.dictionaries(strategies.text(max_size=6), _json, max_size=6),
    data=strategies.data(),
)
def test_canonical_sha256_ignores_key_order(json_object, data):
    """Property (written with the code; docs/design/storage-schema.md, "A
    verdict carries the content hash of what it judged"): the content hash
    of a JSON object does not depend on the order its keys were written in.
    (That ytlm's runner hash is the same function is tested in ytlm.)"""
    keys = data.draw(strategies.permutations(list(json_object)))
    shuffled = {key: json_object[key] for key in keys}
    assert db.canonical_sha256(shuffled) == db.canonical_sha256(json_object)
    assert re.fullmatch(r"[0-9a-f]{64}", db.canonical_sha256(json_object))


@settings(deadline=HASH_DEADLINE)
@given(first_value=_json, second_value=_json)
def test_different_json_values_get_different_hashes(first_value, second_value):
    if json.dumps(first_value, sort_keys=True) != json.dumps(second_value, sort_keys=True):
        assert db.canonical_sha256(first_value) != db.canonical_sha256(second_value)


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
        heading_match = re.match(r"^### `([a-z_]+)`( \(view\))?$", line)
        if heading_match:
            current = out[heading_match.group(1)] = {
                "view": bool(heading_match.group(2)),
                "history": None,
                "columns": [],
                "same_as": None,
            }
            continue
        if line.startswith("## "):
            current = None
        if current is None:
            continue
        history_match = re.match(r"^History: (.+)\.$", line)
        if history_match:
            current["history"] = history_match.group(1)
        columns_match = re.match(r"^Every column of `([a-z_]+)`", line)
        if columns_match:
            current["same_as"] = columns_match.group(1)
        if line.startswith("| ") and not line.startswith(("| column |", "|---")):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            current["columns"].append(tuple(cells))
    return out


def test_the_schema_document_mirrors_the_code():
    """coding.md, "Storage and migrations": the stored schema is defined in
    code; its doc mirrors it and a test fails if they differ."""
    connection = sqlite3.connect(":memory:")
    connection.executescript(db.SCHEMA)
    objects = {
        name: kind
        for name, kind in connection.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE '%segments_fts%'"
        )
    }
    document = _doc_sections()
    assert set(document) == set(objects)
    history = {table_name: "master" for table_name in db.MASTER_COLUMNS}
    history |= {table_name: "append-only" for table_name in db.APPEND_ONLY}
    history |= {
        table_name: f"list `{name}` of `{parent_name}`"
        for table_name, parent_name, name in (
            (table_name, "sources" if parent == "source_id" else "corpora", name)
            for table_name, parent, name, _ in db.LIST_TABLES
        )
    }
    history |= {
        "runs": "runs",
        "artifacts": "derived",
        "model_suggestions": "derived",
        "model_suggestion_candidates": "derived",
        "segments": "derived",
        "cleaned_segments": "derived",
        "claims": "derived",
        "locks": "coordination",
    }
    for name, kind in objects.items():
        section = document[name]
        assert section["view"] == (kind == "view"), name
        columns = connection.execute(f"PRAGMA table_xinfo({name})").fetchall()
        if section["same_as"]:
            same = [
                column[1]
                for column in connection.execute(f"PRAGMA table_xinfo({section['same_as']})")
            ]
            assert [column[1] for column in columns] == same, name
            continue
        if kind == "table":
            assert section["history"] == history[name], name
        want = []
        for _, column_name, column_type, notnull, _, primary_key, _ in columns:
            if (
                kind == "table"
                and column_name in BASE
                and section["columns"][:1] == [("(base)", "", "", "")]
            ):
                continue
            null = "" if kind == "view" else ("no" if notnull or primary_key else "yes")
            want.append((column_name, column_type, null))
        got = [
            (column_name, column_type, nullable)
            for column_name, column_type, nullable, _ in section["columns"]
            if column_name != "(base)"
        ]
        assert got == want, name
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()[0]
        checks = dict(re.findall(r"CHECK \((\w+) IN \(([^)]*)\)\)", sql))
        for column_name, _type, _null, meaning in section["columns"]:
            if kind == "table" and column_name in checks:
                for value in re.findall(r"'([^']*)'", checks[column_name]):
                    assert f"`{value}`" in meaning, (name, column_name, value)


def test_master_data_logs_every_column_but_the_identity_ones():
    """architecture.md, "History and identity": master data keeps a
    field-level change log by trigger; a column left out would change
    without a trace."""
    connection = sqlite3.connect(":memory:")
    connection.executescript(db.SCHEMA)
    for table, logged in db.MASTER_COLUMNS.items():
        columns = [column[1] for column in connection.execute(f"PRAGMA table_xinfo({table})")]
        expected = [column for column in columns if column not in BASE or column == "metadata"]
        if table == "corpus_members":
            expected = [column for column in expected if column not in ("corpus_id", "source_id")]
        if table == "review_answers":
            expected = [column for column in expected if column not in ("corpus_id", "card_id")]
        if table == "feeds":
            expected = [column for column in expected if column != "feed_id"]
        if table == "source_skips":
            expected = [column for column in expected if column != "source_id"]
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
        relative_path = path.relative_to(SRC).as_posix()
        if relative_path in ("db.py", "store.py") or relative_path in NOT_YET:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if SQL.search(line) or RAW_ACCESS.search(line):
                offenders.append(f"{relative_path}:{line_number}: {line.strip()}")
    assert offenders == []


def test_a_database_another_process_upgraded_meanwhile_is_left_to_it(tmp_path, monkeypatch):
    """coding.md, "Storage and migrations": every migration runs in one
    explicit transaction; the version is re-read once the write lock is held,
    so a second process never rebuilds an already upgraded database."""
    path = tmp_path / "transcript.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript((Path(__file__).parent / "fixtures" / "schema_v1.sql").read_text())
    connection.close()
    real_backup = db.backup
    others = []

    def backup_while_another_process_migrates(connection, database_path, *, label):
        saved = real_backup(connection, database_path, label=label)
        if not others:  # the other process opens and upgrades first
            others.append(database_path)
            db.Database(database_path).close()
        return saved

    monkeypatch.setattr(db, "backup", backup_while_another_process_migrates)
    first = sqlite3.connect(path)
    done = db.migrate(first, path)
    assert done["by"] == "another process"
    assert first.execute("PRAGMA user_version").fetchone()[0] == db.USER_VERSION
    assert first.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# 16. Backup retention: the newest three (owner ruling 2026-10-03) ----------


def _old_backups(dbpath, count: int) -> list[str]:
    backups_directory = dbpath.parent / db.BACKUPS_DIRNAME
    backups_directory.mkdir(exist_ok=True)
    names = [f"{dbpath.stem}-2026010{day}T000000Z-before-v4.sqlite" for day in range(1, count + 1)]
    for name in names:
        (backups_directory / name).write_bytes(b"old")
    (backups_directory / "notes.txt").write_text("the owner's")
    (backups_directory / "other-20260101T000000Z-x.sqlite").write_bytes(b"not ours")
    return names


def test_a_verified_backup_leaves_the_newest_three(dbpath):
    old = _old_backups(dbpath, 4)
    _build_v1(dbpath)
    database_handle = db.Database(dbpath)
    database_handle.close()
    assert sorted(database_handle.migration["backups_deleted"]) == old[:2]
    backups_directory = dbpath.parent / db.BACKUPS_DIRNAME
    ours = sorted(
        path.name for path in backups_directory.iterdir() if path.name.startswith(f"{dbpath.stem}-")
    )
    assert len(ours) == 3 and ours[:2] == old[2:]
    assert ours[2] == Path(database_handle.migration["backup"]).name
    assert (backups_directory / "notes.txt").exists() and (
        backups_directory / "other-20260101T000000Z-x.sqlite"
    ).exists()


def test_an_existing_database_gains_the_new_video_detail_unknown(tmp_path):
    """A v1 database made before the detail columns existed upgrades on open;
    the video's detail reads as unknown: no topics, no people, perspective
    NULL (coding.md, "Storage and migrations": NULL is the honest history;
    docs/design/storage-schema.md, "Migrations"). Moved from ytlm's
    tests/test_digest.py (nagarkar/ytlm#50)."""
    path = tmp_path / "transcript.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V1.read_text())
    for column in ("topics", "people", "perspective"):
        connection.execute(f"ALTER TABLE videos DROP COLUMN {column}")
    connection.execute(
        "INSERT INTO videos (id, created_at, created_by, schema_version, url) "
        "VALUES ('x', '2026-09-01T00:00:00Z', 'cli', '1', 'u')"
    )
    connection.commit()
    connection.close()
    video = Store(db.Database(path)).source("x")
    assert (video["topics"], video["people"], video["perspective"]) == ([], [], None)
