"""The review page script against a real local review server, in jsdom (Node and
`npm ci --prefix tests/js` once; skipped without them). Bee-shaped fixtures only."""

from __future__ import annotations

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
