"""Review answers (db.py `review_answers`, Store.save_review_answer ...;
docs/design/storage-schema.md):
what the review page saved and `review apply` has not landed, and the v4 -> v5 migration."""

from __future__ import annotations

import sqlite3

import pytest

from corpuslm import db
from corpuslm.store import Store


@pytest.fixture
def database(tmp_path):
    database_handle = db.Database(tmp_path / "transcript.sqlite")
    yield database_handle
    database_handle.close()


@pytest.fixture
def corpus_id(database):
    return Store(database).add_corpus({"name": "Walks"}, created_by="test")


def test_an_answer_is_one_row_per_card_and_answering_again_replaces_it(database, corpus_id):
    store = Store(database)
    store.save_review_answer(corpus_id, "card-1", "Wren", "2026-10-09T10:00:00Z", created_by="page")
    store.save_review_answer(corpus_id, "card-1", "Ren", "2026-10-09T10:05:00Z", created_by="page")
    store.save_review_answer(corpus_id, "card-2", "skip", "2026-10-09T10:06:00Z", created_by="page")
    rows = store.review_answers(corpus_id)
    assert [(row["card_id"], row["verdict"], row["version"]) for row in rows] == [
        ("card-1", "Ren", 2),
        ("card-2", "skip", 1),
    ]
    assert store.review_answer_counts() == {corpus_id: 2}


def test_the_change_log_keeps_what_an_answer_was_before(database, corpus_id):
    store = Store(database)
    store.save_review_answer(corpus_id, "card-1", "Wren", "2026-10-09T10:00:00Z", created_by="page")
    store.save_review_answer(corpus_id, "card-1", "Ren", "2026-10-09T10:05:00Z", created_by="page")
    row = store.review_answer(corpus_id, "card-1")
    assert row is not None
    changes = {
        change["column_name"]: (change["old_value"], change["new_value"])
        for change in store.changes_of("review_answers", row["id"])
    }
    assert changes["verdict"] == ("Wren", "Ren")


def test_an_answer_for_no_corpus_is_a_value_error(database):
    with pytest.raises(ValueError, match="no such corpus"):
        Store(database).save_review_answer(
            "cor-nope", "c", "x", "2026-10-09T10:00:00Z", created_by="p"
        )


def test_an_empty_card_id_is_stopped_by_the_database(database, corpus_id):
    with pytest.raises(sqlite3.IntegrityError):
        Store(database).save_review_answer(
            corpus_id, "", "x", "2026-10-09T10:00:00Z", created_by="p"
        )


def test_deleting_an_answer_says_whether_there_was_one(database, corpus_id):
    store = Store(database)
    store.save_review_answer(corpus_id, "card-1", "x", "2026-10-09T10:00:00Z", created_by="p")
    assert store.delete_review_answer(corpus_id, "card-1") is True
    assert store.delete_review_answer(corpus_id, "card-1") is False
    assert store.review_answers(corpus_id) == []


def test_merging_a_corpus_carries_its_unlanded_answers_in_the_target_record(database):
    store = Store(database)
    into = store.add_corpus({"name": "Into"}, created_by="test")
    folded = store.add_corpus({"name": "Folded"}, created_by="test")
    store.save_review_answer(folded, "card-1", "Wren", "2026-10-09T10:00:00Z", created_by="p")
    store.merge_corpora(into, [folded], created_by="test")
    assert store.review_answer_counts() == {}
    kept = (store.corpus(into) or {})["metadata"]
    assert '"card_id": "card-1"' in kept


def _v4_database(path):
    """A database as the version before this one left it: no review answers table."""
    db.Database(path).close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER review_answers_change_log")
    connection.execute("DROP TABLE review_answers")
    connection.execute("DROP TABLE model_suggestion_candidates")
    connection.execute("DROP TABLE model_suggestions")
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()


def test_a_version_4_database_gains_the_table_after_a_verified_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    _v4_database(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO corpora (id, version, created_at, created_by, schema_version, name) "
        "VALUES ('cor-1', 1, 't', 'test', '4', 'Kept')"
    )
    connection.commit()
    connection.close()

    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 4 and opened.migration["to"] == db.USER_VERSION == 14
        backup = opened.migration["backup"]
        assert backup.endswith("-before-v14.sqlite")
        assert opened.one("PRAGMA user_version")[0] == db.USER_VERSION
        assert opened.one("SELECT COUNT(*) FROM model_suggestions")[0] == 0
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        assert opened.one("SELECT COUNT(*) FROM review_answers")[0] == 0
        assert opened.one("SELECT COUNT(*) FROM corpora")[0] == 1
        # the trigger came with it: a changed answer is logged
        store = Store(opened)
        store.save_review_answer("cor-1", "c", "a", "2026-10-09T10:00:00Z", created_by="p")
        store.save_review_answer("cor-1", "c", "b", "2026-10-09T10:01:00Z", created_by="p")
        row = store.review_answer("cor-1", "c")
        assert row is not None and store.changes_of("review_answers", row["id"])
    finally:
        opened.close()
    copy = sqlite3.connect(backup)
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 4
    assert copy.execute("SELECT COUNT(*) FROM corpora").fetchone()[0] == 1
    copy.close()
    again = db.Database(path)
    assert again.migration["backup"] is None  # opening a current database changes nothing
    again.close()
