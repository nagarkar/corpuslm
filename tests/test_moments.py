"""Moments (`src/corpuslm/moments.py`): the notes parse and import, the proposal pass with a
scripted reply, a ruling that survives a rebuild of the same span, staleness, the estimate, and
the v12 -> v13 step. Synthetic text only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from corpuslm import claims, db, llm, moments
from corpuslm.store import Store

MODEL = "gemini/gemini-2.5-flash-lite"
CONFIG = {
    "claims": {"model": MODEL, "section_chars": 6000},
    "moments": {"model": MODEL, "max_moments": 3},
    "llm": {},
}
SOURCE = "bee:10526678"
SEGMENTS = [
    {"idx": i, "t0": 10.0 * i, "t1": 10.0 * i + 10, "speaker": "", "text": f"line {i}"}
    for i in range(12)
]
NOTES = """# Insight moments, group 1 (conversations 10526678 to 10591627)

## 10526678 - 20 September 2026

### Moment 1: a choice is right only as of now
- Where it is: 25 to 61 seconds, 00:25 to 01:01 (36 s). Lead-in from 20 s.
- Insight: prose that stays in the file.

### Moment 2: the world moves, so you choose again
- Where: 90 s to 115 s (1:30 to 1:55). Part 1 is 25 s.

### Moment 3: no range here
- Only prose.

## 99999999

