"""Events: what corpuslm publishes to the common events database (eventlm).

Every corpuslm verb that writes publishes after its own transaction commits, as the last thing
it does: `claims.built` (subject the source id; facts the counts of claims and directives) from
the claim pass, `source.cleaned` from the AI cleanup step of the cleanup chain, `speaker.landed`
from `review apply` for each source whose speaker answer landed. Events carry ids and counts,
never content. The file is named by `EVENTS_DB`, read as the other pointers are: the environment
first, else the `EVENTS_DB=` line of the working directory's `.env`. A missing pointer or file is
a board note, not an error: the verb's own work stands, nothing was published, and the note says
how to point at the file. Tests use `eventlm.Events.fake()`; nothing in a test touches a file.

Never imports: db, store, cli, clikit.

Decisions:
- A producer publishes after its own writes commit; an event never points at a row that is not
  there. (owner, 2026-10-21) Enforced by:
  tests/test_events.py::test_claims_built_is_published_after_the_claims_are_committed.
- A missing `EVENTS_DB` is a board note, never an error. (owner, 2026-10-21) Enforced by:
  tests/test_events.py::test_a_missing_pointer_is_a_note_and_nothing_fails.

Terms:
- **event**: one appended row of the events file: producer, kind, subject (ids), facts (counts).
- **EVENTS_DB**: the pointer to the events file, an environment variable or a line in `.env`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from eventlm.events import Events

PRODUCER = "corpuslm"
POINTER = "EVENTS_DB"
CLAIMS_BUILT = "claims.built"
SOURCE_CLEANED = "source.cleaned"
SPEAKER_LANDED = "speaker.landed"


def pointer(env_file: Path | None = None) -> Path | None:
    """The events file `EVENTS_DB` names: the environment, else the `EVENTS_DB=` line of
    `env_file` (default `./.env`)."""
    value = os.environ.get(POINTER, "").strip()
    if value:
        return Path(value).expanduser()
    env_path = Path(env_file) if env_file is not None else Path.cwd() / ".env"
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{POINTER}="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            return Path(value).expanduser() if value else None
    return None


def open_events(env_file: Path | None = None) -> tuple[Events | None, str | None]:
    """The events store, or None and the board note saying why nothing will be published."""
    path = pointer(env_file)
    if path is None:
        return None, f"{POINTER} is not set; nothing was published (write {POINTER}=PATH in .env)"
    if not path.is_file():
        return None, f"{path} does not exist; nothing was published (run `eventlm init {path}`)"
    return Events.open(path), None


def publish(
    events: Events | None,
    kind: str,
    subject: dict[str, Any],
    facts: dict[str, Any] | None = None,
) -> int | None:
    """Append one event as corpuslm, or nothing when there is no store. Returns the event id."""
    if events is None:
        return None
    return events.publish(PRODUCER, kind, subject, facts or {})


__all__ = [
    "CLAIMS_BUILT",
    "POINTER",
    "PRODUCER",
    "SOURCE_CLEANED",
    "SPEAKER_LANDED",
    "Events",
    "open_events",
    "pointer",
    "publish",
]
