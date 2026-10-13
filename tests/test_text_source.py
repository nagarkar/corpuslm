"""Text sources (`src/corpuslm/text_source.py`): the markdown parse, the import, a re-import that
leaves claims stale, the claim pass cutting by heading, and the v10 -> v11 step. Synthetic
text only."""

from __future__ import annotations

import sqlite3

import pytest

from corpuslm import claims, db, text_source
from corpuslm.store import Store

EXPORT = """---
title: front matter is skipped
---

# Seven Sermons

An opening paragraph before any section heading.

## The First Sermon

Time is the counting of beats.
A line that continues the same paragraph.

Rank is a state, not a possession.

### A deeper heading stays inside its section

A paragraph under the deeper heading.

## Perspective sheet

Stances the sheet states, which must never become claims.

### Vocabulary

beat: the atomic interval.

## The Second Sermon

Nothing is the ground.
"""


def test_parse_makes_paragraphs_sections_by_heading_and_leaves_the_sheet_out():
    parsed = text_source.parse(EXPORT)
    assert parsed.title == "Seven Sermons"
    assert [segment["text"].split("\n")[0] for segment in parsed.segments] == [
        "An opening paragraph before any section heading.",
        "Time is the counting of beats.",
        "Rank is a state, not a possession.",
        "A paragraph under the deeper heading.",
        "Nothing is the ground.",
    ]
    assert parsed.segments[1]["text"].count("\n") == 1  # the continued line stays in its paragraph
    assert [(s.heading, s.level, s.first, s.last) for s in parsed.sections] == [
        ("Seven Sermons", 1, 0, 0),
        ("The First Sermon", 2, 1, 3),
        ("The Second Sermon", 2, 4, 4),
    ]
    assert parsed.left_out == ("Perspective sheet",)
    assert parsed.words == 37
    assert [(s["idx"], s["t0"], s["t1"]) for s in parsed.segments][:2] == [
        (0, 0.0, 1.0),
        (1, 1.0, 2.0),
    ]


def test_parse_without_headings_makes_one_section_from_the_default_title():
    parsed = text_source.parse("One.\n\nTwo.\n", default_title="Notes")
    assert parsed.title == "Notes"
    assert [(s.heading, s.first, s.last) for s in parsed.sections] == [("Notes", 0, 1)]


def test_source_ids_are_slugs():
    assert text_source.source_id("SMSTTD") == "text:smsttd"
    assert text_source.source_id("Seven More Sermons, 2nd") == "text:seven-more-sermons-2nd"
    with pytest.raises(ValueError, match="no letters or digits"):
        text_source.source_id("---")


