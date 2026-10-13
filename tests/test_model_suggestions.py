"""Model suggestions (corpuslm.model_suggestions): the AI cleanup's top suggestion for every
ambiguity, kept in the database, recorded from the artifact files with no model call. Sanitized
synthetic text only."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from review_kinds import (
    BEE_KIND,
    BEE_SOURCE,
    SECOND_BEE_SOURCE,
    YOUTUBE_SOURCE,
    add_youtube_source,
    ambiguity,
    bee_transcript,
    youtube_transcript,
)

from corpuslm import cli, db, model_suggestions, review_kinds
from corpuslm.review_page import KindSetup
from corpuslm.store import Store


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
    return {
        "youtube": KindSetup(review_kinds.youtube_review(), tmp_path / "videos"),
        "bee": KindSetup(BEE_KIND, tmp_path / "transcripts"),
    }


def put_ai_cleanup(store, kinds, source_id, document, *, state="built"):
    """Write a source's AI cleanup file and record the artifact the way the builder does."""
    kind = store.source(source_id)["kind"]
    node = kinds[kind].kind.ai_cleanup_node
    name = "transcript.pass3.json" if kind == "youtube" else "model_cleanup.json"
    folder = kinds[kind].sources_directory / source_id
    folder.mkdir(parents=True, exist_ok=True)
    data = json.dumps(document).encode("utf-8")
    (folder / name).write_bytes(data)
    store.record_artifact(
        source_id,
        node,
        path=f"{folder.name}/{source_id}/{name}",
        sha256=hashlib.sha256(data).hexdigest(),
        state=state,
        inputs={},
        provenance={},
        created_by="test",
    )
    return hashlib.sha256(data).hexdigest()


def add_bee(store, source_id):
    store.add_source(source_id, f"transcripts/{source_id}.md", created_by="test", kind="bee")


def with_ambiguities(document, *items):
    return document | {"ambiguities": list(items)}


def test_the_suggestion_is_the_models_pick_not_the_resolution(store):
    add_bee(store, BEE_SOURCE)
    resolved = ambiguity(
        "amb-0001",
        1,
        "Wren!",
        "Wren",
        ["Wren", "Ren"],
        status="resolved",
        resolution="Ren",
        probabilities=[0.7, 0.3],
    )
    rows = model_suggestions.suggestions_of([resolved])
    assert rows[0]["suggestion"] == "Wren" and rows[0]["span_norm"] == "wren"
    assert rows[0]["suggestion_probability"] == 0.7


def test_a_reply_without_probabilities_leaves_them_empty(store):
    first_prompt = ambiguity("amb-0001", 1, "wren", "Wren", ["Wren", "Ren"])
    row = model_suggestions.suggestions_of([first_prompt])[0]
    assert row["suggestion_probability"] is None and row["probabilities"] == [None, None]
    odd = ambiguity("amb-0002", 1, "wren", "Wrenn", ["Wren", "Ren"], probabilities=[0.5, 0.5])
    assert model_suggestions.suggestions_of([odd])[0]["suggestion_probability"] is None


def test_the_store_keeps_candidates_in_order_and_refuses_an_unknown_source(store):
    add_bee(store, BEE_SOURCE)
    rows = model_suggestions.suggestions_of(
        [
            ambiguity(
                "amb-0001",
                1,
                "wren",
                "Ren",
                ["Wren", "Ren", "Wrenn"],
                probabilities=[0.25, 0.5, 0.25],
            )
        ]
    )
    store.replace_model_suggestions(
        BEE_SOURCE, rows, artifact_sha256="a" * 64, model="m", created_by="test"
    )
    held = store.model_suggestions(BEE_SOURCE)["amb-0001"]
    assert held["candidates"] == ["Wren", "Ren", "Wrenn"] and held["probabilities"] == [
        0.25,
        0.5,
        0.25,
    ]
    assert store.model_suggestion_counts() == {
        "sources": 1,
        "suggestions": 1,
        "with_probability": 1,
    }
    with pytest.raises(KeyError):
        store.replace_model_suggestions(
            "bee:none", rows, artifact_sha256="a" * 64, model="m", created_by="test"
        )


