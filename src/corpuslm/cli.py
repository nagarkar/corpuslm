"""The corpuslm command line (docs/design/surfaces.md): `help`, `doctor`, `completion`, the
review verbs `review page`, `review serve`, `review apply`, `review check` and
`review accept-suggestions`, and `suggestions record`.

Verbs print a short board (counts, paths, next step), never content;
`--json` anywhere on the line prints the board as JSON. The review verbs read one settings file
(`--config FILE`, see `corpuslm.review_settings`), so the database path never appears on a
command line."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import clikit, db
from .model_suggestions import backfill
from .review_accept_suggestions import accept, parse_cutoff
from .review_apply import apply as apply_answers
from .review_page import render, workspace_data_by_kind, write_pages
from .review_server import Answers, make_server
from .review_settings import ReviewSettings, SettingsError, kind_setups, load
from .store import Store

PROG = "corpuslm"
LAUNCH_AGENT = "com.nagarkar.corpuslm.review"
KICKSTART = f"launchctl kickstart -k gui/501/{LAUNCH_AGENT}"

DESCRIPTIONS = {
    "help": ("List the verbs, or show one verb's options.", "corpuslm help review serve"),
    "doctor": (
        "Check a transcript database without changing it: schema version, journal mode, "
        "integrity, and a row count of every table.",
        "corpuslm doctor /path/to/transcript.sqlite",
    ),
    "completion": (
        "Print the bash completion script, generated from the verbs.",
        'eval "$(corpuslm completion)"',
    ),
    "review page": (
        "Write the review page of every corpus of every source kind in the settings file as one "
        "file (a corpus dropdown), for a look; answers cannot be saved from a file, only from "
        "`review serve`. Writes only the page file.",
        "corpuslm review page --config /path/to/review.toml --out /tmp/review.html",
    ),
    "review serve": (
        "Serve the review page of every corpus of every source kind on this Mac (loopback "
        "address, one port from the settings file) and keep the owner's taps as answers in the "
        "database until `review apply` lands them. Runs until stopped; launchd keeps it up. "
        "Does not open the page in a browser. `--dry-run` starts nothing and prints the counts.",
        "corpuslm review serve --config /path/to/review.toml",
    ),
    "review apply": (
        "Land the answers saved on the review page as verdicts, for every source kind (or one "
        "with --kind), then delete the landed answers. Calls no model. A speaker answer "
        "rebuilds the speaker map where the tool's own `review apply` is used; other "
        "answers wait for the next AI clean-up run. `--dry-run` counts and writes nothing.",
        "corpuslm review apply --config /path/to/review.toml --dry-run",
    ),
    "review accept-suggestions": (
        "Answer the open ambiguity cards of one source kind with the clean-up model's recorded "
        "suggestion, for sources recorded before a date (--before): each answer is saved as the "
        "review page would save it, so it shows on the page for the owner to change; nothing "
        "is landed. A card that already has an answer, a speaker card, a card with a source "
        "on or after the date or of another kind, and a card with no recorded suggestion "
        "(or differing ones) are left alone. Calls no model. `--dry-run` counts and writes "
        "nothing.",
        "corpuslm review accept-suggestions --config /path/to/review.toml --kind bee "
        "--before 2026-07-01 --dry-run",
    ),
    "suggestions record": (
        "Record the clean-up model's top suggestion for every ambiguity in the database, read "
        "from each source's existing model clean-up file (no model is called, nothing is "
        "billed). Safe to run again: sources already recorded are left alone. `--dry-run` "
        "counts and writes nothing.",
        "corpuslm suggestions record --config /path/to/review.toml --dry-run",
    ),
    "review check": (
        "Check the running review server: it answers, and it is not older than the newest "
        f"commit of corpuslm's code (else: {KICKSTART}).",
        "corpuslm review check --config /path/to/review.toml",
    ),
}
VERBS = DESCRIPTIONS  # the verbs and their descriptions, for the repo skill's parity test


class DoctorError(Exception):
    pass


def doctor(path: Path) -> dict:
    """Read-only checks of the database at `path`. Never creates or migrates it."""
    if not path.is_file():
        raise DoctorError(f"{path} does not exist; nothing was created")
    found = db.inspect(path)
    version, integrity = found["user_version"], found["integrity_check"]
    problems = []
    if version > db.USER_VERSION:
        problems.append(f"schema version {version} is newer than this corpuslm ({db.USER_VERSION})")
    elif version < db.USER_VERSION:
        problems.append(
            f"schema version {version} is older than {db.USER_VERSION}; "
            "the next writer that opens it upgrades it"
        )
    if integrity != "ok":
        problems.append(f"integrity_check: {integrity}")
    return {
        "verb": "doctor",
        "ok": not problems,
        "path": str(path),
        "realpath": os.path.realpath(path),
        "user_version": version,
        "code_user_version": db.USER_VERSION,
        "journal_mode": found["journal_mode"],
        "integrity_check": integrity,
        "tables": found["tables"],
        "problems": problems,
    }


def _print(board: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(board, indent=2, sort_keys=True))
        return
    for key, value in board.items():
        if isinstance(value, dict):
            print(f"{key}:")
            for k, v in value.items():
                print(f"  {k}: {v}")
        elif isinstance(value, list):
            print(f"{key}: {'; '.join(str(v) for v in value) if value else 'none'}")
        else:
            print(f"{key}: {value}")


class _Parser(clikit.Parser):
    """A usage error reads `error: <what>. <the fix>` and exits 1."""

    def error(self, message: str):  # type: ignore[override]
        self.exit(1, f"error: {message}. Type '{PROG} help' for the verbs.\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG)
    verbs = parser.add_subparsers(dest="verb", metavar="<verb>")

    def leaf(group, name: str, help_text: str, key: str):
        sub = group.add_parser(name, help=help_text)
        clikit.describe(sub, *DESCRIPTIONS[key])
        return sub

    verbs_help = leaf(verbs, "help", "list the verbs, or show one verb's options", "help")
    verbs_help.add_argument("topic", nargs="*", help="a verb, then its subverb")
    doctor_parser = leaf(verbs, "doctor", "check a transcript database; changes nothing", "doctor")
    doctor_parser.add_argument("database", metavar="DB_PATH", help="the transcript database file")
    leaf(verbs, "completion", "print the bash completion script", "completion")
    review = verbs.add_parser("review", help="the review page: page, serve, apply, check")
    review_verbs = review.add_subparsers(dest="review_verb", metavar="<subverb>")
    for name, help_text in (
        ("page", "write the review page as one file"),
        ("serve", "serve the review page and keep its answers"),
        ("apply", "land the saved answers as verdicts"),
        ("check", "check the running review server"),
    ):
        sub = leaf(review_verbs, name, help_text, f"review {name}")
        sub.add_argument("--config", required=True, metavar="FILE", help="the review settings file")
        if name == "page":
            sub.add_argument("--out", metavar="FILE", default=None, help="the page file to write")
        if name in ("page", "serve", "apply"):
            sub.add_argument(
                "--dry-run", action="store_true", help="count and print; write nothing"
            )
        if name == "serve":
            sub.add_argument("--port", type=int, default=None, help="override the settings port")
        if name == "apply":
            sub.add_argument(
                "--kind", default=None, metavar="SOURCE_KIND", help="land only this source kind"
            )
    accept_parser = leaf(
        review_verbs,
        "accept-suggestions",
        "answer open cards with the model's recorded suggestion",
        "review accept-suggestions",
    )
    accept_parser.add_argument(
        "--config", required=True, metavar="FILE", help="the review settings file"
    )
    accept_parser.add_argument(
        "--kind", required=True, metavar="SOURCE_KIND", help="only sources of this kind"
    )
    accept_parser.add_argument(
        "--before",
        required=True,
        metavar="YYYY-MM-DD",
        help="only sources recorded (published_at) before this date",
    )
    accept_parser.add_argument(
        "--dry-run", action="store_true", help="count and print; write nothing"
    )
    suggestions = verbs.add_parser("suggestions", help="the model's recorded suggestions: record")
    suggestions_verbs = suggestions.add_subparsers(dest="suggestions_verb", metavar="<subverb>")
    record_parser = leaf(
        suggestions_verbs,
        "record",
        "record the model's suggestions from the existing clean-up files",
        "suggestions record",
    )
    record_parser.add_argument(
        "--config", required=True, metavar="FILE", help="the review settings file"
    )
    record_parser.add_argument(
        "--dry-run", action="store_true", help="count and print; write nothing"
    )
    return parser


def _open_existing(settings: ReviewSettings) -> db.Database:
    if not settings.database.is_file():
        raise SettingsError(f"{settings.database} does not exist; nothing was created")
    return db.Database(settings.database)


def _links_note(settings: ReviewSettings) -> str:
    notes = [s.kind.links_note for s in kind_setups(settings).values() if s.kind.links_note]
    return " ".join(dict.fromkeys(notes))


def review_page_board(settings: ReviewSettings, out: Path | None, dry_run: bool) -> dict:
    database = _open_existing(settings)
    try:
        data = workspace_data_by_kind(database, kind_setups(settings))
    finally:
        database.close()
    target = out or settings.path.with_name("review-page.html")
    board = {
        "verb": "review page",
        "ok": True,
        "corpora": len(data["corpora"]),
        "open_items": data["open_items"],
        "cards": data["cards"],
        "file": str(target),
        "next": "corpuslm review serve --config " + str(settings.path),
    }
    if dry_run:
        return board | {"note": "dry run: nothing was written"}
    written = write_pages(data, target, links_note=_links_note(settings))
    return board | {"files": [w["path"] for w in written]}


def review_serve_board(settings: ReviewSettings, port: int | None, dry_run: bool):
    """(server or None, board). The server is bound but not serving until `serve_forever`."""
    chosen = port or settings.port
    setups = kind_setups(settings)
    database = _open_existing(settings)
    try:
        data = workspace_data_by_kind(database, setups)
        waiting = sum(Store(database).review_answer_counts().values())
    finally:
        database.close()
    board = {
        "verb": "review serve",
        "ok": True,
        "url": f"http://127.0.0.1:{chosen}/",
        "corpora": len(data["corpora"]),
        "open_items": data["open_items"],
        "cards": data["cards"],
        "answers_waiting": waiting,
        "next": "corpuslm review apply --config " + str(settings.path),
    }
    if dry_run:
        return None, board | {"note": "dry run: nothing was started"}
    note = _links_note(settings)

    def build_page() -> str:  # built per request, so the page is never older than the files
        fresh = db.Database(settings.database)
        try:
            return render(workspace_data_by_kind(fresh, setups), links_note=note)
        finally:
            fresh.close()

    server = make_server("127.0.0.1", chosen, page=build_page, answers=Answers(settings.database))
    return server, board


def serve_until_stopped(server) -> None:
    """Serve until SIGTERM (launchd's stop) or Ctrl-C."""

    def stop(signum: int, frame: object) -> None:
        # shutdown() waits for serve_forever, which runs in this thread: ask from another one
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def review_apply_board(settings: ReviewSettings, kind: str | None, dry_run: bool) -> dict:
    setups = kind_setups(settings)
    if kind is not None and kind not in setups:
        raise SettingsError(
            f"source kind {kind!r} is not in the settings file; known: {', '.join(setups)}"
        )
    database = _open_existing(settings)
    try:
        report = apply_answers(
            database,
            setups,
            created_by="corpuslm",
            only_kinds=None if kind is None else [kind],
            dry_run=dry_run,
        )
    finally:
        database.close()
    return {
        "verb": "review apply",
        "ok": True,
        "corpora_with_answers": report.corpora,
        "answers_saved": report.answers_saved,
        "verdicts_to_land": report.verdicts_to_land,
        "verdicts_landed": report.verdicts_landed,
        "answers_cleared": report.answers_cleared,
        "unknown_cards": report.unknown_cards,
        "sources_waiting_for_ai_cleanup": report.sources_waiting_for_ai_cleanup,
        "note": "dry run: nothing was written"
        if dry_run
        else "a speaker answer takes effect when its tool rebuilds the speaker map "
        "(beelm review apply does); other answers wait for the next AI clean-up run",
        "next": "ytlm run --now / beelm corpus ai-cleanup --dry-run",
    }


def review_accept_suggestions_board(
    settings: ReviewSettings, kind: str, before: str, dry_run: bool
) -> dict:
    setups = kind_setups(settings)
    if kind not in setups:
        raise SettingsError(
            f"source kind {kind!r} is not in the settings file; known: {', '.join(setups)}"
        )
    cutoff = parse_cutoff(before)
    database = _open_existing(settings)
    try:
        report = accept(database, setups, kind=kind, before=cutoff, dry_run=dry_run)
    finally:
        database.close()
    return {
        "verb": "review accept-suggestions",
        "ok": True,
        "kind": kind,
        "before": cutoff.isoformat(),
        "cards_considered": report.cards_considered,
        "cards_to_answer": report.cards_to_answer,
        "cards_answered": report.cards_answered,
        "cards_skipped_already_answered": report.cards_skipped_already_answered,
        "cards_skipped_a_source_not_before_the_cutoff": (
            report.cards_skipped_a_source_not_before_the_cutoff
        ),
        "cards_skipped_no_recorded_suggestion": report.cards_skipped_no_recorded_suggestion,
        "cards_skipped_suggestions_differ": report.cards_skipped_suggestions_differ,
        "cards_left_alone_with_a_source_of_another_kind": (
            report.cards_left_alone_with_a_source_of_another_kind
        ),
        "note": "dry run: nothing was written"
        if dry_run
        else "the answers are saved, not landed: the owner sees them on the review page and "
        "can change them; `review apply` lands them",
        "next": "corpuslm review apply --config " + str(settings.path) + " --dry-run",
    }


def suggestions_record_board(settings: ReviewSettings, dry_run: bool) -> dict:
    database = _open_existing(settings)
    try:
        store = Store(database)
        report = backfill(store, kind_setups(settings), created_by="corpuslm", dry_run=dry_run)
        held = store.model_suggestion_counts()
    finally:
        database.close()
    return {
        "verb": "suggestions record",
        "ok": True,
        "sources_checked": report.sources_checked,
        "sources_without_model_cleanup": report.sources_without_model_cleanup,
        "sources_file_changed_since_built": report.sources_file_changed_since_built,
        "sources_unreadable": report.sources_unreadable,
        "sources_read": report.sources_read,
        "sources_already_recorded": report.sources_already_recorded,
        "sources_to_record": report.sources_to_record,
        "sources_recorded": report.sources_recorded,
        "suggestions_to_record": report.suggestions_to_record,
        "suggestions_recorded": report.suggestions_recorded,
        "suggestions_to_record_with_probability": report.suggestions_to_record_with_probability,
        "database_sources_with_suggestions": held["sources"],
        "database_suggestions": held["suggestions"],
        "database_suggestions_with_probability": held["with_probability"],
        "note": "dry run: nothing was written"
        if dry_run
        else "recorded; a second run writes nothing",
        "next": "corpuslm review accept-suggestions --config "
        + str(settings.path)
        + " --kind bee --before YYYY-MM-DD --dry-run",
    }


def _newest_commit_time(repository: Path) -> datetime | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repository), "log", "-1", "--format=%cI", "--", "src"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        return datetime.fromisoformat(out) if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def review_check_board(settings: ReviewSettings) -> dict:
    url = f"http://127.0.0.1:{settings.port}/health"
    problems: list[str] = []
    started: datetime | None = None
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            started_text = json.loads(response.read()).get("started_at", "")
        started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
    except (OSError, ValueError, urllib.error.URLError):
        problems.append(f"no review server answers at {url}; start it: {KICKSTART}")
    head = _newest_commit_time(Path(__file__).resolve().parents[2])
    if started and head and started < head:
        problems.append(f"server is older than HEAD: `{KICKSTART}`")
    return {
        "verb": "review check",
        "ok": not problems,
        "url": f"http://127.0.0.1:{settings.port}/",
        "server_started_at": started.isoformat() if started else "not running",
        "code_newest_commit_at": head.isoformat() if head else "unknown",
        "problems": problems,
    }


