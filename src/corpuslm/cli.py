"""The corpuslm command line (docs/design/surfaces.md): `help`, `doctor`, `completion`, the
review verbs `review page`, `review serve`, `review apply`, `review check` and
`review accept-suggestions`, `suggestions record`, the usage verbs `usage show` and
`usage serve`, the claim verbs `claims build`, `claims status` and `claims import-digests`, and
`text import` (a manuscript export as a text source), the concept verbs `concepts build`,
`concepts status`, `concepts list`, `concepts edges` and `concepts verdict`, and the moment verbs
`moments import`, `moments propose`, `moments status`, `moments list` and `moments verdict`, the
body-of-work verbs `body add` and `body list`, the sheet verbs `sheet draft`, `sheet refresh`
and `sheet status`, and the monologue verbs `monologue new`, `monologue attach`, `monologue
detach`, `monologue status`, `monologue run`, `monologue list` and `monologue export`.

Verbs print a short board (counts, paths, next step), never content;
`--json` anywhere on the line prints the board as JSON. The review verbs read one settings file
(`--config FILE`, see `corpuslm.review_settings`), so the database path never appears on a
command line."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import (
    bodies,
    claims,
    clikit,
    concepts,
    concepts_page,
    db,
    document,
    llm,
    moments,
    moments_page,
    monologue_page,
    monologues,
    review_rebuilders,
    teleprompter_page,
    text_source,
    usage,
    usage_page,
    usage_server,
)
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
        "with --kind), then delete the landed answers. Calls no model. A kind whose tool must "
        "rebuild what an answer changes (Bee: `./beelm.sh review apply`) lands only where that "
        "tool's rebuilder is installed in this environment; otherwise its answers stay unlanded "
        "and the board names the tool's verb. Other answers wait for the next AI cleanup run. "
        "`--dry-run` counts and writes nothing.",
        "corpuslm review apply --config /path/to/review.toml --dry-run",
    ),
    "review accept-suggestions": (
        "Answer the open ambiguity cards of one source kind with the AI cleanup's recorded "
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
        "Record the AI cleanup's top suggestion for every ambiguity in the database, read "
        "from each source's existing AI cleanup file (no model is called, nothing is "
        "billed). Safe to run again: sources already recorded are left alone. `--dry-run` "
        "counts and writes nothing.",
        "corpuslm suggestions record --config /path/to/review.toml --dry-run",
    ),
    "usage show": (
        "Show what the system spent: model spend by tool, stage and day with each tool's "
        "per-run cap beside the month, and caption fetches per fetcher, outcome and day. "
        "Numbers and money only; the dollar figures are the tools' own ledger figures and "
        "the vendor's meter is the authority. Reads the ledger folders and the database "
        "named under [usage] in the settings file; writes nothing. `--json` prints the "
        "board as JSON.",
        "corpuslm usage show --config /path/to/review.toml --month 2026-10",
    ),
    "usage serve": (
        "Serve the same usage board as a page on this Mac (loopback address, port from "
        "[usage] in the settings file, refreshed every 60 seconds) at / and /health. "
        "Read-only. Runs until stopped; launchd keeps it up. `--dry-run` starts nothing "
        "and prints the address.",
        "corpuslm usage serve --config /path/to/review.toml",
    ),
    "review check": (
        "Check the running review server: it answers, and it is not older than the newest "
        f"commit of corpuslm's code (else: {KICKSTART}).",
        "corpuslm review check --config /path/to/review.toml",
    ),
}
DESCRIPTIONS |= {
    "claims build": (
        "List what the chosen sources claim, one short sentence per claim tied to its segments, "
        "by a cheap model call per section (billed; `[claims]` and `[llm]` in the settings "
        "file). A source whose claims are current or imported is left alone. `--dry-run` "
        "counts the calls and estimates the cost; nothing is sent.",
        "corpuslm claims build --config /path/to/review.toml --kind bee --dry-run",
    ),
    "claims status": (
        "What stands for each source: claims counted, and whether they are current, stale "
        "(the reason names what changed), imported from a ytlm digest, or absent. Read-only.",
        "corpuslm claims status --config /path/to/review.toml",
    ),
    "claims import-digests": (
        "Turn ytlm's existing digests (`videos/<id>/digest.json` under the YouTube folder of the "
        "settings file) into claims with no model call. A source that already has claims is "
        "left alone. `--dry-run` counts and writes nothing.",
        "corpuslm claims import-digests --config /path/to/review.toml --dry-run",
    ),
    "concepts build": (
        "Name the concepts each claim of the chosen sources uses, batch by batch (billed; "
        "`[concepts]` plus the `[claims]` workspace and `[llm]` caps in the settings file). "
        "Labels already in use are reused, so the lexicon converges. A source whose tags are "
        "current is left alone. `--dry-run` counts the calls and estimates the cost.",
        "corpuslm concepts build --config /path/to/review.toml --kind bee --dry-run",
    ),
    "concepts status": (
        "How many concepts are in, out or on the boundary under the gate, the boundary list "
        "with its counts, the section-count distribution, and which sources' tags are current, "
        "stale or missing. Read-only.",
        "corpuslm concepts status --config /path/to/review.toml",
    ),
    "concepts list": (
        "The lexicon: every concept with its state and counts, most used first; `--state` "
        "narrows it. Read-only.",
        "corpuslm concepts list --config /path/to/review.toml --state in",
    ),
    "concepts edges": (
        "The concepts that share claims with one concept, most shared first, with the shared "
        "claim ids as evidence. Nothing is inferred. Read-only.",
        'corpuslm concepts edges "beat" --config /path/to/review.toml',
    ),
    "concepts verdict": (
        "Rule a boundary concept in or out; the newest ruling wins over the gate. Appends a row; "
        "nothing is ever overwritten.",
        'corpuslm concepts verdict "beat" --config /path/to/review.toml --in',
    ),
    "moments import": (
        "Bring the owner's insight-moment notes in as approved moments: every `.md` file in a "
        "folder, one `## <conversation id>` heading per Bee conversation, one `### Moment N: "
        "<insight>` per moment whose first bullet names a seconds range. A moment already present "
        "is left alone. No model. `--dry-run` counts and writes nothing.",
        'corpuslm moments import "/path/to/Insight moments" --config /path/to/review.toml '
        "--dry-run",
    ),
    "moments propose": (
        "Propose each chosen source's moments from its claims, one cheap model call per source "
        "(billed; `[moments]` plus the `[claims]` workspace and `[llm]` caps). The model's "
        "moments of a source are rebuilt whole; imported ones stay; a ruling on the same span "
        "survives. `--dry-run` counts the calls and estimates the cost.",
        "corpuslm moments propose --config /path/to/review.toml --kind bee --dry-run",
    ),
    "moments status": (
        "How many moments are approved, proposed or declined, by origin, and which sources' "
        "proposals are current, stale or missing. Read-only.",
        "corpuslm moments status --config /path/to/review.toml",
    ),
    "moments list": (
        "The moments with their state, span, origin and concepts, newest source first; "
        "`--state` and `--source` narrow it. Read-only.",
        "corpuslm moments list --config /path/to/review.toml --state proposed",
    ),
    "moments verdict": (
        "Approve or decline one moment by id; the newest ruling wins. Appends a row.",
        "corpuslm moments verdict mom-abc123 --config /path/to/review.toml --approve",
    ),
    "body add": (
        "Name a body of work a monologue may draw on: a standing one (a text source with "
        "`--text`, or a corpus with `--corpus`), which may carry a perspective sheet in its own "
        "manuscript (`--manuscript NAME --directory DIR`), or an occasional one (one source with "
        "`--source`). `--note` is the owner's commitments note, in the owner's words.",
        'corpuslm body add SMSTTD --text text:smsttd --manuscript SMSTTD --directory "/path/to/'
        'manuscripts/SMSTTD" --config /path/to/review.toml',
    ),
    "body list": (
        "The bodies of work used so far, most recently used first, with their kind, what they "
        "are, whether they have a sheet, and the note. Read-only.",
        "corpuslm body list --config /path/to/review.toml",
    ),
    "sheet draft": (
        "Draft a standing body's perspective sheet (stances, moves, tone) in one model call "
        "(billed; `[sheet]` plus the `[claims]` workspace and `[llm]` caps) and stage it as "
        "revisions into the `Perspective sheet` tab of the body's manuscript, for the owner to "
        "rule on in the Doc; the vocabulary section is written from the concepts in force. "
        "A tab with open revisions takes none. `--dry-run` estimates and sends nothing.",
        "corpuslm sheet draft SMSTTD --config /path/to/review.toml --dry-run",
    ),
    "sheet refresh": (
        "Rewrite the sheet's vocabulary section from the concepts in force; the owner's prose "
        "stays. No model.",
        "corpuslm sheet refresh SMSTTD --config /path/to/review.toml",
    ),
    "sheet status": (
        "The sheet as it stands: its version hash (recorded on the body when the resolved text "
        "changed), its open revisions, and whether it has prose yet. Reads the Doc.",
        "corpuslm sheet status SMSTTD --config /path/to/review.toml",
    ),
    "monologue new": (
        "Name a monologue, with an optional working title and brief (what it is about, in the "
        "owner's words). Attach moments and bodies of work next.",
        'corpuslm monologue new karma --brief "karmic theory, from the walk with my son" '
        "--config /path/to/review.toml",
    ),
    "monologue attach": (
        "Attach one approved moment (`--moment ID`) or one body of work (`--body NAME`) to a "
        "monologue; `monologue detach` takes it off again.",
        "corpuslm monologue attach karma --body SMSTTD --config /path/to/review.toml",
    ),
    "monologue detach": (
        "Take one moment (`--moment ID`) or one body of work (`--body NAME`) off a monologue.",
        "corpuslm monologue detach karma --moment mom-abc123 --config /path/to/review.toml",
    ),
    "monologue status": (
        "What stands: the runs so far, and whether the latest run's ingredients are current or "
        "what changed since (recipe, model, moments, a body's sheet). Reads the Doc for the "
        "sheets' versions; writes nothing but a body's recorded sheet version.",
        "corpuslm monologue status karma --config /path/to/review.toml",
    ),
    "monologue run": (
        "Run the recipe (billed; `[monologue]` plus the `[claims]` workspace and `[llm]` caps) "
        "unless a run for the same ingredients exists, in which case nothing is sent. The "
        "latest script tab's settled text, with the owner's edits, is the style guide; "
        "`--fresh` drops it for this run. The script becomes a new `Script v<n>` tab of the "
        "monologue's Doc. `--dry-run` estimates and sends nothing.",
        "corpuslm monologue run karma --config /path/to/review.toml --dry-run",
    ),
    "monologue list": (
        "Every monologue with its title, state, moments, bodies, runs and whether its "
        "ingredients are current. Read-only but for recorded sheet versions.",
        "corpuslm monologue list --config /path/to/review.toml",
    ),
    "monologue export": (
        "Write the latest script tab's settled text (with the owner's edits) as plain text, "
        "headed by the monologue's id and title, for the teleprompter and for podlm to record "
        "which monologue an episode came from. Open revisions are reported; the settled text is "
        "what is exported. The same text is served at /teleprompter/<name> on the review server.",
        'corpuslm monologue export karma --to "/path/to/karma.txt" --config /path/to/review.toml',
    ),
    "text import": (
        "Make or refresh a text source (a manuscript, paper or book) from a markdown export the "
        "owner made: one segment per paragraph, sections by heading, the `Perspective sheet` "
        "section left out. Never reads a live manuscript. A re-import replaces the segments and "
        "leaves the source's claims stale. `--dry-run` counts and writes nothing.",
        'corpuslm text import "/path/to/_exports/Book.md" --config /path/to/review.toml '
        "--name smsttd --dry-run",
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
            for entry_name, entry_value in value.items():
                print(f"  {entry_name}: {entry_value}")
        elif isinstance(value, list):
            print(f"{key}: {'; '.join(str(item) for item in value) if value else 'none'}")
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
        "record the model's suggestions from the existing cleanup files",
        "suggestions record",
    )
    record_parser.add_argument(
        "--config", required=True, metavar="FILE", help="the review settings file"
    )
    record_parser.add_argument(
        "--dry-run", action="store_true", help="count and print; write nothing"
    )
    claims_group = verbs.add_parser(
        "claims", help="what the sources claim: build, status, import-digests"
    )
    claims_verbs = claims_group.add_subparsers(dest="claims_verb", metavar="<subverb>")
    for name, help_text in (
        ("build", "list the sources' claims by a model call per section (billed)"),
        ("status", "what stands for each source's claims; read-only"),
        ("import-digests", "turn ytlm's digests into claims; no model"),
    ):
        sub = leaf(claims_verbs, name, help_text, f"claims {name}")
        sub.add_argument("--config", required=True, metavar="FILE", help="the review settings file")
        if name in ("build", "status"):
            sub.add_argument(
                "--kind", default=None, metavar="SOURCE_KIND", help="only sources of this kind"
            )
        if name == "build":
            sub.add_argument(
                "--source",
                action="append",
                default=None,
                metavar="SOURCE_ID",
                help="one source (repeatable); without it, every source of --kind or all",
            )
        if name in ("build", "import-digests"):
            sub.add_argument(
                "--dry-run", action="store_true", help="count and estimate; send and write nothing"
            )
    concepts_group = verbs.add_parser(
        "concepts", help="the lexicon derived from claims: build, status, list, edges, verdict"
    )
    concepts_verbs = concepts_group.add_subparsers(dest="concepts_verb", metavar="<subverb>")
    for name, help_text in (
        ("build", "name the concepts each claim uses (billed)"),
        ("status", "the lexicon's counts and the boundary list; read-only"),
        ("list", "every concept with its state and counts; read-only"),
        ("edges", "the concepts one concept shares claims with; read-only"),
        ("verdict", "rule a boundary concept in or out"),
    ):
        sub = leaf(concepts_verbs, name, help_text, f"concepts {name}")
        if name in ("edges", "verdict"):
            sub.add_argument("concept", metavar="LABEL", help="the concept's label or key")
        sub.add_argument("--config", required=True, metavar="FILE", help="the review settings file")
        if name in ("build", "status"):
            sub.add_argument(
                "--kind", default=None, metavar="SOURCE_KIND", help="only sources of this kind"
            )
        if name == "build":
            sub.add_argument(
                "--source",
                action="append",
                default=None,
                metavar="SOURCE_ID",
                help="one source (repeatable); without it, every source of --kind or all",
            )
            sub.add_argument(
                "--dry-run", action="store_true", help="count and estimate; send and write nothing"
            )
        if name == "list":
            sub.add_argument(
                "--state", default=None, choices=("in", "out", "boundary"), help="only this state"
            )
        if name == "verdict":
            ruling = sub.add_mutually_exclusive_group(required=True)
            ruling.add_argument("--in", dest="ruling", action="store_const", const="in")
            ruling.add_argument("--out", dest="ruling", action="store_const", const="out")
    moments_group = verbs.add_parser(
        "moments", help="the building block of monologues: import, propose, status, list, verdict"
    )
    moments_verbs = moments_group.add_subparsers(dest="moments_verb", metavar="<subverb>")
    for name, help_text in (
        ("import", "the owner's insight-moment notes as approved moments; no model"),
        ("propose", "propose moments from claims (billed)"),
        ("status", "counts by state and origin, and what stands per source; read-only"),
        ("list", "the moments with state, span and concepts; read-only"),
        ("verdict", "approve or decline one moment"),
    ):
        sub = leaf(moments_verbs, name, help_text, f"moments {name}")
        if name == "import":
            sub.add_argument("folder", metavar="DIR", help="the folder of note files")
        if name == "verdict":
            sub.add_argument("moment", metavar="MOMENT_ID", help="the moment's id")
        sub.add_argument("--config", required=True, metavar="FILE", help="the review settings file")
        if name in ("propose", "status"):
            sub.add_argument(
                "--kind", default=None, metavar="SOURCE_KIND", help="only sources of this kind"
            )
        if name == "propose":
            sub.add_argument(
                "--source",
                action="append",
                default=None,
                metavar="SOURCE_ID",
                help="one source (repeatable); without it, every source of --kind or all",
            )
        if name in ("import", "propose"):
            sub.add_argument(
                "--dry-run", action="store_true", help="count and estimate; send and write nothing"
            )
        if name == "list":
            sub.add_argument(
                "--state",
                default=None,
                choices=("approved", "proposed", "declined"),
                help="only this state",
            )
            sub.add_argument("--source", default=None, metavar="SOURCE_ID", help="one source")
        if name == "verdict":
            ruling = sub.add_mutually_exclusive_group(required=True)
            ruling.add_argument("--approve", dest="ruling", action="store_const", const="approved")
            ruling.add_argument("--decline", dest="ruling", action="store_const", const="declined")
    body_group = verbs.add_parser("body", help="bodies of work a monologue draws on: add, list")
    body_verbs = body_group.add_subparsers(dest="body_verb", metavar="<subverb>")
    body_add = leaf(body_verbs, "add", "name a body of work", "body add")
    body_add.add_argument("name", metavar="NAME", help="the owner's name for it")
    body_add.add_argument(
        "--config", required=True, metavar="FILE", help="the review settings file"
    )
    what = body_add.add_mutually_exclusive_group(required=True)
    what.add_argument("--text", metavar="SOURCE_ID", help="a text source (standing)")
    what.add_argument("--corpus", metavar="CORPUS_NAME", help="a corpus (standing)")
    what.add_argument("--source", metavar="SOURCE_ID", help="one source (occasional)")
    body_add.add_argument("--note", default="", metavar="TEXT", help="the owner's commitments note")
    body_add.add_argument(
        "--manuscript", default="", metavar="NAME", help="the authorlm manuscript for the sheet"
    )
    body_add.add_argument("--directory", default="", metavar="DIR", help="that manuscript's folder")
    body_list = leaf(body_verbs, "list", "the bodies of work used so far", "body list")
    body_list.add_argument(
        "--config", required=True, metavar="FILE", help="the review settings file"
    )
    sheet_group = verbs.add_parser(
        "sheet", help="a standing body's perspective sheet: draft, refresh, status"
    )
    sheet_verbs = sheet_group.add_subparsers(dest="sheet_verb", metavar="<subverb>")
    for name, help_text in (
        ("draft", "draft the sheet and stage it as revisions (billed)"),
        ("refresh", "rewrite the vocabulary section; no model"),
        ("status", "the sheet's version and open revisions"),
    ):
        sub = leaf(sheet_verbs, name, help_text, f"sheet {name}")
        sub.add_argument("body", metavar="BODY", help="the body of work's name")
        sub.add_argument("--config", required=True, metavar="FILE", help="the review settings file")
        if name == "draft":
            sub.add_argument(
                "--dry-run", action="store_true", help="estimate; send and write nothing"
            )
    monologue_group = verbs.add_parser(
        "monologue", help="the first artifact: new, attach, detach, status, run, list"
    )
    monologue_verbs = monologue_group.add_subparsers(dest="monologue_verb", metavar="<subverb>")
    for name, help_text in (
        ("new", "name a monologue"),
        ("attach", "attach a moment or a body of work"),
        ("detach", "take a moment or a body of work off"),
        ("status", "the runs, and what changed since the latest"),
        ("run", "run the recipe unless the ingredients ran already (billed)"),
        ("list", "every monologue; read-only"),
        ("export", "the settled script as plain text for the teleprompter"),
    ):
        sub = leaf(monologue_verbs, name, help_text, f"monologue {name}")
        if name != "list":
            sub.add_argument("name", metavar="NAME", help="the monologue's name")
        sub.add_argument("--config", required=True, metavar="FILE", help="the review settings file")
        if name == "new":
            sub.add_argument("--title", default="", metavar="TITLE", help="a working title")
            sub.add_argument("--brief", default="", metavar="TEXT", help="what it is about")
        if name in ("attach", "detach"):
            which = sub.add_mutually_exclusive_group(required=True)
            which.add_argument("--moment", metavar="MOMENT_ID", help="an approved moment's id")
            which.add_argument("--body", metavar="BODY", help="a body of work's name")
        if name == "export":
            sub.add_argument("--to", default=None, metavar="FILE", help="the text file to write")
        if name == "run":
            sub.add_argument(
                "--fresh", action="store_true", help="drop the prior script for this run"
            )
            sub.add_argument(
                "--dry-run", action="store_true", help="estimate; send and write nothing"
            )
    text_group = verbs.add_parser("text", help="text sources (manuscripts, papers, books): import")
    text_verbs = text_group.add_subparsers(dest="text_verb", metavar="<subverb>")
    text_import = leaf(text_verbs, "import", "a markdown export as a text source", "text import")
    text_import.add_argument("file", metavar="FILE", help="the markdown export the owner made")
    text_import.add_argument(
        "--config", required=True, metavar="FILE", help="the review settings file"
    )
    text_import.add_argument(
        "--name",
        required=True,
        metavar="NAME",
        help="the body of work's name; the id is text:<name>",
    )
    text_import.add_argument(
        "--title",
        default="",
        metavar="TITLE",
        help="the title to show (default: the first heading)",
    )
    text_import.add_argument(
        "--dry-run", action="store_true", help="count and print; write nothing"
    )
    usage_group = verbs.add_parser("usage", help="what the system spends: show, serve")
    usage_verbs = usage_group.add_subparsers(dest="usage_verb", metavar="<subverb>")
    for name, help_text in (
        ("show", "show model spend and caption fetches"),
        ("serve", "serve the usage page"),
    ):
        sub = leaf(usage_verbs, name, help_text, f"usage {name}")
        sub.add_argument("--config", required=True, metavar="FILE", help="the review settings file")
        if name == "show":
            sub.add_argument(
                "--month", default=None, metavar="YYYY-MM", help="the month (default: this one)"
            )
        else:
            sub.add_argument("--port", type=int, default=None, help="override the settings port")
            sub.add_argument(
                "--dry-run", action="store_true", help="start nothing; print the address"
            )
    return parser


def _usage_board(settings: ReviewSettings, month: str | None) -> dict:
    if settings.usage is None:
        raise SettingsError(
            f"{settings.path} has no [usage] table; see scripts/review.example.toml"
        )
    if month is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise SettingsError(f"--month must look like 2026-10, not {month!r}")
    return usage.board(settings, month=month)


def usage_serve_board(settings: ReviewSettings, port: int | None, dry_run: bool):
    """(server or None, board). The server is bound but not serving until `serve_forever`."""
    _usage_board(settings, None)  # a settings file with no [usage] table stops here
    assert settings.usage is not None
    chosen = port or settings.usage.port
    board = {
        "verb": "usage serve",
        "ok": True,
        "url": f"http://127.0.0.1:{chosen}/",
        "tools": list(settings.usage.ledgers),
        "next": "corpuslm usage show --config " + str(settings.path),
    }
    if dry_run:
        return None, board | {"note": "dry run: nothing was started"}

    def build_page() -> str:  # built per request, so the page is never older than the ledgers
        return usage_page.render(usage.board(settings))

    return usage_server.make_server("127.0.0.1", chosen, page=build_page), board


def _open_existing(settings: ReviewSettings) -> db.Database:
    if not settings.database.is_file():
        raise SettingsError(f"{settings.database} does not exist; nothing was created")
    return db.Database(settings.database)


def _links_note(settings: ReviewSettings) -> str:
    notes = [
        setup.kind.links_note for setup in kind_setups(settings).values() if setup.kind.links_note
    ]
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
    return board | {"files": [written_file["path"] for written_file in written]}


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
    if settings.concepts is not None:
        board["concepts_url"] = f"http://127.0.0.1:{chosen}/concepts"
    board["moments_url"] = f"http://127.0.0.1:{chosen}/moments"
    board["teleprompter_url"] = f"http://127.0.0.1:{chosen}/teleprompter/<monologue>"
    board["monologues_url"] = f"http://127.0.0.1:{chosen}/monologues"
    if dry_run:
        return None, board | {"note": "dry run: nothing was started"}
    note = _links_note(settings)

    def build_page() -> str:  # built per request, so the page is never older than the files
        fresh = db.Database(settings.database)
        try:
            return render(workspace_data_by_kind(fresh, setups), links_note=note)
        finally:
            fresh.close()

    pages = {}
    if settings.concepts is not None:
        config = settings.client_config()

        def build_concepts_page() -> str:
            fresh = db.Database(settings.database)
            try:
                store = Store(fresh)
                ruled = [row for row in concepts.listing(store, config) if row.get("verdict")]
                return concepts_page.render(concepts.summary(store, config), ruled)
            finally:
                fresh.close()

        pages["/concepts"] = build_concepts_page

    def build_moments_page() -> str:
        fresh = db.Database(settings.database)
        try:
            store = Store(fresh)
            rows = moments.listing(store, state="proposed")
            return moments_page.render(moments.summary(store), rows)
        finally:
            fresh.close()

    pages["/moments"] = build_moments_page

    def build_teleprompter(name: str) -> str:
        fresh = db.Database(settings.database)
        try:
            store = Store(fresh)
            row = store.monologue(name)
            if row is None:
                raise KeyError(name)
            _, text, _ = monologues.script_text(_document_store(settings), row)
            return teleprompter_page.render(row["name"], row["title"], text)
        finally:
            fresh.close()

    def build_monologues_list() -> str:
        fresh = db.Database(settings.database)
        try:
            config = settings.client_config() if settings.monologue is not None else None
            doc = _document_store(settings) if settings.claims is not None else None
            rows = (
                monologues.listing(Store(fresh), doc, config)
                if doc is not None
                else [
                    {"name": r["name"], "title": r["title"], "state": r["state"], "moments": 0,
                     "bodies": [], "runs": 0}
                    for r in Store(fresh).monologues()
                ]
            )  # fmt: skip
            return monologue_page.render_list(rows)
        finally:
            fresh.close()

    def build_monologue(name: str) -> str:
        fresh = db.Database(settings.database)
        try:
            return monologue_page.render(monologues.candidates(Store(fresh), name))
        finally:
            fresh.close()

    pages["/monologues"] = build_monologues_list
    prefixes = {"/teleprompter/": build_teleprompter, "/monologues/": build_monologue}
    server = make_server(
        "127.0.0.1",
        chosen,
        page=build_page,
        answers=Answers(settings.database, settings),
        pages=pages,
        page_prefixes=prefixes,
    )
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
        wanted = {
            name: settings.kinds[name].directory
            for name in setups
            if setups[name].kind.rebuilt_by and (kind is None or name == kind)
        }
        rebuilders, unavailable = review_rebuilders.discover(database, wanted)
        report = apply_answers(
            database,
            setups,
            created_by="corpuslm",
            only_kinds=None if kind is None else [kind],
            dry_run=dry_run,
            rebuilders=rebuilders,
        )
    finally:
        database.close()
    left = [
        f"{name}: {count} verdicts left unlanded; run {setups[name].kind.rebuilt_by} "
        f"(its rebuilder is not available here: {unavailable.get(name, 'unknown')})"
        for name, count in sorted(report.verdicts_left_unlanded.items())
    ]
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
        "left_unlanded": left,
        "note": "dry run: nothing was written"
        if dry_run
        else "a speaker answer takes effect when its tool rebuilds the speaker map; other "
        "answers wait for the next AI cleanup run",
        "next": (
            report_next(setups, report.verdicts_left_unlanded)
            if left
            else "ytlm run --now / ./beelm.sh corpus ai-cleanup --dry-run"
        ),
    }


def report_next(setups: dict, left: dict[str, int]) -> str:
    """The verb of the tool that lands what this process left, one per kind."""
    return " / ".join(setups[name].kind.rebuilt_by + " --dry-run" for name in sorted(left))


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
        "sources_without_ai_cleanup": report.sources_without_ai_cleanup,
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


def _claims_sources(store: Store, kind: str | None, chosen: list[str] | None) -> list[str]:
    if chosen:
        unknown = [source_id for source_id in chosen if not store.has_source(source_id)]
        if unknown:
            raise ValueError(f"unknown source: {', '.join(unknown)}")
        return list(dict.fromkeys(chosen))
    return store.source_ids(kind, include_ignored=False)


def _claims_client(settings: ReviewSettings, verb: str) -> llm.Client:
    if settings.claims is None:
        raise SettingsError(
            f"{settings.path}: [claims] is missing; add model and workspace (see "
            "scripts/review.example.toml)"
        )
    return llm.Client(settings.claims.workspace, settings.client_config(), verb=verb)


def claims_build_board(
    settings: ReviewSettings, kind: str | None, chosen: list[str] | None, dry_run: bool
) -> dict:
    database = _open_existing(settings)
    try:
        store = Store(database)
        client = _claims_client(settings, "claims build")
        source_ids = _claims_sources(store, kind, chosen)
        estimate = claims.estimate(store, client, source_ids)
        board = {
            "verb": "claims build",
            "ok": True,
            "sources_chosen": len(source_ids),
            "sources_to_build": estimate.sources,
            "from_cache": estimate.from_cache,
            "calls_billed": estimate.calls,
            "tokens_in": estimate.tokens_in,
            "tokens_out_assumed": estimate.tokens_out,
            "estimated_usd": estimate.usd,
            "model": estimate.model,
            "caps": settings.llm,
        }
        if dry_run:
            return board | {"status": "dry-run", "note": "dry run: nothing was sent or written"}
        standing = {
            entry.source_id: entry.state
            for entry in claims.status(store, client.config, source_ids)
        }
        built, claims_written, dropped, cost = 0, 0, 0, 0.0
        failed: dict[str, str] = {}
        stopped = ""
        for source_id in source_ids:
            if standing[source_id] in ("current", "imported"):
                continue
            try:
                out = claims.build(store, client, source_id, created_by="corpuslm")
            except llm.SpendCapReached as error:
                stopped = str(error)
                break
            except (llm.BadReply, llm.VendorError, llm.TruncatedReply) as error:
                # one source's bad reply is that source's failure, not the run's
                failed[source_id] = f"{type(error).__name__}: {str(error)[:200]}"
                claims.record_failed(store, source_id, failed[source_id], created_by="corpuslm")
                continue
            built += 1
            claims_written += out["claims"]
            dropped += out["dropped"]
            cost += out["cost_usd"]
        return board | {
            "built": built,
            "claims": claims_written,
            "dropped": dropped,
            "failed": len(failed),
            "failed_sources": failed,
            "cost_usd": round(cost, 6),
            "stopped_by": stopped,
            "ok": not stopped and not failed,
            "next": "corpuslm claims status --config " + str(settings.path),
        }
    finally:
        database.close()


def claims_status_board(settings: ReviewSettings, kind: str | None) -> dict:
    database = _open_existing(settings)
    try:
        store = Store(database)
        source_ids = store.source_ids(kind, include_ignored=False)
        config = settings.client_config() if settings.claims is not None else None
        entries = claims.status(store, config, source_ids)
        states = {state: 0 for state in ("current", "stale", "imported", "none")}
        reasons: dict[str, int] = {}
        for entry in entries:
            states[entry.state] += 1
            if entry.state == "stale":
                reasons[entry.reason] = reasons.get(entry.reason, 0) + 1
        return {
            "verb": "claims status",
            "ok": True,
            "sources": len(entries),
            "claims": store.claim_count(),
            "by_kind": store.claim_kind_counts(),
            "states": states,
            "stale_reasons": reasons,
            "next": "corpuslm claims build --config " + str(settings.path) + " --dry-run"
            if states["stale"] or states["none"]
            else "nothing to build",
        }
    finally:
        database.close()


def claims_import_board(settings: ReviewSettings, dry_run: bool) -> dict:
    if "youtube" not in settings.kinds:
        raise SettingsError(f"{settings.path}: [kinds.youtube] is missing; digests live there")
    database = _open_existing(settings)
    try:
        store = Store(database)
        out = claims.import_digests(
            store,
            settings.kinds["youtube"].directory,
            store.source_ids("youtube", include_ignored=False),
            created_by="corpuslm",
            dry_run=dry_run,
        )
        return (
            {"verb": "claims import-digests", "ok": True}
            | out
            | ({"status": "dry-run", "note": "dry run: nothing was written"} if dry_run else {})
        )
    finally:
        database.close()


def _concepts_client(settings: ReviewSettings, verb: str) -> llm.Client:
    if settings.concepts is None:
        raise SettingsError(
            f"{settings.path}: [concepts] is missing; add model (see scripts/review.example.toml)"
        )
    return _claims_client(settings, verb)


def _concepts_config(settings: ReviewSettings) -> dict:
    if settings.concepts is None:
        raise SettingsError(
            f"{settings.path}: [concepts] is missing; add model (see scripts/review.example.toml)"
        )
    return settings.client_config()


def concepts_build_board(
    settings: ReviewSettings, kind: str | None, chosen: list[str] | None, dry_run: bool
) -> dict:
    database = _open_existing(settings)
    try:
        store = Store(database)
        client = _concepts_client(settings, "concepts build")
        source_ids = _claims_sources(store, kind, chosen)
        estimate = concepts.estimate(store, client, source_ids)
        board = {
            "verb": "concepts build",
            "ok": True,
            "sources_chosen": len(source_ids),
            "sources_to_build": estimate.sources,
            "from_cache": estimate.from_cache,
            "calls_billed": estimate.calls,
            "tokens_in": estimate.tokens_in,
            "tokens_out_assumed": estimate.tokens_out,
            "estimated_usd": estimate.usd,
            "model": estimate.model,
            "caps": settings.llm,
        }
        if dry_run:
            return board | {"status": "dry-run", "note": "dry run: nothing was sent or written"}
        standing = {
            entry.source_id: entry.state
            for entry in concepts.status(store, client.config, source_ids)
        }
        built = tags = new_labels = 0
        cost = 0.0
        stopped = ""
        for source_id in source_ids:
            if standing[source_id] in ("current", "no_claims"):
                continue
            try:
                out = concepts.build(store, client, source_id, created_by="corpuslm")
            except llm.SpendCapReached as error:
                stopped = str(error)
                break
            built += 1
            tags += out["tags"]
            new_labels += out["new_labels"]
            cost += out["cost_usd"]
        return board | {
            "built": built,
            "tags": tags,
            "new_labels": new_labels,
            "cost_usd": round(cost, 6),
            "stopped_by": stopped,
            "ok": not stopped,
            "next": "corpuslm concepts status --config " + str(settings.path),
        }
    finally:
        database.close()


def concepts_status_board(settings: ReviewSettings, kind: str | None) -> dict:
    database = _open_existing(settings)
    try:
        store = Store(database)
        config = _concepts_config(settings)
        source_ids = store.source_ids(kind, include_ignored=False)
        states = {state: 0 for state in ("current", "stale", "none", "no_claims")}
        for entry in concepts.status(store, config, source_ids):
            states[entry.state] += 1
        return (
            {"verb": "concepts status", "ok": True}
            | concepts.summary(store, config)
            | {"sources": states}
        )
    finally:
        database.close()


def concepts_list_board(settings: ReviewSettings, state: str | None) -> dict:
    database = _open_existing(settings)
    try:
        rows = concepts.listing(Store(database), _concepts_config(settings), state)
        return {"verb": "concepts list", "ok": True, "count": len(rows), "concepts": rows}
    finally:
        database.close()


def concepts_edges_board(settings: ReviewSettings, label: str) -> dict:
    database = _open_existing(settings)
    try:
        found = concepts.edges(Store(database), label)
        return {
            "verb": "concepts edges",
            "ok": True,
            "concept": concepts.normalize(label),
            "edges": [
                {
                    "key": edge["key"],
                    "label": edge["label"],
                    "shared_claims": edge["shared"],
                    "claim_ids": edge["claim_ids"],
                }
                for edge in found
            ],  # fmt: skip
        }
    finally:
        database.close()


def concepts_verdict_board(settings: ReviewSettings, label: str, ruling: str) -> dict:
    database = _open_existing(settings)
    try:
        out = concepts.verdict(
            Store(database), label, ruling, answered_through="review file", created_by="corpuslm"
        )
        return {"verb": "concepts verdict", "ok": True} | out
    finally:
        database.close()


def _document_store(settings: ReviewSettings) -> document.DocumentStore:
    if settings.claims is None:
        raise SettingsError(
            f"{settings.path}: [claims] is missing; its workspace is where the document store "
            "keeps its state"
        )
    return document.store(settings.client_config(), settings.claims.workspace, origin="corpuslm")


def body_add_board(settings: ReviewSettings, namespace) -> dict:
    database = _open_existing(settings)
    try:
        store = Store(database)
        corpus_id = None
        if namespace.corpus:
            corpus = store.corpus_by_name(namespace.corpus, exact=True)
            if corpus is None:
                raise ValueError(f"unknown corpus {namespace.corpus!r}")
            corpus_id = corpus["id"]
        out = bodies.add(
            store,
            namespace.name,
            text_source_id=namespace.text,
            corpus_id=corpus_id,
            source_id=namespace.source,
            note=namespace.note,
            manuscript=namespace.manuscript,
            directory=namespace.directory,
            created_by="corpuslm",
        )
        return {"verb": "body add", "ok": True} | out
    finally:
        database.close()


def body_list_board(settings: ReviewSettings) -> dict:
    database = _open_existing(settings)
    try:
        rows = bodies.listing(Store(database))
        return {"verb": "body list", "ok": True, "count": len(rows), "bodies": rows}
    finally:
        database.close()


def sheet_draft_board(settings: ReviewSettings, name: str, dry_run: bool) -> dict:
    if settings.sheet is None:
        raise SettingsError(f"{settings.path}: [sheet] is missing; add model")
    database = _open_existing(settings)
    try:
        store = Store(database)
        client = _claims_client(settings, "sheet draft")
        estimate = bodies.estimate(store, client, name)
        board = {
            "verb": "sheet draft",
            "ok": True,
            "body": name,
            "claims_sampled": estimate.claims,
            "concepts": estimate.concepts,
            "tokens_in": estimate.tokens_in,
            "tokens_out_assumed": estimate.tokens_out,
            "estimated_usd": estimate.usd,
            "from_cache": estimate.cached,
            "model": estimate.model,
            "caps": settings.llm,
        }
        if dry_run:
            return board | {"status": "dry-run", "note": "dry run: nothing was sent or written"}
        out = bodies.draft(store, client, _document_store(settings), name, created_by="corpuslm")
        return (
            board
            | out
            | {
                "next": "rule on the revisions in the Doc, then: corpuslm sheet status "
                + name
                + " --config "
                + str(settings.path)
            }
        )
    finally:
        database.close()


def sheet_refresh_board(settings: ReviewSettings, name: str) -> dict:
    database = _open_existing(settings)
    try:
        out = bodies.refresh_vocabulary(
            Store(database), _document_store(settings), name, settings.client_config()
        )
        return {"verb": "sheet refresh", "ok": True} | out
    finally:
        database.close()


def sheet_status_board(settings: ReviewSettings, name: str) -> dict:
    database = _open_existing(settings)
    try:
        out = bodies.status(Store(database), _document_store(settings), name)
        return {"verb": "sheet status", "ok": True} | out
    finally:
        database.close()


def _monologue_config(settings: ReviewSettings) -> dict:
    if settings.monologue is None:
        raise SettingsError(f"{settings.path}: [monologue] is missing; add model")
    return settings.client_config()


def monologue_new_board(settings: ReviewSettings, name: str, title: str, brief: str) -> dict:
    database = _open_existing(settings)
    try:
        out = monologues.create(
            Store(database), name, title=title, brief=brief, created_by="corpuslm"
        )
        return (
            {"verb": "monologue new", "ok": True}
            | out
            | {
                "next": f"corpuslm monologue attach {out['name']} --moment ID | --body NAME "
                f"--config {settings.path}"
            }
        )
    finally:
        database.close()


def monologue_attach_board(settings: ReviewSettings, namespace, *, detach: bool) -> dict:
    database = _open_existing(settings)
    try:
        action = monologues.detach if detach else monologues.attach
        out = action(
            Store(database), namespace.name, moment_id=namespace.moment, body=namespace.body
        )
        return {"verb": "monologue detach" if detach else "monologue attach", "ok": True} | out
    finally:
        database.close()


def monologue_status_board(settings: ReviewSettings, name: str) -> dict:
    database = _open_existing(settings)
    try:
        standing = monologues.status(
            Store(database), _document_store(settings), name, _monologue_config(settings)
        )
        return {
            "verb": "monologue status",
            "ok": True,
            "monologue": standing.name,
            "runs": standing.runs,
            "ingredients": standing.state,
            "changes": standing.changes,
        }
    finally:
        database.close()


def monologue_run_board(settings: ReviewSettings, name: str, fresh: bool, dry_run: bool) -> dict:
    _monologue_config(settings)
    database = _open_existing(settings)
    try:
        store = Store(database)
        client = _claims_client(settings, "monologue run")
        doc = _document_store(settings)
        estimate = monologues.estimate(store, client, doc, name, fresh=fresh)
        board = {
            "verb": "monologue run",
            "ok": True,
            "monologue": monologues.slug(name),
            "moments": estimate.moments,
            "bodies": estimate.bodies,
            "stored_run": estimate.stored,
            "tokens_in": estimate.tokens_in,
            "tokens_out_assumed": estimate.tokens_out,
            "estimated_usd": estimate.usd,
            "model": estimate.model,
            "caps": settings.llm,
        }
        if dry_run:
            return board | {"status": "dry-run", "note": "dry run: nothing was sent or written"}
        assert settings.claims is not None
        directory = settings.claims.workspace / "monologues" / monologues.slug(name)
        out = monologues.run(
            store, client, doc, name, directory=directory, fresh=fresh, created_by="corpuslm"
        )
        return board | out
    finally:
        database.close()


def monologue_export_board(settings: ReviewSettings, name: str, to: str | None) -> dict:
    database = _open_existing(settings)
    try:
        out = monologues.export(
            Store(database), _document_store(settings), name, Path(to) if to else None
        )
        return {"verb": "monologue export", "ok": True} | out
    finally:
        database.close()


def monologue_list_board(settings: ReviewSettings) -> dict:
    database = _open_existing(settings)
    try:
        config = settings.client_config() if settings.monologue is not None else None
        rows = monologues.listing(Store(database), _document_store(settings), config)
        return {"verb": "monologue list", "ok": True, "count": len(rows), "monologues": rows}
    finally:
        database.close()


def _moments_client(settings: ReviewSettings, verb: str) -> llm.Client:
    if settings.moments is None:
        raise SettingsError(
            f"{settings.path}: [moments] is missing; add model (see scripts/review.example.toml)"
        )
    return _claims_client(settings, verb)


def moments_import_board(settings: ReviewSettings, folder: Path, dry_run: bool) -> dict:
    database = _open_existing(settings)
    try:
        out = moments.import_notes(Store(database), folder, created_by="corpuslm", dry_run=dry_run)
        return (
            {"verb": "moments import", "ok": True}
            | out
            | ({"status": "dry-run", "note": "dry run: nothing was written"} if dry_run else {})
        )
    finally:
        database.close()


def moments_propose_board(
    settings: ReviewSettings, kind: str | None, chosen: list[str] | None, dry_run: bool
) -> dict:
    database = _open_existing(settings)
    try:
        store = Store(database)
        client = _moments_client(settings, "moments propose")
        source_ids = _claims_sources(store, kind, chosen)
        estimate = moments.estimate(store, client, source_ids)
        board = {
            "verb": "moments propose",
            "ok": True,
            "sources_chosen": len(source_ids),
            "sources_to_build": estimate.sources,
            "from_cache": estimate.from_cache,
            "calls_billed": estimate.calls,
            "tokens_in": estimate.tokens_in,
            "tokens_out_assumed": estimate.tokens_out,
            "estimated_usd": estimate.usd,
            "model": estimate.model,
            "caps": settings.llm,
        }
        if dry_run:
            return board | {"status": "dry-run", "note": "dry run: nothing was sent or written"}
        standing = {
            entry.source_id: entry.state
            for entry in moments.status(store, client.config, source_ids)
        }
        built = proposed = dropped = 0
        cost = 0.0
        failed: dict[str, str] = {}
        stopped = ""
        for source_id in source_ids:
            if standing[source_id] in ("current", "no_claims"):
                continue
            try:
                out = moments.build(store, client, source_id, created_by="corpuslm")
            except llm.SpendCapReached as error:
                stopped = str(error)
                break
            except (llm.BadReply, llm.VendorError, llm.TruncatedReply) as error:
                failed[source_id] = f"{type(error).__name__}: {str(error)[:200]}"
                continue
            built += 1
            proposed += out["proposed"]
            dropped += out["dropped"]
            cost += out["cost_usd"]
        return board | {
            "built": built,
            "proposed": proposed,
            "dropped": dropped,
            "failed": len(failed),
            "failed_sources": failed,
            "cost_usd": round(cost, 6),
            "stopped_by": stopped,
            "ok": not stopped and not failed,
            "next": "open the moments page, or corpuslm moments list --config "
            + str(settings.path)
            + " --state proposed",
        }
    finally:
        database.close()


def moments_status_board(settings: ReviewSettings, kind: str | None) -> dict:
    database = _open_existing(settings)
    try:
        store = Store(database)
        board = {"verb": "moments status", "ok": True} | moments.summary(store)
        if settings.moments is not None:
            states = {state: 0 for state in ("current", "stale", "none", "no_claims")}
            config = settings.client_config()
            for entry in moments.status(
                store, config, store.source_ids(kind, include_ignored=False)
            ):
                states[entry.state] += 1
            board["sources"] = states
        return board
    finally:
        database.close()


def moments_list_board(settings: ReviewSettings, state: str | None, source: str | None) -> dict:
    database = _open_existing(settings)
    try:
        rows = moments.listing(Store(database), state=state, source_id=source)
        return {"verb": "moments list", "ok": True, "count": len(rows), "moments": rows}
    finally:
        database.close()


def moments_verdict_board(settings: ReviewSettings, moment_id: str, ruling: str) -> dict:
    database = _open_existing(settings)
    try:
        out = moments.verdict(
            Store(database), moment_id, ruling, answered_through="review file",
            created_by="corpuslm",
        )  # fmt: skip
        return {"verb": "moments verdict", "ok": True} | out
    finally:
        database.close()


def text_import_board(
    settings: ReviewSettings, file: Path, name: str, title: str, dry_run: bool
) -> dict:
    database = _open_existing(settings)
    try:
        out = text_source.import_text(
            Store(database), file, name=name, title=title, created_by="corpuslm", dry_run=dry_run
        )
        return (
            {"verb": "text import", "ok": True}
            | out
            | (
                {"status": "dry-run", "note": "dry run: nothing was written"}
                if dry_run
                else {
                    "next": "corpuslm claims build --config "
                    + str(settings.path)
                    + f" --source {out['source']} --dry-run"
                }
            )
        )
    finally:
        database.close()


def _verb_board() -> dict:
    return {
        "verbs": {
            verb: f"{description} Example: {example}"
            for verb, (description, example) in DESCRIPTIONS.items()
        }
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    rest, flags, _ = clikit.extract_options(args)
    as_json = "--json" in flags
    parser = build_parser()
    if not rest or rest[0] in ("-h", "--help"):
        text, code = clikit.help_text(parser, PROG)
        _print(
            {
                "verbs": {
                    verb: f"{description} Example: {example}"
                    for verb, (description, example) in DESCRIPTIONS.items()
                }
            },
            True,
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
        if namespace.verb == "usage" and namespace.usage_verb is None:
            text, code = clikit.help_text(parser, PROG, ["usage"])
            print(text, end="")
            return code
        if namespace.verb == "monologue" and namespace.monologue_verb is None:
            text, code = clikit.help_text(parser, PROG, ["monologue"])
            print(text, end="")
            return code
        if namespace.verb == "body" and namespace.body_verb is None:
            text, code = clikit.help_text(parser, PROG, ["body"])
            print(text, end="")
            return code
        if namespace.verb == "sheet" and namespace.sheet_verb is None:
            text, code = clikit.help_text(parser, PROG, ["sheet"])
            print(text, end="")
            return code
        if namespace.verb == "moments" and namespace.moments_verb is None:
            text, code = clikit.help_text(parser, PROG, ["moments"])
            print(text, end="")
            return code
        if namespace.verb == "text" and namespace.text_verb is None:
            text, code = clikit.help_text(parser, PROG, ["text"])
            print(text, end="")
            return code
        if namespace.verb == "concepts" and namespace.concepts_verb is None:
            text, code = clikit.help_text(parser, PROG, ["concepts"])
            print(text, end="")
            return code
        if namespace.verb == "claims" and namespace.claims_verb is None:
            text, code = clikit.help_text(parser, PROG, ["claims"])
            print(text, end="")
            return code
        settings = load(Path(namespace.config))
        if namespace.verb == "monologue":
            match namespace.monologue_verb:
                case "new":
                    board = monologue_new_board(
                        settings, namespace.name, namespace.title, namespace.brief
                    )
                case "attach":
                    board = monologue_attach_board(settings, namespace, detach=False)
                case "detach":
                    board = monologue_attach_board(settings, namespace, detach=True)
                case "status":
                    board = monologue_status_board(settings, namespace.name)
                case "run":
                    board = monologue_run_board(
                        settings, namespace.name, namespace.fresh, namespace.dry_run
                    )
                case "export":
                    board = monologue_export_board(settings, namespace.name, namespace.to)
                case _:
                    board = monologue_list_board(settings)
            _print(board, as_json)
            return 0 if board["ok"] else 1
        if namespace.verb == "body":
            board = (
                body_add_board(settings, namespace)
                if namespace.body_verb == "add"
                else body_list_board(settings)
            )
            _print(board, as_json)
            return 0
        if namespace.verb == "sheet":
            match namespace.sheet_verb:
                case "draft":
                    board = sheet_draft_board(settings, namespace.body, namespace.dry_run)
                case "refresh":
                    board = sheet_refresh_board(settings, namespace.body)
                case _:
                    board = sheet_status_board(settings, namespace.body)
            _print(board, as_json)
            return 0
        if namespace.verb == "moments":
            match namespace.moments_verb:
                case "import":
                    board = moments_import_board(
                        settings, Path(namespace.folder), namespace.dry_run
                    )
                case "propose":
                    board = moments_propose_board(
                        settings, namespace.kind, namespace.source, namespace.dry_run
                    )
                case "status":
                    board = moments_status_board(settings, namespace.kind)
                case "list":
                    board = moments_list_board(settings, namespace.state, namespace.source)
                case _:
                    board = moments_verdict_board(settings, namespace.moment, namespace.ruling)
            _print(board, as_json)
            return 0 if board["ok"] else 1
        if namespace.verb == "text":
            board = text_import_board(
                settings,
                Path(namespace.file),
                namespace.name,
                namespace.title,
                namespace.dry_run,
            )
            _print(board, as_json)
            return 0
        if namespace.verb == "concepts":
            match namespace.concepts_verb:
                case "build":
                    board = concepts_build_board(
                        settings, namespace.kind, namespace.source, namespace.dry_run
                    )
                case "status":
                    board = concepts_status_board(settings, namespace.kind)
                case "list":
                    board = concepts_list_board(settings, namespace.state)
                case "edges":
                    board = concepts_edges_board(settings, namespace.concept)
                case _:
                    board = concepts_verdict_board(settings, namespace.concept, namespace.ruling)
            _print(board, as_json)
            return 0 if board["ok"] else 1
        if namespace.verb == "claims":
            match namespace.claims_verb:
                case "build":
                    board = claims_build_board(
                        settings, namespace.kind, namespace.source, namespace.dry_run
                    )
                case "status":
                    board = claims_status_board(settings, namespace.kind)
                case _:
                    board = claims_import_board(settings, namespace.dry_run)
            _print(board, as_json)
            return 0 if board["ok"] else 1
        if namespace.verb == "usage":
            if namespace.usage_verb == "show":
                shown = _usage_board(settings, namespace.month)
                if as_json:
                    _print(shown, True)
                else:
                    print(usage_page.text(shown), end="")
                return 0
            server, board = usage_serve_board(settings, namespace.port, namespace.dry_run)
            _print(board, as_json)
            sys.stdout.flush()
            if server is not None:
                serve_until_stopped(server)
            return 0
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
    except (
        DoctorError,
        SettingsError,
        llm.NotConfigured,
        llm.BadReply,
        llm.VendorError,
        sqlite3.Error,
        OSError,
        ValueError,
        KeyError,
        document.DocumentUnavailable,
    ) as error:
        _print({"verb": args[0], "ok": False, "error": str(error)}, as_json)
        return 1
    except ModuleNotFoundError as error:
        # an optional package is missing: say what to install, no traceback
        fix = (
            "pip install -e ../authorllm/authorlm"
            if error.name == "authorlm"
            else "pip install -e '.[llm]'"
        )
        _print(
            {
                "verb": args[0],
                "ok": False,
                "error": f"{error.name} is not installed; run: {fix} in the corpuslm checkout",
            },
            as_json,
        )
        return 1
    _print(board, as_json)
    return 0 if board["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