def test_a_rebuilt_artifact_replaces_its_rows_and_a_second_backfill_writes_nothing(store, kinds):
    add_bee(store, BEE_SOURCE)
    add_youtube_source(store, store.add_corpus({"name": "C"}, created_by="test"))
    put_ai_cleanup(store, kinds, BEE_SOURCE, bee_transcript())
    put_ai_cleanup(store, kinds, YOUTUBE_SOURCE, youtube_transcript())
    first = model_suggestions.backfill(store, kinds, created_by="test")
    assert (first.sources_checked, first.sources_recorded, first.suggestions_recorded) == (2, 2, 2)
    assert store.model_suggestion_counts()["suggestions"] == 2
    second = model_suggestions.backfill(store, kinds, created_by="test")
    assert (second.sources_already_recorded, second.sources_recorded) == (2, 0)
    assert second.suggestions_recorded == 0
    # the artifact is rebuilt with one more ambiguity: its rows are replaced, the other's kept
    rebuilt = bee_transcript(
        ambiguities=[
            ambiguity("amb-0001", 1, "wren", "Wren", ["Wren", "Ren"], probabilities=[0.9, 0.1]),
            ambiguity("amb-0002", 2, "kiln", "kiln", ["kiln", "kill"]),
        ]
    )
    sha256 = put_ai_cleanup(store, kinds, BEE_SOURCE, rebuilt)
    third = model_suggestions.backfill(store, kinds, created_by="test")
    assert (third.sources_recorded, third.sources_already_recorded) == (1, 1)
    held = store.model_suggestions(BEE_SOURCE)
    assert sorted(held) == ["amb-0001", "amb-0002"]
    assert {held_row["artifact_sha256"] for held_row in held.values()} == {sha256}
    assert held["amb-0001"]["suggestion_probability"] == 0.9
    assert store.model_suggestion_counts() == {
        "sources": 2,
        "suggestions": 3,
        "with_probability": 1,
    }


def test_a_file_changed_since_it_was_built_is_counted_and_not_read(store, kinds):
    add_bee(store, BEE_SOURCE)
    add_bee(store, SECOND_BEE_SOURCE)
    put_ai_cleanup(store, kinds, BEE_SOURCE, bee_transcript())
    other = bee_transcript() | {"video_id": SECOND_BEE_SOURCE}
    put_ai_cleanup(store, kinds, SECOND_BEE_SOURCE, other)
    (kinds["bee"].sources_directory / BEE_SOURCE / "model_cleanup.json").write_text("{}")
    report = model_suggestions.backfill(store, kinds, created_by="test")
    assert report.sources_file_changed_since_built == 1 and report.sources_recorded == 1
    assert store.model_suggestions(BEE_SOURCE) == {}


def test_a_source_without_a_built_artifact_or_file_is_counted(store, kinds):
    add_bee(store, BEE_SOURCE)
    add_bee(store, SECOND_BEE_SOURCE)
    put_ai_cleanup(store, kinds, SECOND_BEE_SOURCE, bee_transcript(), state="failed")
    (kinds["bee"].sources_directory / SECOND_BEE_SOURCE / "model_cleanup.json").unlink()
    report = model_suggestions.backfill(store, kinds, created_by="test")
    assert report.sources_checked == 2 and report.sources_without_ai_cleanup == 2
    assert report.sources_recorded == 0


def test_a_dry_run_counts_and_writes_nothing(store, kinds):
    add_bee(store, BEE_SOURCE)
    put_ai_cleanup(store, kinds, BEE_SOURCE, bee_transcript())
    report = model_suggestions.backfill(store, kinds, created_by="test", dry_run=True)
    assert report.sources_to_record == 1 and report.suggestions_to_record == 1
    assert report.sources_recorded == 0 and report.suggestions_recorded == 0
    assert store.model_suggestion_counts()["suggestions"] == 0


