"""Usage: the one board of what the system spends, built from numbers only.

Two kinds of use, side by side. Model spend comes from each tool's own ledger folder
(`usage.jsonl`, read by `llmlm.usage.model_spend`), by tool, stage and day, beside the per-run
and per-day cap the tool's own settings file sets. Caption fetches come from the shared database
(`Store.fetch_attempt_counts`), per fetcher, outcome and day. The settings file names the folders
(`[usage]` in `review.toml`, see `corpuslm.review_settings`). The board is read by `usage show` in
the terminal and by `usage serve` as a page (`corpuslm.usage_page`, `corpuslm.usage_server`).

Counts and money only: a ledger line's prompt, model and tag are never copied into the board, and
the database is opened read-only, so reading the board changes nothing.

Never imports: cli, clikit, review_server, any model transport.

Decisions:
- One dashboard for the whole system, owned here: a tool keeps no usage page of its own, only its
  own `usage` verb. Enforced by: tests/test_usage.py::test_the_board_has_both_kinds_of_use.
- The board reads the ledger and the database and writes nothing; a missing ledger or database is
  named as a problem, never created. Enforced by:
  tests/test_usage.py::test_reading_the_board_changes_nothing.
- Dollar figures are the tools' own ledger figures from their price tables; the vendor's meter is
  the authority. A call with no known price is counted as `cost_unknown`, and a chat-road call is
  counted but never summed with billed ones. Enforced by:
  tests/test_usage.py::test_cost_unknown_and_chat_calls_are_counted_never_summed.

Terms:
- **model spend**: the dollars, calls, cached calls and tokens in a tool's `usage.jsonl`.
- **caption fetch**: one attempt to get a video's captions, kept in `fetch_attempts`; a fetcher is
  Supadata, YouTube or speech-to-text, an outcome is ok, none, error or skipped.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from llmlm import usage as model_usage

from . import clock
from .db import Database
from .review_settings import ReviewSettings
from .store import Store

NOTE = model_usage.NOTE


class _ReadOnlyDatabase(Database):
    """The shared database opened for reading only: no migration, no journal change."""

    def __init__(self, path: Path):  # noqa: D107 (does not call super: that would migrate)
        self.path = path
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._transaction_depth = 0


def fetch_usage(database: Path, month: str) -> dict:
    """Caption-fetch attempts per fetcher and outcome (counts only) for the month and all time.
    Metadata lookups (`metadata:*`) are left out. A Supadata `error` is whatever the service
    answered (HTTP 429 when the plan is used up); `none` is a video with no caption track."""
    board: dict = {"this_month": {}, "all_time": {}, "by_day": {}, "last_error": {}, "problems": []}
    if not database.is_file():
        board["problems"].append(f"{database} does not exist")
        return board
    try:
        connection = _ReadOnlyDatabase(database)
        try:
            rows = Store(connection).fetch_attempt_counts()
        finally:
            connection.close()
    except sqlite3.Error as error:
        board["problems"].append(f"cannot read fetch attempts: {error}")
        return board
    for row in rows:
        fetcher, outcome, count = row["fetcher"], row["outcome"], row["n"]
        if fetcher.startswith("metadata:"):
            continue
        board["by_day"].setdefault(row["day"], {}).setdefault(fetcher, {})[outcome] = count
        slots = [board["all_time"]] + (
            [board["this_month"]] if row["day"].startswith(month) else []
        )
        for table in slots:
            table.setdefault(fetcher, {}).setdefault(outcome, 0)
            table[fetcher][outcome] += count
        if outcome == "error":
            board["last_error"][fetcher] = row["last_detail"]
    return board


def board(
    settings: ReviewSettings, *, month: str | None = None, now: datetime | None = None
) -> dict:
    """The whole board for `month` (YYYY-MM; default the current month, UTC)."""
    usage = settings.usage
    if usage is None:
        raise ValueError(f"{settings.path} has no [usage] table; see scripts/review.example.toml")
    month = month or (now or clock.utc_now()).astimezone(UTC).strftime("%Y-%m")
    spend = model_usage.model_spend(usage.ledgers, month, usage.tool_settings)
    fetches = fetch_usage(settings.database, month)
    return {
        "month": month,
        "model_spend": {"tools": spend["tools"], "cost_usd": spend["cost_usd"]},
        "caption_fetches": fetches,
        "note": NOTE,
        "problems": spend["problems"] + fetches["problems"],
    }
