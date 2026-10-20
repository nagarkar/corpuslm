"""Line speakers (`src/corpuslm/line_speakers.py`, `speaker_map.apply_line_speakers`): the pass
with scripted replies, the names a model may use, the owner's pick winning, an unsure pick naming
nothing, staleness, the estimate, the settings table and the v15 -> v16 step. Synthetic text
only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from corpuslm import db, line_speakers, llm, review_settings, speaker_map
from corpuslm.store import Store

MODEL = "gemini/gemini-2.5-flash-lite"
HOUSEHOLD = [
    {"name": "Ana", "role": "the owner, a mother"},
    {"name": "Teo", "role": "her son", "aliases": ["Leo"]},
]
CONFIG = {
    "line_speakers": {
        "model": MODEL,
        "owner": "Ana",
        "household": HOUSEHOLD,
        "window_lines": 4,
        "context_lines": 1,
    },
    "llm": {},
}
SOURCE = "bee:1"
LABELS = ["Ana", "Unknown", "Unknown", "Ana", "Unknown", "Unknown"]
SEGMENTS = [
    {"idx": i, "t0": 5.0 * i, "t1": 5.0 * i + 5, "speaker": label, "text": f"line {i}"}
    for i, label in enumerate(LABELS)
]
SUMMARY = "## Summary\nA walk with her son.\n\n## Atmosphere\nQuiet.\n\n## Action Items\n- none"


class World:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "ws"
        self.scripts = tmp_path / "scripts"
        self.database = db.Database(tmp_path / "transcript.sqlite")
        self.store = Store(self.database)
        self.store.add_source(SOURCE, "l", created_by="test", kind="bee")
        self.store.update_source(SOURCE, {"title": "A walk", "description": SUMMARY})
        self.store.replace_segments(SOURCE, SEGMENTS)

    def script(self, first: int, lines: list[dict]) -> None:
        folder = self.scripts / line_speakers.STAGE
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{SOURCE}-w{first}.json").write_text(
            json.dumps(
                {
                    "reply": json.dumps({"present": [{"name": "Teo"}], "lines": lines}),
                    "tokens_in": 100,
                    "tokens_out": 20,
                }
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
        0,
        [
            {"index": 1, "who": "Ana", "confidence": 0.9},
            {"index": 2, "who": "Teo", "confidence": 0.4},
            {"index": 3, "who": "Teo", "confidence": 0.9},  # labeled Ana: not asked, ignored
        ],
    )
    made.script(
        4,
        [
            {"index": 4, "who": "Mallory", "confidence": 0.95},
            {"index": 5, "who": "media", "confidence": 0.95},
            {"index": 9, "who": "Teo", "confidence": 0.9},  # outside the window: ignored
        ],
    )
    yield made
    made.database.close()


def test_windows_cover_every_line_once_with_context_before():
    lines = [{"idx": i} for i in range(6)]
    pairs = line_speakers.windows(lines, 4, 1)
    assert [[line["idx"] for line in window] for _, window in pairs] == [[0, 1, 2, 3], [4, 5]]
    assert [[line["idx"] for line in context] for context, _ in pairs] == [[], [3]]


def test_the_summary_keeps_who_was_there_and_what_played():
    assert line_speakers.summary_of({"description": SUMMARY}) == (
        "## Summary\nA walk with her son.\n\n## Atmosphere\nQuiet."
    )
    assert line_speakers.summary_of({"description": "plain"}) == "plain"


def test_the_pass_keeps_one_pick_per_unidentified_line_of_each_window(world):
    out = line_speakers.build(world.store, world.client(), SOURCE, created_by="t")
    assert out["picks"] == 4 and out["confident"] == 2 and out["unsure"] == 2
    picks = world.store.line_speakers(SOURCE)
    assert sorted(picks) == [1, 2, 4, 5]
    assert picks[5] == {"name": "media", "confidence": 0.95}


def test_a_name_outside_the_household_is_read_as_unclear(world):
    line_speakers.build(world.store, world.client(), SOURCE, created_by="t")
    assert world.store.line_speakers(SOURCE)[4] == {"name": "unclear", "confidence": 0.0}


def test_an_unsure_pick_keeps_the_recorder_label():
    document = {
        "segments": [
            {"idx": 0, "role": "Unknown", "speaker": "Teo"},  # the per-label map's name
            {"idx": 1, "role": "Unknown", "speaker": "Teo"},
            {"idx": 2, "role": "Unknown", "speaker": "Teo"},
            {"idx": 3, "role": "Ana", "speaker": "Ana"},
        ]
    }
    picks = {
        0: {"name": "Ana", "confidence": 0.9},
        1: {"name": "Ana", "confidence": 0.85},
        2: {"name": "media", "confidence": 0.95},
    }
    out = speaker_map.apply_line_speakers(document, picks, {})
    assert [segment["speaker"] for segment in out["segments"]] == [
        "Ana",
        "Unknown",
        "Media",
        "Ana",
    ]


def test_the_owner_pick_wins_and_survives_a_rebuild(world):
    world.store.append_line_speaker_verdict(
        SOURCE, 1, "Teo", answered_through="phone page", created_by="t"
    )
    line_speakers.build(world.store, world.client(), SOURCE, created_by="t")
    verdicts = world.store.current_line_speaker_verdicts(SOURCE)
    assert verdicts == {1: "Teo"}
    document = {"segments": [{"idx": 1, "role": "Unknown", "speaker": ""}]}
    out = speaker_map.apply_line_speakers(document, world.store.line_speakers(SOURCE), verdicts)
    assert out["segments"][0]["speaker"] == "Teo"


def test_status_counts_named_and_unsure_and_names_what_changed(world):
    before = line_speakers.status(world.store, CONFIG, [SOURCE])[0]
    assert (before.state, before.unidentified, before.named) == ("none", 4, 0)
    line_speakers.build(world.store, world.client(), SOURCE, created_by="t")
    world.store.append_line_speaker_verdict(
        SOURCE, 2, "Teo", answered_through="phone page", created_by="t"
    )
    after = line_speakers.status(world.store, CONFIG, [SOURCE])[0]
    assert (after.state, after.named, after.unsure, after.owner) == ("current", 3, 1, 1)
    changed = json.loads(json.dumps(CONFIG))
    changed["line_speakers"]["household"].append({"name": "Mia", "role": "her daughter"})
    stale = line_speakers.status(world.store, changed, [SOURCE])[0]
    assert (stale.state, stale.reason) == ("stale", "household changed")


def test_the_estimate_reads_only_the_cache(world):
    first = line_speakers.estimate(world.store, world.client(), [SOURCE])
    assert (first.sources, first.calls, first.lines, first.from_cache) == (1, 2, 4, 0)
    assert first.tokens_out == 4 * line_speakers.OUTPUT_TOKENS_PER_LINE
    assert world.store.line_speakers(SOURCE) == {}
    line_speakers.build(world.store, world.client(), SOURCE, created_by="t")
    again = line_speakers.estimate(world.store, world.client(), [SOURCE])
    assert again.sources == 0  # current: nothing to ask


def test_settings_need_a_model_and_the_owner():
    with pytest.raises(llm.NotConfigured):
        line_speakers.settings({"line_speakers": {"owner": "Ana"}})
    with pytest.raises(llm.NotConfigured):
        line_speakers.settings({"line_speakers": {"model": MODEL}})
    found = line_speakers.settings(CONFIG)
    assert line_speakers.household_text(found) == (
        "- Ana; the owner, a mother.\n- Teo; her son; may appear as Leo."
    )


def test_the_settings_file_takes_a_line_speakers_table(tmp_path):
    path = tmp_path / "review.toml"
    path.write_text(
        f'database = "{tmp_path / "t.sqlite"}"\n'
        f'[kinds.bee]\ndirectory = "{tmp_path}"\n'
        "[line_speakers]\n"
        f'model = "{MODEL}"\nowner = "Ana"\n'
        '[[line_speakers.household]]\nname = "Ana"\nrole = "the owner"\n'
    )
    loaded = review_settings.load(path)
    assert loaded.client_config()["line_speakers"]["household"] == [
        {"name": "Ana", "role": "the owner"}
    ]
    path.write_text(
        f'database = "{tmp_path / "t.sqlite"}"\n[kinds.bee]\ndirectory = "{tmp_path}"\n'
        '[line_speakers]\nmodel = "m"\nowner = "A"\nx = 1\n'
    )
    with pytest.raises(review_settings.SettingsError):
        review_settings.load(path)


def test_a_version_15_database_gains_the_line_speaker_tables_after_a_verified_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    first = db.Database(path)
    Store(first).add_source(SOURCE, "l", created_by="t", kind="bee")
    first.close()
    connection = sqlite3.connect(path)
    for statement in (
        "DROP VIEW vw_current_line_speaker_verdicts",
        "DROP TRIGGER line_speaker_verdicts_no_update",
        "DROP TRIGGER line_speaker_verdicts_no_delete",
        "DROP TABLE line_speaker_verdicts",
        "DROP TABLE line_speakers",
        "PRAGMA user_version = 15",
    ):
        connection.execute(statement)
    connection.commit()
    connection.close()
    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 15 and opened.migration["to"] == db.USER_VERSION == 17
        assert opened.migration["backup"].endswith("-before-v17.sqlite")
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        store = Store(opened)
        store.append_line_speaker_verdict(
            SOURCE, 0, "Ana", answered_through="phone page", created_by="t"
        )
        with pytest.raises(sqlite3.IntegrityError):
            opened.execute("DELETE FROM line_speaker_verdicts")
    finally:
        opened.close()


def test_a_stage_timeout_reaches_the_vendor_request(tmp_path, monkeypatch):
    seen = []

    class Transport(llm.LiteLLMTransport):
        def send(self, model, messages, params, *, tag):
            seen.append(params.get("timeout"))
            return llm.RawReply(
                '{"lines": [{"index": 1, "who": "Ana", "confidence": 1}]}', 1, 1, 0.0
            )

    config = json.loads(json.dumps(CONFIG))
    config["line_speakers"]["timeout_seconds"] = 900
    client = llm.Client(tmp_path / "ws", config, transport=Transport())
    variables = {"owner": "Ana", "household": "", "evidence": "", "summary": "", "transcript": "",
                 "first": "1", "last": "1"}  # fmt: skip
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    client.complete(line_speakers.STAGE, line_speakers.prompt(), variables, tag="t")
    assert seen == [900.0]


def test_a_corpus_chooses_its_members_unless_sources_are_named(world):
    from corpuslm import cli

    corpus_id = world.store.add_corpus({"name": "Focus"}, created_by="t")
    world.store.add_member(corpus_id, SOURCE, "author", created_by="t")
    world.store.add_source("bee:2", "l", created_by="t", kind="bee")
    assert cli._chosen_sources(world.store, None, None, "Focus") == [SOURCE]
    assert cli._chosen_sources(world.store, None, ["bee:2"], "Focus") == ["bee:2"]
    assert sorted(cli._chosen_sources(world.store, "bee", None, None)) == [SOURCE, "bee:2"]
    with pytest.raises(ValueError):
        cli._chosen_sources(world.store, None, None, "Nope")


def test_a_page_pick_lands_as_a_verdict_and_names_the_indexed_line(world):
    world.store.replace_cleaned_segments(SOURCE, SEGMENTS)
    found = line_speakers.settings(CONFIG)
    out = line_speakers.pick_line(world.store, found, SOURCE, 2, "media", created_by="t")
    assert out["indexed"] == 1
    assert world.store.current_line_speaker_verdicts(SOURCE) == {2: "media"}
    shown = {s["idx"]: s["speaker"] for s in world.store.best_segments(SOURCE)}
    assert shown[2] == "Media"
    lines = line_speakers.span_lines(world.store, found, SOURCE, 1, 3)
    assert [(line["idx"], line["unidentified"], line["pick"]) for line in lines] == [
        (1, True, ""), (2, True, "media"), (3, False, "")
    ]  # fmt: skip
    with pytest.raises(ValueError):
        line_speakers.pick_line(world.store, found, SOURCE, 2, "Mallory", created_by="t")
    assert line_speakers.choices(found)[:2] == [("Ana", "Ana"), ("Teo", "Teo")]
