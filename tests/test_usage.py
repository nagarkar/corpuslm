"""Usage (corpuslm.usage, usage_page): one board of model spend and caption fetches."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from llmlm import ledger
from review_kinds import YOUTUBE_SOURCE, add_youtube_source

from corpuslm import clock, db, review_settings, usage, usage_page
from corpuslm.store import Store

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def event(**over):
    return {
        "stage": "cleanup",
        "prompt_id": "cleanup",
        "model": "some/model",
        "tokens_in": 1000,
        "tokens_out": 200,
        "cost_usd": 0.25,
        "kind": "api",
        "cached": False,
        "tag": "SECRET-TAG-TEXT",
        "outcome": "ok",
    } | over


def spend(folder, day: str, **over):
    """One ledger line written the way a tool writes it, on the given day."""
    moment = datetime.fromisoformat(f"{day}T08:00:00+00:00")
    ledger.Ledger(folder, clock=lambda: moment).usage(event(**over))


@pytest.fixture
def settings(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    store = Store(database)
    corpus = store.add_corpus({"name": "Mixed"}, created_by="test")
    add_youtube_source(store, corpus)
    for fetcher, outcome, detail in (
        ("supadata", "ok", ""),
        ("supadata", "ok", ""),
        ("supadata", "error", "supadata: HTTP 429 Limit Exceeded"),
        ("ytapi", "none", ""),
        ("metadata:supadata", "ok", ""),
    ):
        store.append_fetch_attempt(YOUTUBE_SOURCE, fetcher, outcome, detail, created_by="test")
    database.close()
    for tool, per_run in (("ytlm", 2.0), ("beelm", 0.5)):
        (tmp_path / f"{tool}.toml").write_text(f"[llm]\nmax_usd_per_run = {per_run}\n")
    spend(tmp_path / "ytlm-logs", "2026-10-05", stage="pass3")
    spend(tmp_path / "ytlm-logs", "2026-10-05", stage="pass3", cached=True, cost_usd=0.0)
    spend(tmp_path / "ytlm-logs", "2026-10-06", stage="digest", cost_usd=None)
    spend(tmp_path / "ytlm-logs", "2026-10-06", stage="digest", kind="chat", cost_usd=None)
    spend(tmp_path / "ytlm-logs", "2026-09-30", stage="pass3", cost_usd=9.0)
    spend(tmp_path / "beelm-logs", "2026-10-09", stage="model_cleanup", cost_usd=0.5)
    path = tmp_path / "review.toml"
    path.write_text(
        f'database = "{tmp_path / "transcript.sqlite"}"\n'
        f'[kinds.youtube]\ndirectory = "{tmp_path}"\n'
        f'[usage.ledgers]\nytlm = "{tmp_path / "ytlm-logs"}"\nbeelm = "{tmp_path / "beelm-logs"}"\n'
        f'[usage.tool_settings]\nytlm = "{tmp_path / "ytlm.toml"}"\n'
        f'beelm = "{tmp_path / "beelm.toml"}"\n'
    )
    return review_settings.load(path)


def test_the_board_has_both_kinds_of_use(settings):
    board = usage.board(settings, now=NOW)
    this_month = datetime.now(UTC).strftime("%Y-%m")
    assert board["month"] == "2026-10"
    assert set(board["model_spend"]["tools"]) == {"ytlm", "beelm"}
    ytlm = board["model_spend"]["tools"]["ytlm"]
    assert ytlm["total"]["calls"] == 3 and ytlm["total"]["cached"] == 1
    assert ytlm["total"]["cost_usd"] == 0.25  # September's $9 is another month
    assert ytlm["per_run_usd"] == 2.0 and ytlm["per_day_usd"] is None
    assert ytlm["by_stage"]["pass3"]["calls"] == 2
    assert ytlm["by_day"]["2026-10-05"]["cost_usd"] == 0.25
    assert ytlm["biggest_day_usd"] == 0.25
    assert board["model_spend"]["tools"]["beelm"]["per_run_usd"] == 0.5
    assert board["model_spend"]["cost_usd"] == 0.75
    fetches = usage.board(settings, month=this_month)["caption_fetches"]
    assert fetches["all_time"]["supadata"] == {"ok": 2, "error": 1}
    assert "metadata:supadata" not in fetches["all_time"]
    assert fetches["last_error"]["supadata"] == "supadata: HTTP 429 Limit Exceeded"
    assert board["problems"] == []


def test_cost_unknown_and_chat_calls_are_counted_never_summed(settings):
    digest = usage.board(settings, now=NOW)["model_spend"]["tools"]["ytlm"]["by_stage"]["digest"]
    assert digest["calls"] == 1 and digest["cost_unknown"] == 1
    assert digest["chat"] == 1 and digest["cost_usd"] == 0.0


def test_reading_the_board_changes_nothing(settings):
    files = [settings.database, *settings.usage.ledgers.values()]
    before = {
        path: [(file.name, file.read_bytes()) for file in sorted(path.rglob("*")) if file.is_file()]
        if path.is_dir()
        else path.read_bytes()
        for path in files
    }
    usage.board(settings, now=NOW)
    after = {
        path: [(file.name, file.read_bytes()) for file in sorted(path.rglob("*")) if file.is_file()]
        if path.is_dir()
        else path.read_bytes()
        for path in files
    }
    assert before == after


def test_a_missing_ledger_or_database_is_a_problem_and_nothing_is_created(settings, tmp_path):
    (tmp_path / "beelm-logs" / "usage.jsonl").unlink()
    settings.database.unlink()
    for extra in tmp_path.glob("transcript.sqlite-*"):
        extra.unlink()
    board = usage.board(settings, now=NOW)
    assert any("beelm: no usage.jsonl" in problem for problem in board["problems"])
    assert any("does not exist" in problem for problem in board["problems"])
    assert not settings.database.exists()


def test_the_board_carries_numbers_never_content(settings):
    text = json.dumps(usage.board(settings, now=NOW)) + usage_page.render(
        usage.board(settings, now=NOW)
    )
    assert "SECRET-TAG-TEXT" not in text and "some/model" not in text


def test_the_page_shows_spend_caps_fetches_and_the_note(settings):
    page = usage_page.render(usage.board(settings, now=NOW))
    assert 'http-equiv="refresh" content="60"' in page
    assert "prefers-color-scheme:dark" in page and 'name="viewport"' in page
    assert "Per-run cap $2.00" in page and "Per-run cap $0.50" in page
    assert "meter is the authority" in page and "ledger figures" in page
    assert "Supadata calls answered" in page and "HTTP 429" in page


def test_a_settings_file_without_a_usage_table_is_named(tmp_path):
    path = tmp_path / "review.toml"
    path.write_text(
        f'database = "{tmp_path}/t.sqlite"\n[kinds.youtube]\ndirectory = "{tmp_path}"\n'
    )
    with pytest.raises(ValueError, match="no \\[usage\\] table"):
        usage.board(review_settings.load(path), now=clock.utc_now())


def test_a_fetch_error_text_is_escaped_on_the_page(settings):
    database = db.Database(settings.database)
    Store(database).append_fetch_attempt(
        YOUTUBE_SOURCE, "supadata", "error", "zz <script>alert(1)</script>", created_by="test"
    )
    database.close()
    page = usage_page.render(usage.board(settings, now=NOW))
    assert "<script>" not in page and "&lt;script&gt;" in page