### Moment 1: a conversation the database does not have
- Where: 0 to 10 seconds.
"""


def _claims(rows):
    return [
        {"section": 0, "segments": segments, "text": text, "kind": "assertion"}
        for text, segments in rows
    ]


class World:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "ws"
        self.scripts = tmp_path / "scripts"
        self.database = db.Database(tmp_path / "transcript.sqlite")
        self.store = Store(self.database)
        self.store.add_source(SOURCE, "l", created_by="test", kind="bee")
        self.store.update_source(SOURCE, {"title": "A walk", "published_at": "2026-09-20"})
        self.store.replace_segments(SOURCE, SEGMENTS)
        self.store.replace_claims(
            SOURCE,
            _claims([("One.", [2, 3]), ("Two.", [5]), ("Three.", [9, 10]), ("Four.", [11])]),
            build_key="k1",
            created_by="test",
        )
        self.store.record_artifact(
            SOURCE, claims.NODE, path="", sha256="k1", state="built",
            inputs={"text": "t"}, provenance={}, created_by="test",
        )  # fmt: skip
        self.notes = tmp_path / "notes"
        self.notes.mkdir()
        (self.notes / "Group 1 - 10526678.md").write_text(NOTES)

    def script(self, reply: dict, tag: str = SOURCE, tokens=(100, 20)) -> None:
        folder = self.scripts / moments.STAGE
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{tag}.json").write_text(
            json.dumps(
                {"reply": json.dumps(reply), "tokens_in": tokens[0], "tokens_out": tokens[1]}
            )
        )

    def client(self, config: dict | None = None) -> llm.Client:
        return llm.Client(
            self.root,
            config or CONFIG,
            transport=llm.ScriptedTransport(self.scripts),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )


@pytest.fixture
def world(tmp_path):
    made = World(tmp_path)
    made.script(
        {
            "moments": [
                {"insight": "A choice is right only as of now.", "claims": [0, 1]},
                {"insight": "The world moves and one chooses again.", "claims": [2]},
                {"insight": "Cites nothing the list has.", "claims": [40]},
            ]
        }
    )
    yield made
    made.database.close()


def test_parse_notes_takes_the_heading_as_the_insight_and_the_first_range():
    notes, unranged = moments.parse_notes(NOTES)
    assert [(n.conversation, n.insight, n.first_second, n.last_second) for n in notes] == [
        ("10526678", "a choice is right only as of now", 25, 61),
        ("10526678", "the world moves, so you choose again", 90, 115),
        ("99999999", "a conversation the database does not have", 0, 10),
    ]
    assert unranged == 1
    only_title, _ = moments.parse_notes(
        "# Insight moments: conversation 10690259\n\n### Moment 1: x\n- 3 to 9 s.\n"
    )
    assert only_title[0].conversation == "10690259"


def test_import_maps_seconds_to_segments_approves_and_is_safe_to_run_again(world):
    preview = moments.import_notes(world.store, world.notes, created_by="t", dry_run=True)
    assert preview == {
        "files": 1,
        "imported": 2,
        "sources": 1,
        "skipped": 0,
        "unknown_source": 1,
        "unranged": 1,
    }
    assert world.store.moment_count() == 0
    out = moments.import_notes(world.store, world.notes, created_by="t", dry_run=False)
    assert out == preview
    rows = world.store.moments(SOURCE)
    assert [(m["first_position"], m["last_position"], m["origin"]) for m in rows] == [
        (2, 6, "import"),
        (9, 11, "import"),
    ]
    assert rows[0]["insight"] == "a choice is right only as of now"
    assert moments.summary(world.store) == {
        "moments": 2, "states": {"approved": 2, "declined": 0, "proposed": 0},
        "origins": {"import": 2, "model": 0},
    }  # fmt: skip
    again = moments.import_notes(world.store, world.notes, created_by="t", dry_run=False)
    assert (again["imported"], again["skipped"]) == (0, 2)
    with pytest.raises(FileNotFoundError, match="no .md files"):
        moments.import_notes(world.store, world.notes / "nope", created_by="t", dry_run=True)


def test_build_proposes_from_claims_drops_empty_spans_and_keeps_imported_ones(world):
    moments.import_notes(world.store, world.notes, created_by="t", dry_run=False)
    out = moments.build(world.store, world.client(), SOURCE, created_by="test")
    assert (out["proposed"], out["dropped"], out["cached"]) == (
        2,
        1,
        False,
    )  # (2,5) is new; (2,6) is the imported one
    rows = world.store.moments(SOURCE)
    assert [(m["first_position"], m["last_position"], m["origin"]) for m in rows] == [
        (2, 5, "model"), (2, 6, "import"), (9, 10, "model"), (9, 11, "import")
    ]  # fmt: skip
    (entry,) = moments.status(world.store, CONFIG, [SOURCE])
    assert entry.state == "current"
    listed = moments.listing(world.store, state="proposed")
    assert [(m["start_seconds"], m["end_seconds"], m["insight"]) for m in listed] == [
        (20.0, 60.0, "A choice is right only as of now."),
        (90.0, 110.0, "The world moves and one chooses again."),
    ]
    assert listed[0]["concepts"] == []


def test_a_rebuild_keeps_the_ruling_on_the_same_span(world):
    moments.build(world.store, world.client(), SOURCE, created_by="test")
    proposed = moments.listing(world.store, state="proposed")
    assert len(proposed) == 2
    moments.verdict(
        world.store, proposed[1]["id"], "declined", answered_through="phone page", created_by="t"
    )
    moments.verdict(
        world.store, proposed[0]["id"], "approved", answered_through="phone page", created_by="t"
    )
    world.store.mark_stale(SOURCE, moments.NODE)
    again = moments.build(world.store, world.client(), SOURCE, created_by="test")
    assert again["cached"] is True
    states = {
        (m["first_position"], m["last_position"]): m["state"] for m in moments.listing(world.store)
    }
    assert states == {(2, 5): "approved", (9, 10): "declined"}
    with pytest.raises(KeyError):
        moments.verdict(
            world.store, "mom-nope", "approved", answered_through="phone page", created_by="t"
        )
    with pytest.raises(ValueError, match="approved or declined"):
        moments.verdict(
            world.store, proposed[0]["id"], "maybe", answered_through="phone page", created_by="t"
        )
    with pytest.raises(sqlite3.IntegrityError):
        world.database.execute("DELETE FROM moment_verdicts")


def test_rebuilt_claims_make_the_proposals_stale_and_the_estimate_calls_no_model(tmp_path):
    world = World(tmp_path)  # no scripted reply
    try:
        estimate = moments.estimate(world.store, world.client(), [SOURCE])
        assert (estimate.sources, estimate.calls) == (1, 1) and estimate.usd > 0
        assert not (world.root / "logs").exists()
        (entry,) = moments.status(world.store, CONFIG, [SOURCE])
        assert entry.state == "none"
        world.store.record_artifact(
            SOURCE, moments.NODE, path="", sha256="x", state="built",
            inputs=moments.inputs(world.store, SOURCE, CONFIG), provenance={}, created_by="t",
        )  # fmt: skip
        world.store.record_artifact(
            SOURCE, claims.NODE, path="", sha256="k2", state="built",
            inputs={"text": "t2"}, provenance={}, created_by="t",
        )  # fmt: skip
        (entry,) = moments.status(world.store, CONFIG, [SOURCE])
        assert (entry.state, entry.reason) == ("stale", "claims changed")
        capped = world.client(CONFIG | {"llm": {"max_usd_per_run": 0.0}})
        with pytest.raises(llm.SpendCapReached):
            moments.build(world.store, capped, SOURCE, created_by="t")
    finally:
        world.database.close()


def test_a_version_12_database_gains_the_moment_tables_after_a_verified_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    first = db.Database(path)
    Store(first).add_source(SOURCE, "l", created_by="test", kind="bee")
    first.close()
    connection = sqlite3.connect(path)
    for statement in (
        "DROP VIEW vw_current_moment_verdicts",
        "DROP TRIGGER moment_verdicts_no_update",
        "DROP TRIGGER moment_verdicts_no_delete",
        "DROP TABLE moment_verdicts",
        "DROP TABLE moments",
        "PRAGMA user_version = 12",
    ):
        connection.execute(statement)
    connection.commit()
    connection.close()
    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 12 and opened.migration["to"] == db.USER_VERSION == 15
        assert opened.migration["backup"].endswith("-before-v15.sqlite")
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        store = Store(opened)
        made = store.add_moment(SOURCE, 0, 1, "x", origin="import", created_by="t")
        assert made and store.add_moment(SOURCE, 0, 1, "y", origin="model", created_by="t") is None
        subject = store.moment_subject(SOURCE, 0, 1)
        store.append_moment_verdict(
            SOURCE, subject, "approved", answered_through="review file", created_by="t"
        )
        assert store.current_moment_verdicts(SOURCE) == {subject: "approved"}
        with pytest.raises(sqlite3.IntegrityError):
            opened.execute("UPDATE moment_verdicts SET verdict = 'declined'")
    finally:
        opened.close()