def test_an_artifact_with_no_ambiguities_clears_the_rows_of_the_one_before(store, kinds):
    add_bee(store, BEE_SOURCE)
    put_ai_cleanup(store, kinds, BEE_SOURCE, bee_transcript())
    model_suggestions.backfill(store, kinds, created_by="test")
    put_ai_cleanup(store, kinds, BEE_SOURCE, with_ambiguities(bee_transcript()))
    model_suggestions.backfill(store, kinds, created_by="test")
    assert store.model_suggestions(BEE_SOURCE) == {}


def test_a_version_5_database_gains_the_tables_after_a_verified_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    db.Database(path).close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE model_suggestion_candidates")
    connection.execute("DROP TABLE model_suggestions")
    connection.execute("PRAGMA user_version = 5")
    connection.execute(
        "INSERT INTO corpora (id, version, created_at, created_by, schema_version, name) "
        "VALUES ('cor-1', 1, 't', 'test', '5', 'Kept')"
    )
    connection.commit()
    connection.close()
    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 5 and opened.migration["to"] == db.USER_VERSION == 10
        assert opened.migration["backup"].endswith("-before-v10.sqlite")
        assert opened.one("PRAGMA user_version")[0] == db.USER_VERSION
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        assert opened.one("SELECT COUNT(*) FROM model_suggestions")[0] == 0
        assert opened.one("SELECT COUNT(*) FROM corpora")[0] == 1
    finally:
        opened.close()
    copy = sqlite3.connect(opened.migration["backup"])
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 5
    copy.close()


@pytest.fixture
def config(tmp_path, store, kinds, database):
    kinds["bee"] = KindSetup(review_kinds.bee_review("Quill"), tmp_path / "transcripts")
    add_bee(store, "bee:101")
    put_ai_cleanup(store, kinds, "bee:101", bee_transcript() | {"video_id": "bee:101"})
    database.close()
    path = tmp_path / "review.toml"
    path.write_text(
        f'database = "{tmp_path / "transcript.sqlite"}"\n'
        f'[kinds.youtube]\ndirectory = "{tmp_path / "videos"}"\n'
        f'[kinds.bee]\ndirectory = "{tmp_path / "transcripts"}"\nowner_first_name = "Quill"\n'
    )
    return path


def test_the_verb_prints_counts_only_and_a_second_run_records_nothing(config, capsys):
    assert cli.main(["suggestions", "record", "--config", str(config), "--json"]) == 0
    board = json.loads(capsys.readouterr().out)
    assert board["sources_recorded"] == 1 and board["database_suggestions"] == 1
    assert all(
        isinstance(value, int)
        for key, value in board.items()
        if key.startswith(("sources", "sugg"))
    )
    assert "Wren" not in json.dumps(board) and "wren" not in json.dumps(board)
    assert cli.main(["suggestions", "record", "--config", str(config), "--json"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["sources_recorded"] == 0 and again["sources_already_recorded"] == 1
    assert again["database_suggestions"] == 1


def test_a_source_whose_artifact_cannot_be_recorded_is_counted_and_the_rest_go_on(store, kinds):
    add_bee(store, BEE_SOURCE)
    add_bee(store, SECOND_BEE_SOURCE)
    bad = ambiguity("amb-0001", 1, "wren", "Wren", ["Wren", "Ren"], probabilities=[3, 1])
    put_ai_cleanup(store, kinds, BEE_SOURCE, with_ambiguities(bee_transcript(), bad))
    put_ai_cleanup(store, kinds, SECOND_BEE_SOURCE, bee_transcript())
    report = model_suggestions.backfill(store, kinds, created_by="test")
    assert report.sources_unreadable == 1 and report.sources_recorded == 1
    assert store.model_suggestions(BEE_SOURCE) == {}
