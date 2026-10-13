"""The `ignored` source status (docs/design/storage-schema.md: `sources.status`, "Migrations" v8
to v9): a source the owner set aside stays in the catalog with its files, but is excluded from
every read the owner works from. Sanitized synthetic text only."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from review_kinds import (
    BEE_KIND,
    BEE_SOURCE,
    SECOND_BEE_SOURCE,
    add_corpus_of_bee_sources,
    ambiguity,
    bee_speaker_map,
    bee_transcript,
    segment,
    write_source,
)

from corpuslm import (
    db,
    model_suggestions,
    review_accept_suggestions,
    review_apply,
    review_page,
)
from corpuslm.review_page import KindSetup
from corpuslm.store import Store

ANSWERED_AT = "2026-10-09T10:00:00Z"


@pytest.fixture
def database(tmp_path):
    database_handle = db.Database(tmp_path / "transcript.sqlite")
    yield database_handle
    database_handle.close()


@pytest.fixture
def store(database):
    return Store(database)


@pytest.fixture
def kinds(tmp_path):
    return {"bee": KindSetup(BEE_KIND, tmp_path / "transcripts")}


@pytest.fixture
def corpus(database, store, kinds):
    """Two Bee recordings in one corpus, both with the same open ambiguity and one segment of
    the word 'kiln'; neither ignored yet."""
    corpus_id = add_corpus_of_bee_sources(store, "Walks", [BEE_SOURCE, SECOND_BEE_SOURCE])
    for source_id in (BEE_SOURCE, SECOND_BEE_SOURCE):
        document = bee_transcript(ambiguities=[ambiguity("amb-0001", 1, "wren", "Wren", ["Wren"])])
        write_source(
            kinds["bee"].sources_directory,
            source_id,
            document
            | {
                "segments": [
                    segment(segment_index, segment_index * 5.0, "wren kiln", speaker="Unknown")
                    for segment_index in range(3)
                ]
            },
            bee_speaker_map(),
        )
        store.update_source(source_id, {"published_at": "2026-03-01T09:00:00.000-08:00"})
        store.replace_segments(
            source_id, [{"idx": 0, "t0": 0.0, "t1": 4.0, "speaker": "A", "text": "the kiln glows"}]
        )
        store.replace_cleaned_segments(
            source_id, [{"idx": 0, "t0": 0.0, "t1": 4.0, "speaker": "A", "text": "the kiln glows"}]
        )
        store.replace_model_suggestions(
            source_id,
            model_suggestions.suggestions_of([ambiguity("amb-0001", 1, "wren", "Wren", ["Wren"])]),
            artifact_sha256="b" * 64,
            model="model",
            created_by="test",
        )
    return corpus_id


def ignore(store, source_id):
    store.update_source(source_id, {"status": "ignored"})


def test_a_source_can_be_ignored_and_a_bogus_status_is_still_rejected(store, database):
    store.add_source("vidAAAAAAA1", "u", created_by="cli")
    ignore(store, "vidAAAAAAA1")
    assert store.source("vidAAAAAAA1")["status"] == "ignored"
    with pytest.raises(sqlite3.IntegrityError):
        database.update("sources", "vidAAAAAAA1", {"status": "bogus"})


def test_an_ignored_member_is_counted_apart_and_left_out_of_the_member_total(store, corpus):
    ignore(store, SECOND_BEE_SOURCE)
    assert store.member_status_counts(corpus) == {"queued": 1, "ignored": 1}
    summary = next(
        summary_row for summary_row in store.corpus_summaries() if summary_row["id"] == corpus
    )
    assert (summary["members"], summary["ingested"], summary["ignored"]) == (1, 0, 1)


def test_the_member_lists_leave_an_ignored_source_out_and_the_catalog_keeps_it(store, corpus):
    ignore(store, SECOND_BEE_SOURCE)
    assert [member["id"] for member in store.member_sources(corpus)] == [BEE_SOURCE]
    assert store.member_source_ids(corpus, include_ignored=False) == [BEE_SOURCE]
    assert store.member_source_ids(corpus) == [BEE_SOURCE, SECOND_BEE_SOURCE]
    assert store.source_ids("bee", include_ignored=False) == [BEE_SOURCE]
    assert store.source_ids("bee") == [BEE_SOURCE, SECOND_BEE_SOURCE]
    assert store.has_source(SECOND_BEE_SOURCE)  # still in the catalog: a scan never offers it
    store.update_source(BEE_SOURCE, {"status": "ingested"})
    ignore(store, BEE_SOURCE)
    assert store.ingested_member_rows(corpus) == []


def test_an_ignored_source_has_no_search_hits_and_keeps_its_segments(store, corpus):
    assert len(store.search("kiln", corpus_id=corpus, limit=10)) == 2
    assert len(store.search_cleaned("kiln", limit=10)) == 2
    ignore(store, SECOND_BEE_SOURCE)
    assert {hit["source_id"] for hit in store.search("kiln", corpus_id=corpus, limit=10)} == {
        BEE_SOURCE
    }
    assert {hit["source_id"] for hit in store.search_cleaned("kiln", limit=10)} == {BEE_SOURCE}
    assert store.segment_count(SECOND_BEE_SOURCE) == 1  # kept
    store.update_source(SECOND_BEE_SOURCE, {"status": "added"})
    assert len(store.search("kiln", corpus_id=corpus, limit=10)) == 2


def test_the_review_page_has_no_cards_or_open_items_for_an_ignored_source(
    database, store, kinds, corpus
):
    data = review_page.page_data_by_kind(database, kinds, "Walks")
    assert data["open_items"] == 4  # two ambiguities and two speakers
    ignore(store, SECOND_BEE_SOURCE)
    data = review_page.page_data_by_kind(database, kinds, "Walks")
    assert data["open_items"] == 2
    seen = {item["video_id"] for card in data["cards"] for item in (card.get("items") or [card])}
    assert SECOND_BEE_SOURCE not in {
        segment_index for segment_index in seen if isinstance(segment_index, str)
    }
    ignore(store, BEE_SOURCE)
    data = review_page.page_data_by_kind(database, kinds, "Walks")
    assert (data["open_items"], data["cards"]) == (0, [])


def test_apply_lands_nothing_for_a_card_whose_only_sources_are_ignored(
    database, store, kinds, corpus
):
    card = next(
        card
        for card in review_page.page_data_by_kind(database, kinds, "Walks")["cards"]
        if card["kind"] == "ambiguity"
    )
    store.save_review_answer(corpus, card["id"], "Wren", ANSWERED_AT, created_by="test")
    ignore(store, BEE_SOURCE)
    ignore(store, SECOND_BEE_SOURCE)
    report = review_apply.apply(database, kinds, created_by="test", dry_run=True)
    assert report.verdicts_to_land == 0
    assert store.current_review_verdicts(BEE_SOURCE) == {}


def test_accept_suggestions_answers_no_card_of_an_ignored_source(database, store, kinds, corpus):
    ignore(store, BEE_SOURCE)
    ignore(store, SECOND_BEE_SOURCE)
    report = review_accept_suggestions.accept(
        database, kinds, kind="bee", before=date(2026, 11, 1), dry_run=True
    )
    assert report.cards_considered == 0 and report.cards_to_answer == 0


def test_the_suggestion_backfill_reads_no_ignored_source(store, kinds, corpus):
    before = model_suggestions.backfill(store, kinds, created_by="test", dry_run=True)
    ignore(store, SECOND_BEE_SOURCE)
    after = model_suggestions.backfill(store, kinds, created_by="test", dry_run=True)
    assert before.sources_checked == 2 and after.sources_checked == 1


# ---- the v8 -> v9 migration --------------------------------------------------------------------


def _make_version_8(path):
    """Put a database back into its version 8 shape: the old `status` CHECK, `kind` as the last
    column (as an upgraded database has it), user_version 8."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ).fetchall():
        connection.execute(f"DROP TRIGGER {name}")
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
    ).fetchall():
        connection.execute(f"DROP VIEW {name}")
    kind_line = (
        "    kind TEXT NOT NULL DEFAULT 'youtube' CHECK (kind IN ('youtube', 'bee', 'text')),\n"
    )
    old = (
        db.SOURCES_TABLE.replace(
            "CREATE TABLE IF NOT EXISTS sources (", "CREATE TABLE sources_v8 ("
        )
        .replace(", 'ignored'", "")
        .replace(kind_line, "")
        .replace(
            "perspective TEXT\n) STRICT",
            "perspective TEXT,\n    " + kind_line.strip().rstrip(",") + "\n) STRICT",
        )
    )
    connection.execute(old)
    columns = ", ".join(
        column_row[1] for column_row in connection.execute("PRAGMA table_info(sources_v8)")
    )
    connection.execute(f"INSERT INTO sources_v8 ({columns}) SELECT {columns} FROM sources")
    connection.execute("DROP TABLE sources")
    connection.execute("ALTER TABLE sources_v8 RENAME TO sources")
    for statement in db._statements(db.SCHEMA):  # the triggers and views, as a v8 file has them
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 8")
    connection.commit()
    connection.close()


