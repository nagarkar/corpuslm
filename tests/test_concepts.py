"""The concept pass (`src/corpuslm/concepts.py`): labels normalized and reused, the gate and the
boundary, a verdict that wins, co-occurrence edges with their evidence, staleness, the estimate
that calls no model, and the v11 -> v12 step. Synthetic text only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from corpuslm import claims, concepts, db, llm
from corpuslm.store import Store

MODEL = "gemini/gemini-2.5-flash-lite"
CONFIG = {
    "claims": {"model": MODEL, "section_chars": 6000},
    "concepts": {"model": MODEL, "min_sections": 2, "min_claims": 2, "batch_claims": 2},
    "llm": {},
}
SEGMENTS = [
    {
        "idx": index,
        "t0": float(index),
        "t1": float(index + 1),
        "speaker": "",
        "text": f"line {index}",
    }
    for index in range(4)
]


def _claims(texts: list[str], sections: list[int]) -> list[dict]:
    return [
        {"section": section, "segments": [index], "text": text, "kind": "assertion"}
        for index, (text, section) in enumerate(zip(texts, sections, strict=True))
    ]


class World:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "ws"
        self.scripts = tmp_path / "scripts"
        self.database = db.Database(tmp_path / "transcript.sqlite")
        self.store = Store(self.database)
        for source_id, kind in (("bee:1", "bee"), ("bee:2", "bee")):
            self.store.add_source(source_id, "l", created_by="test", kind=kind)
            self.store.replace_segments(source_id, SEGMENTS)
        self.store.replace_claims(
            "bee:1",
            _claims(["Time counts beats.", "Rank is a state.", "Choice comes first.", "A stray."],
                    [0, 0, 1, 1]),
            build_key="k1", created_by="test",
        )  # fmt: skip
        self.store.record_artifact(
            "bee:1", claims.NODE, path="", sha256="k1", state="built",
            inputs={"text": "t"}, provenance={}, created_by="test",
        )  # fmt: skip
        self.store.replace_claims(
            "bee:2", _claims(["Beats again.", "Choice again."], [0, 1]),
            build_key="k2", created_by="test",
        )  # fmt: skip
        self.store.record_artifact(
            "bee:2", claims.NODE, path="", sha256="k2", state="built",
            inputs={"text": "t2"}, provenance={}, created_by="test",
        )  # fmt: skip

    def script(self, tag: str, entries: list[tuple[int, list[str]]], tokens=(100, 20)) -> None:
        folder = self.scripts / concepts.STAGE
        folder.mkdir(parents=True, exist_ok=True)
        reply = {"claims": [{"index": index, "concepts": labels} for index, labels in entries]}
        (folder / f"{tag}.json").write_text(
            json.dumps(
                {"reply": json.dumps(reply), "tokens_in": tokens[0], "tokens_out": tokens[1]}
            )
        )

    def client(self, config: dict | None = None) -> llm.Client:
        return llm.client(
            self.root,
            config or CONFIG,
            transport=llm.ScriptedTransport(self.scripts),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )


@pytest.fixture
def world(tmp_path):
    made = World(tmp_path)
    made.script("bee:1-c0", [(0, ["Beat", "time"]), (1, ["rank"])])
    made.script("bee:1-c2", [(0, ["choice", "beat"]), (1, [])])
    made.script("bee:2-c0", [(0, ["beats"]), (1, ["Choice", "choice"])])
    yield made
    made.database.close()


def test_normalize_folds_case_punctuation_and_spacing():
    assert concepts.normalize("  The Beat!  ") == "the beat"
    assert concepts.normalize("Re-earned rank") == "re-earned rank"
    assert concepts.normalize("...") == ""


def test_build_tags_claims_reuses_labels_and_counts_by_section(world):
    out = concepts.build(world.store, world.client(), "bee:1", created_by="test")
    assert (out["batches"], out["claims"], out["tags"], out["new_labels"]) == (2, 4, 5, 4)
    assert out["cost_usd"] > 0 and out["cached"] is False
    beat = world.store.concept("beat")
    assert beat["label"] == "Beat" and beat["alternates"] == ["beat"]
    assert (beat["section_count"], beat["claim_count"], beat["source_count"]) == (2, 2, 1)
    assert world.store.claim_concept_keys(world.store.claims("bee:1")[0]["id"]) == ["beat", "time"]
    assert world.store.claim_concept_keys(world.store.claims("bee:1")[3]["id"]) == []
    (entry,) = concepts.status(world.store, CONFIG, ["bee:1"])
    assert entry.state == "current"
    again = concepts.build(world.store, world.client(), "bee:2", created_by="test")
    assert again["new_labels"] == 1  # "beats" is new to the key set; "Choice" reuses "choice"
    assert world.store.concept("choice")["alternates"] == ["Choice"]
    assert world.store.concept("choice")["source_count"] == 2


def test_in_out_and_boundary_follow_the_gate_and_a_verdict_wins(world):
    concepts.build(world.store, world.client(), "bee:1", created_by="test")
    concepts.build(world.store, world.client(), "bee:2", created_by="test")
    states = concepts.states(world.store, CONFIG)
    # beat: 2 sections, 2 claims -> boundary (min_sections 2 .. 3); rank: 1 section -> out
    assert states == {"beat": "boundary", "time": "out", "rank": "out", "choice": "boundary",
                      "beats": "out"}  # fmt: skip
    wider = CONFIG | {"concepts": CONFIG["concepts"] | {"min_sections": 0, "min_claims": 1}}
    assert concepts.states(world.store, wider)["beat"] == "in"  # a gate change is read at once
    assert concepts.states(world.store, wider)["rank"] == "boundary"
    concepts.verdict(world.store, "Beat", "out", answered_through="review file", created_by="t")
    assert concepts.states(world.store, CONFIG)["beat"] == "out"
    concepts.verdict(world.store, "beat", "in", answered_through="phone page", created_by="t")
    assert concepts.states(world.store, CONFIG)["beat"] == "in"  # the newest ruling wins
    summary = concepts.summary(world.store, CONFIG)
    assert summary["states"] == {"in": 1, "out": 3, "boundary": 1} and summary["verdicts"] == 1
    assert [entry["key"] for entry in summary["boundary"]] == ["choice"]
    assert summary["sections_histogram"] == {1: 3, 2: 2}
    with pytest.raises(sqlite3.IntegrityError):  # append-only: never overwritten
        world.database.execute("UPDATE concept_verdicts SET verdict = 'out'")
    with pytest.raises(KeyError):
        concepts.verdict(world.store, "nope", "in", answered_through="review file", created_by="t")


def test_edges_are_shared_claim_counts_with_the_claims_as_evidence(world):
    concepts.build(world.store, world.client(), "bee:1", created_by="test")
    edges = concepts.edges(world.store, "Beat")
    assert [(edge["key"], edge["shared"]) for edge in edges] == [("choice", 1), ("time", 1)]
    first, third = world.store.claims("bee:1")[0]["id"], world.store.claims("bee:1")[2]["id"]
    assert {edge["key"]: edge["claim_ids"] for edge in edges} == {
        "time": [first],
        "choice": [third],
    }
    assert concepts.edges(world.store, "rank") == []


def test_the_vocabulary_shown_is_in_and_boundary_most_used_first(world):
    concepts.build(world.store, world.client(), "bee:1", created_by="test")
    wider = CONFIG | {"concepts": CONFIG["concepts"] | {"min_sections": 1, "min_claims": 1}}
    assert concepts.vocabulary(world.store, wider)[0] == "Beat"
    assert set(concepts.vocabulary(world.store, wider)) == {"Beat", "time", "rank", "choice"}
    assert concepts.vocabulary(world.store, CONFIG) == ["Beat"]  # boundary is shown; out is not


def test_rebuilt_claims_make_the_tags_stale_and_the_reason_says_so(world):
    concepts.build(world.store, world.client(), "bee:1", created_by="test")
    world.store.replace_claims("bee:1", _claims(["New."], [0]), build_key="k9", created_by="t")
    assert world.store.claim_concept_keys(world.store.claims("bee:1")[0]["id"]) == []  # cascaded
    world.store.record_artifact(
        "bee:1", claims.NODE, path="", sha256="k9", state="built",
        inputs={"text": "t9"}, provenance={}, created_by="test",
    )  # fmt: skip
    (entry,) = concepts.status(world.store, CONFIG, ["bee:1"])
    assert (entry.state, entry.reason) == ("stale", "claims changed")
    world.store.delete_claims("bee:1")
    (entry,) = concepts.status(world.store, CONFIG, ["bee:1"])
    assert entry.state == "no_claims"


def test_the_estimate_calls_no_model_and_the_cap_stops_before_the_call(tmp_path):
    world = World(tmp_path)  # no scripted replies
    try:
        estimate = concepts.estimate(world.store, world.client(), ["bee:1", "bee:2"])
        assert (estimate.sources, estimate.from_cache, estimate.calls) == (2, 0, 3)
        assert estimate.tokens_in > 0 and estimate.usd is not None and estimate.usd > 0
        assert not (world.root / "logs").exists()
        capped = world.client(CONFIG | {"llm": {"max_usd_per_run": 0.0}})
        with pytest.raises(llm.SpendCapReached):
            concepts.build(world.store, capped, "bee:1", created_by="test")
        assert world.store.concept_count() == 0
        with pytest.raises(ValueError, match="no claims"):
            world.store.delete_claims("bee:2")
            concepts.build(world.store, world.client(), "bee:2", created_by="test")
    finally:
        world.database.close()


def test_a_version_11_database_gains_the_concept_tables_after_a_verified_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    first = db.Database(path)
    Store(first).add_source("bee:1", "l", created_by="test", kind="bee")
    first.close()
    connection = sqlite3.connect(path)
    for statement in (
        "DROP VIEW vw_current_concept_verdicts",
        "DROP TRIGGER concept_verdicts_no_update",
        "DROP TRIGGER concept_verdicts_no_delete",
        "DROP TABLE concept_verdicts",
        "DROP TABLE claim_concepts",
        "DROP TABLE concepts",
        "PRAGMA user_version = 11",
    ):
        connection.execute(statement)
    connection.commit()
    connection.close()
    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 11 and opened.migration["to"] == db.USER_VERSION == 18
        assert opened.migration["backup"].endswith("-before-v18.sqlite")
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        store = Store(opened)
        assert store.concept_count() == 0
        made = store.upsert_concept("beat", "Beat", created_by="t")
        store.append_concept_verdict(made, "in", answered_through="review file", created_by="t")
        assert store.current_concept_verdicts() == {made: "in"}
        with pytest.raises(sqlite3.IntegrityError):  # the append-only trigger came with it
            opened.execute("DELETE FROM concept_verdicts")
    finally:
        opened.close()
