"""Review rebuilders (corpuslm.review_rebuilders): a tool registers how it carries a landed answer
into its files, and the verb leaves the kind alone where it is not available."""

from __future__ import annotations

from pathlib import Path

from corpuslm import review_rebuilders
from corpuslm.review_rebuilders import RebuilderUnavailable


class Entry:
    def __init__(self, factory):
        self.factory = factory

    def load(self):
        return self.factory


def test_discover_builds_the_rebuilder_of_each_named_kind():
    made = []

    def factory(database, directory):
        made.append((database, directory))
        return lambda sources: len(sources)

    available, missing = review_rebuilders.discover(
        "db", {"bee": Path("/bee")}, entries={"bee": Entry(factory), "audio": Entry(factory)}
    )
    assert available["bee"]({"a", "b"}) == 2 and missing == {}
    assert made == [("db", Path("/bee"))]  # the kind the settings do not name is never built


def test_a_missing_or_unavailable_rebuilder_is_a_reason_not_an_error():
    def unavailable(database, directory):
        raise RebuilderUnavailable("BEELM_WORKSPACE is not set")

    def broken_import(database, directory):
        raise ImportError("No module named beelm")

    available, missing = review_rebuilders.discover(
        None,
        {"bee": Path("/bee"), "youtube": Path("/yt"), "other": Path("/o")},
        entries={"bee": Entry(unavailable), "youtube": Entry(broken_import)},
    )
    assert available == {}
    assert missing["bee"] == "BEELM_WORKSPACE is not set"
    assert "beelm" in missing["youtube"]
    assert "no tool has registered" in missing["other"]