def _everything(path):
    connection = sqlite3.connect(path)
    connection.row_factory = (
        sqlite3.Row
    )  # rows by column name: the rebuild changes the column order
    try:
        tables = sorted(
            table_row[0]
            for table_row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        return {
            table: sorted(
                repr(sorted(dict(row).items()))
                for row in connection.execute(f'SELECT * FROM "{table}"')
            )
            for table in tables
        }
    finally:
        connection.close()


def test_a_version_8_database_is_rebuilt_with_every_row_and_the_author_tables_unchanged(
    tmp_path, database, store, corpus
):
    path = tmp_path / "transcript.sqlite"
    store.append_review_verdict(
        source_id=BEE_SOURCE,
        key="amb-0001",
        kind="ambiguity",
        verdict="Wren",
        span="wren",
        role="",
        answered_through="phone page",
        subject_sha256="c" * 64,
        created_by="test",
    )
    store.save_review_answer(corpus, "card-x", "Wren", ANSWERED_AT, created_by="test")
    store.update_source(BEE_SOURCE, {"tags": ["walk"], "title": "A walk"})
    database.close()
    _make_version_8(path)
    old = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        old.execute("UPDATE sources SET status = 'ignored'")
    old.close()
    before = _everything(path)

    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 8 and opened.migration["to"] == db.USER_VERSION == 14
        assert opened.migration["backup"].endswith("-before-v14.sqlite")
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        assert opened.all("PRAGMA foreign_key_check") == []
        after = _everything(path)
        assert after == before  # every row of every table, the author tables included
        fresh = sqlite3.connect(":memory:")
        fresh.executescript(db.SCHEMA)
        assert [column_row[1] for column_row in opened.all("PRAGMA table_info(sources)")] == [
            row[1] for row in fresh.execute("PRAGMA table_info(sources)")
        ]
        # the rebuilt table, its triggers and its views work
        again = Store(opened)
        again.update_source(SECOND_BEE_SOURCE, {"status": "ignored"})
        assert again.member_status_counts(corpus) == {"queued": 1, "ignored": 1}
        logged = opened.one(
            "SELECT COUNT(*) AS n FROM change_log "
            "WHERE table_name = 'sources' AND column_name = 'status'"
        )
        assert logged["n"] == 1  # the sources trigger came back
        assert again.search("kiln", corpus_id=corpus, limit=5)[0]["source_id"] == BEE_SOURCE
        again.replace_segments(BEE_SOURCE, [{"idx": 0, "t0": 0.0, "t1": 1.0, "text": "ember"}])
        assert again.search("ember", corpus_id=corpus, limit=5)  # the full-text triggers came back
    finally:
        opened.close()
    copy = sqlite3.connect(opened.migration["backup"])
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 8
    with pytest.raises(sqlite3.IntegrityError):
        copy.execute("UPDATE sources SET status = 'ignored'")
    copy.close()


def test_a_failed_rebuild_rolls_back_and_names_the_backup(
    tmp_path, database, store, corpus, monkeypatch
):
    path = tmp_path / "transcript.sqlite"
    database.close()
    _make_version_8(path)
    before = _everything(path)
    real = db._v8_to_v9_script

    def broken(connection):
        return [*real(connection), "INSERT INTO sources (id) VALUES ('x');"]  # violates NOT NULL

    monkeypatch.setattr(db, "_v8_to_v9_script", broken)
    with pytest.raises(sqlite3.IntegrityError):
        db.Database(path)
    monkeypatch.undo()
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
    connection.close()
    assert _everything(path) == before
