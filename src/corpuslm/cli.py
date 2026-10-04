"""The corpuslm command line: `help` and `doctor` (docs/design/surfaces.md).

Verbs print a short board (counts, paths, next step), never content;
`--json` anywhere on the line prints the board as JSON."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from . import db

VERBS = {
    "help": ("List the verbs.", "corpuslm help"),
    "doctor": (
        "Check a transcript database without changing it: schema version, journal mode, "
        "integrity, and a row count of every table.",
        'corpuslm doctor "/Volumes/Crucial X6/ytlm/ytlm.sqlite"',
    ),
}


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
            print(f"{key}: {'; '.join(value) if value else 'none'}")
        else:
            print(f"{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    if not args or args[0] in ("help", "-h", "--help"):
        _print(
            {"verbs": {name: f"{desc} Example: {ex}" for name, (desc, ex) in VERBS.items()}},
            as_json,
        )
        return 0
    if args[0] == "doctor":
        if len(args) != 2:
            print("usage: corpuslm doctor DB_PATH [--json]", file=sys.stderr)
            return 2
        try:
            board = doctor(Path(args[1]).expanduser())
        except (DoctorError, sqlite3.Error) as e:
            _print({"verb": "doctor", "ok": False, "error": str(e)}, as_json)
            return 1
        _print(board, as_json)
        return 0 if board["ok"] else 1
    print(f"unknown verb {args[0]!r}; run: corpuslm help", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
