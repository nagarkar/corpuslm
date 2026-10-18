"""The teleprompter export (`corpuslm.monologues.export`) and page (`corpuslm.teleprompter_page`),
and the review server's prefix route for it."""

from __future__ import annotations

import threading

import pytest

from corpuslm import db, document, monologues, review_server, teleprompter_page
from corpuslm.store import Store


@pytest.fixture
def made(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    store = Store(database)
    doc = document.FakeDocumentStore(tmp_path / "docs")
    doc.ensure("SpiritualHindu", tmp_path / "ms", "SpiritualHindu")
    monologues.create(
        store, doc, "karma walk", series="SpiritualHindu", workspace=None, title="As of now",
        created_by="t",
    )  # fmt: skip
    yield tmp_path, store, doc
    database.close()


def _script(store, doc, tmp_path, text="# Script v1: As of now\n\nOne.\n\nTwo & three.\n"):
    doc.write_tab("SpiritualHindu", "karma-walk.md", text)


def test_export_writes_the_settled_text_with_the_monologue_id_in_the_header(made):
    tmp_path, store, doc = made
    with pytest.raises(ValueError, match="no script yet"):
        monologues.export(store, doc, "karma-walk")
    _script(store, doc, tmp_path)
    out = monologues.export(store, doc, "karma-walk", tmp_path / "out.txt")
    assert (out["tab"], out["words"], out["open_revisions"], out["note"]) == (
        "karma-walk.md",
        4,
        0,
        "",
    )
    text = (tmp_path / "out.txt").read_text()
    row = store.monologue("karma-walk")
    assert text.startswith(
        f"monologue: {row['id']}\nname: karma-walk\ntitle: As of now\nseries: SpiritualHindu\n"
        "file: karma-walk.md\n\n"
    )
    assert text.endswith("One.\n\nTwo & three.\n") and "# Script" not in text
    doc.stage("SpiritualHindu", "karma-walk.md", [{"old": "", "new": "Added.", "after": 2}])
    out = monologues.export(store, doc, "karma-walk")
    assert out["open_revisions"] == 1 and "open revisions" in out["note"]
    assert out["path"].endswith("ms/karma-walk.teleprompter.txt")
    assert "Added." not in open(out["path"]).read()  # the settled text, never an open revision


def test_the_page_shows_the_paragraphs_large_and_escaped_without_the_heading():
    html = teleprompter_page.render(
        "karma-walk", "As of now", "# Script v1\n\nOne.\n\nTwo & three.\n"
    )
    assert "<title>As of now</title>" in html and "<h1>As of now</h1>" in html
    assert "<p>One.</p><p>Two &amp; three.</p>" in html and "Script v1" not in html
    assert 'id="faster"' in html and "Tap to scroll" in html
    assert "<p>No script yet.</p>" in teleprompter_page.render("x", "", "")


def test_the_server_serves_a_prefix_page_and_declines_a_bad_name(made):
    tmp_path, store, doc = made
    _script(store, doc, tmp_path)
    answers = review_server.Answers(tmp_path / "transcript.sqlite")

    def build(name: str) -> str:  # a connection of its own: the server answers from a thread
        fresh = db.Database(tmp_path / "transcript.sqlite")
        try:
            row = Store(fresh).monologue(name)
        finally:
            fresh.close()
        if row is None:
            raise KeyError(name)
        _, text, _ = monologues.script_text(doc, row)
        return teleprompter_page.render(row["name"], row["title"], text)

    httpd = review_server.make_server(
        "127.0.0.1", 0, page=lambda: "<title>page</title>", answers=answers,
        page_prefixes={"/teleprompter/": build},
    )  # fmt: skip
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        from test_review_server import request

        status, body = request(httpd, "GET", "/teleprompter/karma-walk")
        assert status == 200 and b"<p>One.</p>" in body
        status, _ = request(httpd, "GET", "/teleprompter/nope")
        assert status == 404
        status, _ = request(httpd, "GET", "/teleprompter/../x")
        assert status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
