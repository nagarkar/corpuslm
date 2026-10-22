"""The review verbs of the corpuslm command line (`review page`, `serve`, `apply`, `check`), over a
settings file and a transcript database of sanitized fixtures."""

from __future__ import annotations

import json
import socket
import threading

import pytest
from review_kinds import (
    YOUTUBE_SOURCE,
    add_youtube_source,
    bee_speaker_map,
    bee_transcript,
    write_source,
    youtube_speaker_map,
    youtube_transcript,
)

from corpuslm import cli, db, review_page, review_rebuilders, review_server, review_settings
from corpuslm.store import Store

BEE_ID = "bee:101"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def config(tmp_path):
    """A database with one corpus holding a YouTube and a Bee source, and the settings file."""
    database = db.Database(tmp_path / "transcript.sqlite")
    store = Store(database)
    corpus = store.add_corpus({"name": "Mixed"}, created_by="test")
    add_youtube_source(store, corpus)
    store.add_source(BEE_ID, "transcripts/101.md", created_by="test", kind="bee")
    store.add_member(corpus, BEE_ID, "author", created_by="test")
    database.close()
    write_source(tmp_path / "videos", YOUTUBE_SOURCE, youtube_transcript(), youtube_speaker_map())
    bee = bee_transcript() | {"video_id": BEE_ID}
    write_source(tmp_path / "transcripts", BEE_ID, bee, bee_speaker_map() | {"source_id": BEE_ID})
    path = tmp_path / "review.toml"
    path.write_text(
        f'database = "{tmp_path / "transcript.sqlite"}"\nport = {free_port()}\n'
        f'[kinds.youtube]\ndirectory = "{tmp_path / "videos"}"\n'
        f'[kinds.bee]\ndirectory = "{tmp_path / "transcripts"}"\nowner_first_name = "Quill"\n'
    )
    return path


def run(capsys, *argv):
    code = cli.main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_review_page_writes_one_page_of_every_kind_and_dry_run_writes_nothing(
    config, tmp_path, capsys
):
    out = tmp_path / "page.html"
    code, board = run(
        capsys, "review", "page", "--config", str(config), "--out", str(out), "--dry-run"
    )
    assert code == 0 and board["corpora"] == 1 and board["cards"] == 5
    assert not out.exists()
    code, board = run(capsys, "review", "page", "--config", str(config), "--out", str(out))
    assert code == 0 and board["files"] == [str(out)]
    page = out.read_text()
    assert 'id="corpus"' in page and "Timestamps open the YouTube app" in page
    assert "Times count from the start of each recording" in page


def test_review_serve_dry_run_starts_nothing_and_names_the_url(config, capsys):
    port = review_settings.load(config).port
    code, board = run(capsys, "review", "serve", "--config", str(config), "--dry-run")
    assert (
        code == 0 and board["url"] == f"http://127.0.0.1:{port}/" and board["answers_waiting"] == 0
    )
    assert board["directives_url"] == f"http://127.0.0.1:{port}/directives"
    with socket.socket() as probe:  # the port is still free
        probe.bind(("127.0.0.1", port))


def save_ambiguity_answer(config) -> None:
    settings = review_settings.load(config)
    database = db.Database(settings.database)
    data = review_page.workspace_data_by_kind(database, review_settings.kind_setups(settings))
    card = next(
        candidate for candidate in data["corpora"][0]["cards"] if candidate["kind"] == "ambiguity"
    )
    Store(database).save_review_answer(
        data["corpora"][0]["corpus_id"], card["id"], "Wren", "2026-10-09T10:00:00Z", created_by="t"
    )
    database.close()


class FakeEntry:
    """An installed tool's entry point: loading it gives the factory."""

    def __init__(self, factory):
        self.factory = factory

    def load(self):
        return self.factory


def test_review_apply_lands_the_youtube_answer_and_leaves_bee_to_beelm(config, capsys):
    save_ambiguity_answer(config)
    code, board = run(capsys, "review", "apply", "--config", str(config), "--dry-run")
    assert (code, board["verdicts_to_land"], board["verdicts_landed"]) == (0, 1, 0)
    assert (
        len(board["left_unlanded"]) == 1 and "./beelm.sh review apply" in board["left_unlanded"][0]
    )
    assert board["next"] == "./beelm.sh review apply --dry-run"
    code, board = run(capsys, "review", "apply", "--config", str(config))
    assert (code, board["verdicts_landed"], board["answers_cleared"]) == (0, 1, 0)
    assert board["sources_waiting_for_ai_cleanup"] == 1
    code, board = run(capsys, "review", "apply", "--config", str(config), "--kind", "audio")
    assert code == 1 and "audio" in board["error"]


def test_review_apply_lands_bee_where_beelms_rebuilder_is_registered(config, monkeypatch, capsys):
    save_ambiguity_answer(config)
    rebuilt: list[set[str]] = []
    directories = []

    def factory(database, directory):
        directories.append(directory)
        return lambda sources: rebuilt.append(set(sources)) or len(sources)

    monkeypatch.setattr(review_rebuilders, "registered", lambda: {"bee": FakeEntry(factory)})
    code, board = run(capsys, "review", "apply", "--config", str(config))
    assert (code, board["verdicts_landed"], board["answers_cleared"]) == (0, 2, 1)
    assert board["left_unlanded"] == []
    assert directories == [review_settings.load(config).kinds["bee"].directory]


def test_a_missing_settings_file_or_database_creates_nothing(tmp_path, capsys):
    code, board = run(capsys, "review", "apply", "--config", str(tmp_path / "nope.toml"))
    assert code == 1 and "--config FILE" in board["error"]
    path = tmp_path / "review.toml"
    path.write_text(
        f'database = "{tmp_path / "gone.sqlite"}"\n[kinds.bee]\ndirectory = "{tmp_path}"\n'
    )
    code, board = run(capsys, "review", "page", "--config", str(path))
    assert code == 1 and "nothing was created" in board["error"]
    assert not (tmp_path / "gone.sqlite").exists()


def test_review_check_says_when_no_server_answers_and_when_one_does(config, capsys):
    code, board = run(capsys, "review", "check", "--config", str(config))
    assert (
        code == 1
        and "launchctl kickstart -k gui/501/com.nagarkar.corpuslm.review" in board["problems"][0]
    )
    settings = review_settings.load(config)
    httpd = review_server.make_server(
        "127.0.0.1",
        settings.port,
        page=lambda: "",
        answers=review_server.Answers(settings.database),
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, board = run(capsys, "review", "check", "--config", str(config))
        assert code == 0 and board["problems"] == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_help_lists_the_review_verbs_and_completion_offers_them(capsys):
    assert cli.main(["help"]) == 0
    text = capsys.readouterr().out
    for verb in ("review page", "review serve", "review apply", "review check"):
        assert verb in text
    assert cli.main(["completion"]) == 0
    assert "review" in capsys.readouterr().out


def test_the_launch_agent_template_names_the_verb_and_the_settings_file_only():
    import plistlib
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / "com.nagarkar.corpuslm.review.plist"
    agent = plistlib.loads(path.read_bytes())
    arguments = agent["ProgramArguments"]
    assert arguments[1:6] == ["-m", "corpuslm.cli", "review", "serve", "--config"]
    assert not any(
        argument.endswith(".sqlite") for argument in arguments
    )  # the database path is in the file
    assert agent["Label"] == "com.nagarkar.corpuslm.review"
    assert agent["RunAtLoad"] is True and agent["KeepAlive"] is True
    assert agent["ThrottleInterval"] == 10
    assert agent["StandardOutPath"].endswith("corpuslm-review.log")
