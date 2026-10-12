"""Usage: the one board of what the system spends, built from numbers only.

Two kinds of use, side by side. Model spend comes from each tool's own ledger folder
(`usage.jsonl`, see `corpuslm.ledger`), by tool, stage and day, beside the per-run and per-day cap
the tool's own settings file sets. Caption fetches come from the shared database
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
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from . import clock, ledger
from .db import Database
from .review_settings import ReviewSettings
from .store import Store

NOTE = (
    "Dollar figures are each tool's own ledger figures, worked out from its price table. "
    "The vendor's own meter is the authority."
)
SPEND_FIELDS = ("calls", "cached", "tokens_in", "tokens_out", "cost_usd", "cost_unknown", "chat")


class _ReadOnlyDatabase(Database):
    """The shared database opened for reading only: no migration, no journal change."""

    def __init__(self, path: Path):  # noqa: D107 (does not call super: that would migrate)
        self.path = path
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._transaction_depth = 0


def _empty() -> dict:
    return {"calls": 0, "cached": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
            "cost_unknown": 0, "chat": 0}  # fmt: skip


def _add(row: dict, line: dict) -> None:
    if line.get("kind") == "chat":
        row["chat"] += 1
        return
    row["calls"] += 1
    row["cached"] += 1 if line.get("cached") else 0
    row["tokens_in"] += int(line.get("tokens_in") or 0)
    row["tokens_out"] += int(line.get("tokens_out") or 0)
    cost = line.get("cost_usd")
    if cost is None and not line.get("cached"):
        row["cost_unknown"] += 1
    else:
        row["cost_usd"] = round(row["cost_usd"] + float(cost or 0.0), 6)


def _caps(settings_file: Path | None) -> dict:
    """The two spend caps from a tool's own settings file; None where it sets none."""
    spend_caps: dict = {"per_run_usd": None, "per_day_usd": None, "problem": ""}
    if settings_file is None:
        return spend_caps
    try:
        table = tomllib.loads(settings_file.read_text("utf-8")).get("llm", {})
    except (OSError, tomllib.TOMLDecodeError) as error:
        return spend_caps | {"problem": f"cannot read {settings_file}: {type(error).__name__}"}
    for key, name in (("per_run_usd", "max_usd_per_run"), ("per_day_usd", "max_usd_per_day")):
        value = table.get(name) if isinstance(table, dict) else None
        if isinstance(value, int | float) and not isinstance(value, bool):
            spend_caps[key] = float(value)
    return spend_caps


def tool_spend(tool: str, folder: Path, settings_file: Path | None, month: str) -> dict:
    """One tool's model spend for `month` (YYYY-MM): totals, by stage, by day, and its caps."""
    path = folder / "usage.jsonl"
    found = path.exists() or path.with_name("usage.jsonl.1").exists()
    total, by_stage, by_day = _empty(), {}, {}
    for line in ledger.read(path, with_rotated=True):
        timestamp = str(line.get("at", ""))
        if not timestamp.startswith(month):
            continue
        _add(total, line)
        _add(by_stage.setdefault(str(line.get("stage", "?")), _empty()), line)
        _add(by_day.setdefault(timestamp[:10], _empty()), line)
    spend_caps = _caps(settings_file)
    problems = [spend_caps.pop("problem")] if spend_caps["problem"] else []
    spend_caps.pop("problem", None)
    if not found:
        problems.append(f"{tool}: no usage.jsonl in {folder}")
    return {
        "tool": tool,
        "ledger": str(path),
        "ledger_found": found,
        **spend_caps,
        "total": total,
        "biggest_day_usd": max((day["cost_usd"] for day in by_day.values()), default=0.0),
        "by_stage": dict(sorted(by_stage.items())),
        "by_day": dict(sorted(by_day.items())),
        "problems": problems,
    }


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
    tools = [
        tool_spend(name, folder, usage.tool_settings.get(name), month)
        for name, folder in usage.ledgers.items()
    ]
    fetches = fetch_usage(settings.database, month)
    return {
        "month": month,
        "model_spend": {
            "tools": {tool["tool"]: tool for tool in tools},
            "cost_usd": round(sum(tool["total"]["cost_usd"] for tool in tools), 6),
        },
        "caption_fetches": fetches,
        "note": NOTE,
        "problems": [problem for tool in tools for problem in tool["problems"]]
        + fetches["problems"],
    }
