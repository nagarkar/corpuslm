"""Source kinds `vimeo` and `file` (nagarkar/corpuslm#84): the v16 -> v17 step widens the
`sources` checks, and the surfaces read a source's link from its `locator` instead of building a
watch URL. Synthetic rows only."""

from __future__ import annotations

import sqlite3

import pytest

from corpuslm import db, monologue_page, review_kinds
from corpuslm.store import Store

OLD_KINDS = "'youtube', 'bee', 'text'"
NEW_KINDS = "'youtube', 'bee', 'text', 'vimeo', 'file'"
OLD_METADATA_SOURCES = "'', 'dataapi', 'oembed'"
NEW_METADATA_SOURCES = "'', 'dataapi', 'oembed', 'vimeo', 'ffprobe'"


def _make_version_16(path):
    """Put a database back into its version 16 shape: the old `kind` and `metadata_source`
    checks, user_version 16."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    for kind in ("trigger", "view"):
        for (name,) in connection.execute(
            f"SELECT name FROM sqlite_master WHERE type='{kind}'"
        ).fetchall():
            connection.execute(f"DROP {kind.upper()} {name}")
    old = (
        db.SOURCES_TABLE.replace(
            "CREATE TABLE IF NOT EXISTS sources (", "CREATE TABLE sources_v16 ("
        )
        .replace(NEW_KINDS, OLD_KINDS)
        .replace(NEW_METADATA_SOURCES, OLD_METADATA_SOURCES)
    )
    assert OLD_KINDS in old and OLD_METADATA_SOURCES in old
    connection.execute(old)
    columns = ", ".join(row[1] for row in connection.execute("PRAGMA table_info(sources_v16)"))
    connection.execute(f"INSERT INTO sources_v16 ({columns}) SELECT {columns} FROM sources")
    connection.execute("DROP TABLE sources")
    connection.execute("ALTER TABLE sources_v16 RENAME TO sources")
    for statement in db._statements(db.SCHEMA):
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 16")
    connection.commit()
    connection.close()


def test_a_version_16_database_is_rebuilt_so_vimeo_and_file_sources_can_be_added(tmp_path):
    path = tmp_path / "transcript.sqlite"
    first = db.Database(path)
    store = Store(first)
    store.add_source("aaaaaaaaaaa", "https://www.youtube.com/watch?v=aaaaaaaaaaa", created_by="t")
    store.add_source("bee:1", "l", created_by="t", kind="bee")
    store.add_source("text:x", "l", created_by="t", kind="text")
    store.replace_segments(
        "bee:1", [{"idx": 0, "t0": 0.0, "t1": 1.0, "speaker": "Quill", "text": "ember"}]
    )
    corpus_id = store.add_corpus({"name": "Mixed"}, created_by="t")
    store.add_member(corpus_id, "bee:1", "author", created_by="t")
    first.close()
    _make_version_16(path)
    old = sqlite3.connect(path)
    for kind, metadata_source in (("vimeo", "vimeo"), ("file", "ffprobe")):
        with pytest.raises(sqlite3.IntegrityError):
            old.execute(
                "INSERT INTO sources (id, created_at, created_by, schema_version, kind, locator, "
                f"metadata_source) VALUES ('{kind}:1', 'now', 't', '2', '{kind}', 'l', "
                f"'{metadata_source}')"
            )
    old.close()

    opened = db.Database(path)
    assert opened.one("PRAGMA user_version")[0] == db.USER_VERSION == 17
    store = Store(opened)
    # every row kept its kind, and the member is still a member
    assert {row["id"]: row["kind"] for row in opened.all("SELECT id, kind FROM sources")} == {
        "aaaaaaaaaaa": "youtube",
        "bee:1": "bee",
        "text:x": "text",
    }
    assert store.member_source_ids(corpus_id) == ["bee:1"]
    assert store.add_source(
        "vimeo:1014932215", "https://vimeo.com/1014932215", created_by="t", kind="vimeo"
    )
    assert store.add_source("file:0123456789abcdef", "", created_by="t", kind="file")
    store.update_source("vimeo:1014932215", {"metadata_source": "vimeo"})
    store.update_source("file:0123456789abcdef", {"metadata_source": "ffprobe"})
    assert store.source("file:0123456789abcdef")["metadata_source"] == "ffprobe"
    backups = list((tmp_path / db.BACKUPS_DIRNAME).glob("*-before-v17.sqlite"))
    assert len(backups) == 1
    opened.close()


@pytest.mark.parametrize(
    ("kind", "locator", "expected"),
    [
        ("youtube", "https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://www.youtube.com/watch?v=aaaaaaaaaaa&t=12s"),
        ("vimeo", "https://vimeo.com/1014932215", "https://vimeo.com/1014932215#t=12s"),
        ("vimeo", "https://vimeo.com/1032634079?h=cae40ecf1a", "https://vimeo.com/1032634079?h=cae40ecf1a#t=12s"),
        ("file", "", None),
        ("bee", "recordings/1", None),
    ],
)  # fmt: skip
def test_a_card_links_to_the_source_locator_at_the_second(kind, locator, expected):
    source = {"id": f"{kind}:1", "kind": kind, "locator": locator, "title": "A talk"}
    assert review_kinds.anchor_url(source, 12.7) == expected
    if kind != "bee":
        assert review_kinds.youtube_review().anchor_url(source, 12.7) == expected


def test_vimeo_and_file_sources_share_the_youtube_review_kind():
    kinds = review_kinds.kinds_for_settings({"youtube": "", "vimeo": "", "file": "", "bee": "Q"})
    assert set(kinds) == {"youtube", "vimeo", "file", "bee"}
    assert kinds["vimeo"].scheme is kinds["youtube"].scheme is kinds["file"].scheme
    assert review_kinds.youtube_review().source_kinds == ("youtube", "vimeo", "file")
    assert "vimeo" in review_kinds.KINDS and "file" in review_kinds.KINDS


def test_a_corpus_of_only_vimeo_sources_is_listed_for_the_youtube_tool(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    store = Store(database)
    store.add_source("vimeo:1", "https://vimeo.com/1", created_by="t", kind="vimeo")
    store.add_source("bee:1", "l", created_by="t", kind="bee")
    only_vimeo = store.add_corpus({"name": "Replays"}, created_by="t")
    store.add_member(only_vimeo, "vimeo:1", "author", created_by="t")
    only_bee = store.add_corpus({"name": "Walks"}, created_by="t")
    store.add_member(only_bee, "bee:1", "author", created_by="t")
    assert store.corpus_names("youtube") == []
    assert store.corpus_names(("youtube", "vimeo", "file")) == ["Replays"]
    assert store.corpus_count(("youtube", "vimeo", "file")) == 1


def test_a_source_card_links_the_title_to_the_locator_or_shows_it_alone():
    card = {"source_id": "vimeo:1", "title": "A replay", "locator": "https://vimeo.com/1?h=a&b",
            "date": "", "channel": "", "people": [], "shared_concepts": []}  # fmt: skip
    html = monologue_page.video_card(card)
    assert '<a href="https://vimeo.com/1?h=a&amp;b"' in html and "A replay" in html
    assert "youtube.com/watch" not in html
    bare = monologue_page.video_card(card | {"locator": ""})
    assert "<a href" not in bare and "A replay" in bare
