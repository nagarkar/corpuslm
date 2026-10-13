"""Cleaned segments (schema 8): the best transcript of a source whose raw lines stay in
`segments`, with its own full-text index, searched as references only. Sanitized synthetic text."""

from __future__ import annotations

import sqlite3

import pytest
from test_chain import SEGMENTS, SOURCE, FakeKind

from corpuslm import chain, db
from corpuslm.speaker_map import bee_scheme
from corpuslm.store import Store

CLEANED = [
    {"idx": 0, "t0": 0.0, "t1": 4.0, "speaker": "Quill", "text": "we talked about kiln glaze"},
    {"idx": 1, "t0": 4.0, "t1": 9.5, "speaker": "Fern", "text": "the kiln was too hot"},
    {"idx": 2, "t0": 9.5, "t1": 12.0, "speaker": "Quill", "text": "next week we fire again"},
]


@pytest.fixture
def store(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    repository = Store(database)
    for source, day in (
        ("bee:1", "2026-10-01T09:00:00-07:00"),
        ("bee:2", "2026-10-05T09:00:00-07:00"),
    ):
        repository.add_source(source, f"{source}.json", created_by="test", kind="bee")
        repository.update_source(
            source, {"title": f"Title {source}", "published_at": day}, created_by="test"
        )
        repository.replace_segments(
            source, [{**segment, "text": "raw words only"} for segment in CLEANED]
        )
    yield repository
    database.close()


def test_the_cleaned_rows_have_their_own_search_and_leave_the_raw_segments_alone(store):
    store.replace_cleaned_segments("bee:1", CLEANED)
    store.replace_cleaned_segments("bee:2", CLEANED[:2])
    assert store.cleaned_segment_count() == 5 and store.cleaned_segment_count("bee:2") == 2
    assert store.segment_count() == 6 and store.segments("bee:1")[0]["text"] == "raw words only"
    hits = store.search_cleaned("kiln", limit=10)
    assert {(hit["source_id"], hit["start_seconds"]) for hit in hits} == {
        ("bee:1", 0.0),
        ("bee:1", 4.0),
        ("bee:2", 0.0),
        ("bee:2", 4.0),
    }
    # the raw index never sees the cleaned text, and the cleaned index never the raw text
    assert store.search("kiln", corpus_id=None, limit=10) == []
    assert store.search_cleaned("raw", limit=10) == []


def test_a_hit_is_a_reference_and_never_carries_the_text(store):
    store.replace_cleaned_segments("bee:1", CLEANED)
    (hit,) = store.search_cleaned("glaze", limit=5)
    assert set(hit) == {"source_id", "title", "published_at", "start_seconds", "rank"}
    assert hit["title"] == "Title bee:1" and hit["start_seconds"] == 0.0
    assert not any("glaze" in str(value) for value in hit.values())


def test_before_keeps_only_sources_recorded_on_an_earlier_day_and_limit_caps(store):
    store.replace_cleaned_segments("bee:1", CLEANED)
    store.replace_cleaned_segments("bee:2", CLEANED)
    early = store.search_cleaned("kiln", before="2026-10-05", limit=10)
    assert {hit["source_id"] for hit in early} == {"bee:1"}
    assert store.search_cleaned("kiln", before="2026-10-01", limit=10) == []
    assert len(store.search_cleaned("kiln", limit=3)) == 3


def test_a_rebuild_replaces_the_rows_and_the_index_follows(store):
    store.replace_cleaned_segments("bee:1", CLEANED)
    store.replace_cleaned_segments("bee:1", [{**CLEANED[0], "text": "we talked about pottery"}])
    assert store.cleaned_segment_count("bee:1") == 1
    assert store.search_cleaned("glaze", limit=5) == []
    assert len(store.search_cleaned("pottery", limit=5)) == 1
    assert store.delete_cleaned_segments("bee:1") == 1
    assert store.search_cleaned("pottery", limit=5) == [] and store.cleaned_source_ids() == []


def test_a_query_that_is_not_fts5_syntax_raises(store):
    with pytest.raises(sqlite3.OperationalError):
        store.search_cleaned('kiln "', limit=5)


def test_a_version_7_database_gains_the_cleaned_tables_after_a_verified_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    first = db.Database(path)
    Store(first).add_source("bee:1", "l", created_by="test", kind="bee")
    Store(first).replace_segments("bee:1", SEGMENTS)
    first.close()
    connection = sqlite3.connect(path)
    for statement in (
        "DROP TRIGGER cleaned_segments_ai",
        "DROP TRIGGER cleaned_segments_ad",
        "DROP TRIGGER cleaned_segments_au",
        "DROP TABLE cleaned_segments_fts",
        "DROP TABLE cleaned_segments",
        "PRAGMA user_version = 7",
    ):
        connection.execute(statement)
    connection.commit()
    connection.close()
    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 7 and opened.migration["to"] == db.USER_VERSION == 14
        assert opened.migration["backup"].endswith("-before-v14.sqlite")
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        assert opened.one("SELECT COUNT(*) FROM cleaned_segments")[0] == 0
        assert opened.one("SELECT COUNT(*) FROM segments")[0] == len(SEGMENTS)
        store = Store(opened)
        store.replace_cleaned_segments("bee:1", CLEANED)  # the triggers came with it
        assert len(store.search_cleaned("kiln", limit=5)) == 2
    finally:
        opened.close()
    copy = sqlite3.connect(opened.migration["backup"])
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 7
    copy.close()


def test_a_cleaned_search_table_is_filled_and_the_raw_segments_are_not_touched(store, tmp_path):
    store.add_source(SOURCE, "any", created_by="test", kind="bee")
    store.replace_segments(SOURCE, SEGMENTS)
    shape = chain.ChainShape(search_table="cleaned_segments")
    chain_instance = chain.Chain(store, FakeKind(tmp_path / "t", shape=shape), tmp_path, "test")
    document = {"state": "LLM-CLEANED", "segments": CLEANED}
    assert chain.build_search_index(chain_instance, SOURCE, document, "sha-1")
    assert store.cleaned_segment_count(SOURCE) == 3
    assert [segment["text"] for segment in store.segments(SOURCE)] == [
        segment["text"] for segment in SEGMENTS
    ]
    assert not chain.build_search_index(
        chain_instance, SOURCE, document, "sha-1"
    )  # unchanged: nothing built
    assert chain.search_index_current(chain_instance, SOURCE, document, "sha-1")
    assert not chain.search_index_current(chain_instance, SOURCE, document, "sha-2")
    store.delete_cleaned_segments(SOURCE)  # the rows are gone: the index is rebuilt, not trusted
    assert not chain.search_index_current(chain_instance, SOURCE, document, "sha-1")
    assert chain.build_search_index(chain_instance, SOURCE, document, "sha-1")
    assert chain.build_search_index(
        chain_instance, SOURCE, {**document, "segments": CLEANED[:1]}, "sha-2"
    )
    assert store.cleaned_segment_count(SOURCE) == 1
    # a tool that names nothing keeps writing `segments`
    plain = chain.Chain(store, FakeKind(tmp_path / "t"), tmp_path, "test")
    assert chain.build_search_index(plain, SOURCE, document, "sha-3")
    assert store.segment_count(SOURCE) == 3
    with pytest.raises(ValueError, match="search_table"):
        chain.ChainShape(search_table="elsewhere")


def test_the_best_transcript_is_read_without_writing_a_file(store, tmp_path):
    chain_instance = chain.Chain(store, FakeKind(tmp_path / "t"), tmp_path, "test")
    store.add_source(SOURCE, "any", created_by="test", kind="bee")
    first = chain.build_normalize(chain_instance, SOURCE)
    document = chain.best_transcript(
        chain_instance, SOURCE, None, preference=("normalize",), scheme=bee_scheme("Quill")
    )
    assert document["segments"] and not (tmp_path / "t" / SOURCE / "transcript.json").exists()
    published, sha256 = chain.publish_best(
        chain_instance, SOURCE, None, preference=("normalize",), scheme=bee_scheme("Quill")
    )
    assert published == document and sha256 == chain.transcript_sha256(chain_instance, document)
    assert first.built