@pytest.fixture
def world(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    export = tmp_path / "Book.md"
    export.write_text(EXPORT)
    yield Store(database), export
    database.close()


def test_import_makes_the_source_with_its_segments_and_sections_and_a_dry_run_writes_nothing(
    world,
):
    store, export = world
    preview = text_source.import_text(store, export, name="SMSTTD", created_by="test", dry_run=True)
    assert preview == {
        "source": "text:smsttd",
        "created": True,
        "sections": 3,
        "paragraphs": 5,
        "words": 37,
        "left_out": ["Perspective sheet"],
        "claims_now_stale": 0,
    }
    assert not store.has_source("text:smsttd")
    out = text_source.import_text(store, export, name="SMSTTD", created_by="test", dry_run=False)
    assert out == preview
    row = store.source("text:smsttd")
    assert (row["kind"], row["status"], row["title"], row["locator"]) == (
        "text", "ingested", "Seven Sermons", str(export)
    )  # fmt: skip
    assert store.segment_count("text:smsttd") == 5
    assert [(s.heading, s.first, s.last) for s in text_source.sections_of(row)] == [
        ("Seven Sermons", 0, 0), ("The First Sermon", 1, 3), ("The Second Sermon", 4, 4)
    ]  # fmt: skip
    assert "text:smsttd" in store.source_ids("text")
    assert "text:smsttd" not in store.source_ids("youtube")


def test_a_reimport_replaces_the_segments_and_leaves_the_claims_stale(world):
    store, export = world
    text_source.import_text(store, export, name="SMSTTD", created_by="test", dry_run=False)
    config = {"claims": {"model": "gemini/gemini-2.5-flash-lite", "section_chars": 6000}}
    store.replace_claims(
        "text:smsttd",
        [{"section": 1, "segments": [1], "text": "Time counts beats.", "kind": "assertion"}],
        build_key="k",
        created_by="test",
    )
    store.record_artifact(
        "text:smsttd", claims.NODE, path="", sha256="k", state="built",
        inputs=claims.inputs(store, "text:smsttd", config), provenance={}, created_by="test",
    )  # fmt: skip
    (entry,) = claims.status(store, config, ["text:smsttd"])
    assert entry.state == "current"
    export.write_text(EXPORT + "\nA new closing paragraph.\n")
    out = text_source.import_text(
        store, export, name="SMSTTD", title="Seven", created_by="test", dry_run=False
    )
    assert (out["created"], out["paragraphs"], out["claims_now_stale"]) == (False, 6, 1)
    assert store.source("text:smsttd")["title"] == "Seven"
    assert store.segment_count("text:smsttd") == 6
    (entry,) = claims.status(store, config, ["text:smsttd"])
    assert (entry.state, entry.reason) == ("stale", "text changed")


def test_the_claim_pass_cuts_a_text_source_by_heading(world):
    store, export = world
    text_source.import_text(store, export, name="SMSTTD", created_by="test", dry_run=False)
    row = store.source("text:smsttd")
    parts = claims.sections_of(row, store.best_segments("text:smsttd"), 10)
    assert [[segment["idx"] for segment in part] for part in parts] == [[0], [1, 2, 3], [4]]
    assert len(claims.sections(store.best_segments("text:smsttd"), 10)) == 5  # by characters


def test_a_missing_or_empty_export_is_named(world, tmp_path):
    store, _ = world
    with pytest.raises(FileNotFoundError, match="does not exist"):
        text_source.import_text(store, tmp_path / "nope.md", name="x", created_by="t", dry_run=True)
    empty = tmp_path / "empty.md"
    empty.write_text("# Only a heading\n")
    with pytest.raises(ValueError, match="no paragraphs"):
        text_source.import_text(store, empty, name="x", created_by="t", dry_run=True)


# ---- the v10 -> v11 step ------------------------------------------------------------------


def _make_version_10(path):
    """Put a database back into its version 10 shape: the old `kind` CHECK, user_version 10."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    for kind in ("trigger", "view"):
        for (name,) in connection.execute(
            f"SELECT name FROM sqlite_master WHERE type='{kind}'"
        ).fetchall():
            connection.execute(f"DROP {kind.upper()} {name}")
    old = db.SOURCES_TABLE.replace(
        "CREATE TABLE IF NOT EXISTS sources (", "CREATE TABLE sources_v10 ("
    ).replace("'youtube', 'bee', 'text'", "'youtube', 'bee'")
    connection.execute(old)
    columns = ", ".join(row[1] for row in connection.execute("PRAGMA table_info(sources_v10)"))
    connection.execute(f"INSERT INTO sources_v10 ({columns}) SELECT {columns} FROM sources")
    connection.execute("DROP TABLE sources")
    connection.execute("ALTER TABLE sources_v10 RENAME TO sources")
    for statement in db._statements(db.SCHEMA):
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 10")
    connection.commit()
    connection.close()


def test_a_version_10_database_is_rebuilt_so_a_text_source_can_be_added(tmp_path):
    path = tmp_path / "transcript.sqlite"
    first = db.Database(path)
    Store(first).add_source("bee:1", "l", created_by="test", kind="bee")
    Store(first).replace_segments(
        "bee:1", [{"idx": 0, "t0": 0.0, "t1": 1.0, "speaker": "Quill", "text": "ember"}]
    )
    Store(first).replace_claims(
        "bee:1", [{"section": 0, "segments": [0], "text": "Embers.", "kind": "assertion"}],
        build_key="k", created_by="test",
    )  # fmt: skip
    first.close()
    _make_version_10(path)
    old = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        old.execute(
            "INSERT INTO sources (id, created_at, created_by, schema_version, kind, locator) "
            "VALUES ('text:x', 'now', 't', '2', 'text', 'l')"
        )
    old.close()
    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 10 and opened.migration["to"] == db.USER_VERSION == 15
        assert opened.migration["backup"].endswith("-before-v15.sqlite")
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        assert opened.all("PRAGMA foreign_key_check") == []
        store = Store(opened)
        assert store.claim_count("bee:1") == 1 and store.segment_count("bee:1") == 1
        assert store.add_source("text:x", "l", created_by="test", kind="text") is True
        assert store.search("ember", corpus_id=None, limit=5)  # the full-text triggers came back
        logged = opened.one("SELECT COUNT(*) AS n FROM change_log WHERE table_name = 'sources'")
        store.update_source("text:x", {"title": "X"})
        assert (
            opened.one("SELECT COUNT(*) AS n FROM change_log WHERE table_name = 'sources'")["n"]
            == logged["n"] + 1
        )  # the change-log trigger came back
    finally:
        opened.close()
