"""The review page script against a real local review server, in jsdom (Node and
`npm ci --prefix tests/js` once; skipped without them). Bee-shaped fixtures only."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from review_kinds import (
    BEE_KIND,
    BEE_SOURCE,
    SECOND_BEE_SOURCE,
    add_corpus_of_bee_sources,
    ambiguity,
    bee_speaker_map,
    bee_transcript,
    write_source,
)

from corpuslm import db, review_page, review_server
from corpuslm.store import Store

JAVASCRIPT_DIRECTORY = Path(__file__).parent / "js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (JAVASCRIPT_DIRECTORY / "node_modules").is_dir(),
    reason="needs Node and `npm ci --prefix tests/js`",
)


def test_without_window_claude_the_page_saves_to_the_local_server_and_reads_it_back(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    sources = tmp_path / "sources"
    corpus_id = add_corpus_of_bee_sources(Store(database), "Walks", [BEE_SOURCE, SECOND_BEE_SOURCE])
    write_source(sources, BEE_SOURCE, bee_transcript(), bee_speaker_map())
    write_source(sources, SECOND_BEE_SOURCE, bee_transcript())
    documents = review_server.Answers(tmp_path / "transcript.sqlite")

    def page() -> str:  # the database is opened for each request, in the request's thread
        per_request = db.Database(tmp_path / "transcript.sqlite")
        try:
            return review_page.render(
                review_page.workspace_data(per_request, sources, kind=BEE_KIND)
            )
        finally:
            per_request.close()

    httpd = review_server.make_server("127.0.0.1", 0, page=page, answers=documents)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        process = subprocess.run(
            ["node", str(JAVASCRIPT_DIRECTORY / "local_page.js"), base],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=JAVASCRIPT_DIRECTORY,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
    database.close()
    assert process.returncode == 0, process.stderr[-2000:]
    report = json.loads(process.stdout)
    assert report["status"].startswith("Every tap is saved.")
    assert "run the apply verb" in report["status"]
    assert report["links"] == 0  # a Bee card has a time label and no link
    assert any("0:05" in place for place in report["where"])
    assert report["picker"][:2] == ["Ren", "Quill"]
    assert report["pressed"] == "true"
    assert report["reloaded_pressed"] == "true"
    saved = documents.list(f"reviews/{corpus_id}/items")
    assert [row["data"]["verdict"] for row in saved] == ["Ren"]


def test_a_page_of_entries_sorts_by_date_and_filters_by_heading(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    sources = tmp_path / "sources"
    store = Store(database)
    add_corpus_of_bee_sources(store, "Walks", [BEE_SOURCE, SECOND_BEE_SOURCE])
    store.update_source(
        BEE_SOURCE,
        {
            "title": "Kiln plans",
            "published_at": "2026-10-03T09:14:00-07:00",
            "duration_seconds": 600,
        },
    )
    store.update_source(
        SECOND_BEE_SOURCE,
        {"title": "Garden notes", "published_at": "2026-10-04T18:02:00-07:00"},
    )
    write_source(sources, BEE_SOURCE, bee_transcript(), bee_speaker_map())
    second = bee_transcript(ambiguities=[ambiguity("amb-0001", 2, "kiln", "kiln", ["kiln", "kin"])])
    write_source(sources, SECOND_BEE_SOURCE, second)
    kind = dataclasses.replace(BEE_KIND, source_entries=True)

    def page() -> str:
        per_request = db.Database(tmp_path / "transcript.sqlite")
        try:
            return review_page.render(review_page.workspace_data(per_request, sources, kind=kind))
        finally:
            per_request.close()

    httpd = review_server.make_server(
        "127.0.0.1", 0, page=page, answers=review_server.Answers(tmp_path / "transcript.sqlite")
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        process = subprocess.run(
            ["node", str(JAVASCRIPT_DIRECTORY / "entries_page.js"), base],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=JAVASCRIPT_DIRECTORY,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
    database.close()
    assert process.returncode == 0, process.stderr[-2000:]
    report = json.loads(process.stdout)
    assert report["bar_hidden"] is False
    # newest first, each entry headed by its heading, its time and its length
    assert report["newest_first"] == ["Garden notes", "Kiln plans"]
    assert report["facts"][1].startswith("Sat 2026-10-03 09:14 · 10 min · 2 cards")
    assert report["cards_per_entry"] == [1, 2] and report["loose_cards"] == 0
    assert report["oldest_first"] == ["Kiln plans", "Garden notes"]
    assert report["sort_label"] == "Oldest first"
    # typing heading text leaves only the entries whose heading has it, with their cards
    assert report["filtered"] == ["Garden notes"] and report["filtered_cards"] == 1
    assert "1 of 2 transcripts" in report["filtered_count"]
    assert report["filtered_by_date"] == ["Kiln plans"]
    assert report["no_match"] == "No transcript here matches the heading filter."
    # headings only: the entries stay, their cards fold away; a heading opens its own
    assert report["folded_cards"] == 0 and report["folded"] == ["Kiln plans", "Garden notes"]
    assert report["opened_cards"] == 2
    # an answered card leaves To do, and its entry with it when it was the last
    assert report["after_answer"] == ["Kiln plans"]