def _verb_board() -> dict:
    return {"verbs": {n: f"{d} Example: {e}" for n, (d, e) in DESCRIPTIONS.items()}}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    rest, flags, _ = clikit.extract_options(args)
    as_json = "--json" in flags
    parser = build_parser()
    if not rest or rest[0] in ("-h", "--help"):
        text, code = clikit.help_text(parser, PROG)
        _print(
            {"verbs": {n: f"{d} Example: {e}" for n, (d, e) in DESCRIPTIONS.items()}}, True
        ) if as_json else print(text, end="")
        return code
    if rest[0] == "help":
        topic = rest[1:]
        if as_json and not topic:
            _print(_verb_board(), True)
            return 0
        text, code = clikit.help_text(parser, PROG, topic)
        print(text, end="", file=sys.stdout if code == 0 else sys.stderr)
        return code
    if rest[0] == "completion":
        print(clikit.bash_completion(parser, PROG, [PROG]), end="")
        return 0
    namespace = parser.parse_args(rest)
    try:
        if namespace.verb == "doctor":
            board = doctor(Path(namespace.database).expanduser())
            _print(board, as_json)
            return 0 if board["ok"] else 1
        if namespace.verb == "review" and namespace.review_verb is None:
            text, code = clikit.help_text(parser, PROG, ["review"])
            print(text, end="")
            return code
        if namespace.verb == "suggestions" and namespace.suggestions_verb is None:
            text, code = clikit.help_text(parser, PROG, ["suggestions"])
            print(text, end="")
            return code
        settings = load(Path(namespace.config))
        if namespace.verb == "suggestions":
            board = suggestions_record_board(settings, namespace.dry_run)
            _print(board, as_json)
            return 0
        match namespace.review_verb:
            case "page":
                board = review_page_board(
                    settings, Path(namespace.out) if namespace.out else None, namespace.dry_run
                )
            case "apply":
                board = review_apply_board(settings, namespace.kind, namespace.dry_run)
            case "accept-suggestions":
                board = review_accept_suggestions_board(
                    settings, namespace.kind, namespace.before, namespace.dry_run
                )
            case "check":
                board = review_check_board(settings)
            case _:
                server, board = review_serve_board(settings, namespace.port, namespace.dry_run)
                _print(board, as_json)
                sys.stdout.flush()
                if server is not None:
                    serve_until_stopped(server)
                return 0
    except (DoctorError, SettingsError, sqlite3.Error, OSError, ValueError) as error:
        _print({"verb": args[0], "ok": False, "error": str(error)}, as_json)
        return 1
    _print(board, as_json)
    return 0 if board["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
