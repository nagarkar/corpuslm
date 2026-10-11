"""Review rebuilders: how a tool tells corpuslm it can carry a landed answer into its files.

Landing an answer (a verdict row) is not the end of it: a speaker name changes a speaker map, a
transcript and a subtitle file that only the tool owning that source kind knows how to build. A
tool registers its rebuilder under the entry-point group `corpuslm.review_rebuilders`, named for
its source kind:

    [project.entry-points."corpuslm.review_rebuilders"]
    bee = "beelm.review:rebuilder"

The entry point is a factory `(database, directory) -> Rebuilder` (`database` the open
`corpuslm.db.Database`, `directory` the folder the settings file names for that kind). It raises
`RebuilderUnavailable` when the tool is installed but cannot work here (its workspace is not set).
`discover` returns the rebuilders that are available in this process and, for each kind whose
rebuilder is not, the reason; `corpuslm.review_apply` lands the answers of a kind that needs one
only when it is available.

Never imports: cli, clikit, db, any model transport.

Decisions:
- A rebuilder that is not installed, or whose factory says it is unavailable, is a named reason and
  never an error: the verb leaves that kind's answers unlanded and says which tool's verb lands
  them. Enforced by:
  tests/test_review_rebuilders.py::test_a_missing_or_unavailable_rebuilder_is_a_reason_not_an_error.
- A registered rebuilder is built once per run, for the kinds the settings file names. Enforced by:
  tests/test_review_rebuilders.py::test_discover_builds_the_rebuilder_of_each_named_kind.

Terms:
- **rebuilder**: a callable from the ids of the sources whose answers just landed to how many
  sources it rebuilt.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from importlib import metadata
from pathlib import Path

GROUP = "corpuslm.review_rebuilders"

# A tool's rebuild of what a landed speaker answer changes: the source ids -> how many it rebuilt.
Rebuilder = Callable[[set[str]], int]
Factory = Callable[[object, Path], Rebuilder]


class RebuilderUnavailable(RuntimeError):
    """The tool is installed but cannot rebuild here; the message says why, without text."""


def registered() -> Mapping[str, metadata.EntryPoint]:
    """The entry points of the group, by source kind."""
    return {entry.name: entry for entry in metadata.entry_points(group=GROUP)}


def discover(
    database,
    directories: Mapping[str, Path],
    *,
    entries: Mapping[str, metadata.EntryPoint] | None = None,
) -> tuple[dict[str, Rebuilder], dict[str, str]]:
    """(the rebuilder of each kind in `directories` that is available here, the reason for each
    that is not). `entries` replaces the installed entry points (tests)."""
    found = registered() if entries is None else entries
    available: dict[str, Rebuilder] = {}
    missing: dict[str, str] = {}
    for kind, directory in directories.items():
        entry = found.get(kind)
        if entry is None:
            missing[kind] = "no tool has registered a rebuilder for it in this environment"
            continue
        try:
            available[kind] = entry.load()(database, directory)
        except (ImportError, RebuilderUnavailable) as error:
            missing[kind] = str(error) or type(error).__name__
    return available, missing


__all__ = [
    "GROUP",
    "Factory",
    "Rebuilder",
    "RebuilderUnavailable",
    "discover",
    "registered",
]
