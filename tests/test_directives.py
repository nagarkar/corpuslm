"""Directives (`src/corpuslm/directives.py` with the Bee claims prompt): the form rule, Jev's
gate, the per-kind prompt, the verdicts that survive a rebuild, and the passes that never see a
directive. Synthetic text only; no model or judge is ever called."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from llm_support import client as llm_client
from llmlm import llm
from test_judge import RecordingTransport, noul_reply

from corpuslm import claims, concepts, db, directives, form, judge, moments, prompts
from corpuslm.store import Store

MODEL = "gemini/gemini-2.5-flash-lite"
SOURCE = "bee:9"
VIDEO = "Xq3vT9bLk2A"
SEGMENTS = [
    {"idx": 0, "t0": 0.0, "t1": 1.0, "speaker": "Quill", "text": "a choice is right as of now"},
    {"idx": 1, "t0": 1.0, "t1": 2.0, "speaker": "Quill",
     "text": "Computer, remind me to call Wren"},
    {"idx": 2, "t0": 2.0, "t1": 3.0, "speaker": "Quill", "text": "I should buy milk on the way"},
    {"idx": 3, "t0": 3.0, "t1": 4.0, "speaker": "Quill", "text": "one could say go for it"},
]  # fmt: skip
REPLY = {
    "claims": [
        {"statement": "A choice is right only as of the moment it is made.", "speaker": "Quill",
         "segments": [0], "kind": "assertion"},
        {"statement": "Remind the owner to call Wren.", "speaker": "Quill", "segments": [1],
         "kind": "directive", "addressee": "computer",
         "heard": "Computer, remind me to call Wren", "else_kind": "assertion"},
        {"statement": "Buy milk on the way home.", "speaker": "Quill", "segments": [2],
         "kind": "directive", "addressee": "self", "heard": "I should buy milk on the way",
         "else_kind": "prediction"},
        {"statement": "One could say go for it.", "speaker": "Quill", "segments": [3],
         "kind": "directive", "addressee": "self", "heard": "go for it",
         "else_kind": "assertion"},
    ]
}  # fmt: skip


# What the Bee prompt (version 2) replies: each directive says on what basis it is one, and the
# gate, not the model, decides the addressee.
GUIDED_SEGMENTS = [
    {"idx": 0, "t0": 0.0, "t1": 1.0, "speaker": "Quill",
     "text": "this is a poem that we should further explore and write down"},
    {"idx": 1, "t0": 1.0, "t1": 2.0, "speaker": "Unknown",
     "text": "there should be a chapter on that"},
    {"idx": 2, "t0": 2.0, "t1": 3.0, "speaker": "Quill", "text": "look up the Gaudapada verse"},
    {"idx": 3, "t0": 3.0, "t1": 4.0, "speaker": "Quill", "text": "I will call Wren tomorrow"},
    {"idx": 4, "t0": 4.0, "t1": 5.0, "speaker": "Quill", "text": "Computer, set a timer"},
]  # fmt: skip
GUIDED_REPLY = {
    "claims": [
        {"statement": "Explore the poem further and write it down.", "speaker": "Quill",
         "segments": [0], "kind": "directive", "basis": "guidance",
         "heard": "we should further explore and write down", "else_kind": "assertion"},
        {"statement": "Add a chapter on that.", "speaker": "Unknown", "segments": [1],
         "kind": "directive", "basis": "guidance", "heard": "there should be a chapter on that",
         "else_kind": "assertion"},
        {"statement": "Look up the Gaudapada verse.", "speaker": "Quill", "segments": [2],
         "kind": "directive", "basis": "instruction", "heard": "look up the Gaudapada verse",
         "else_kind": "assertion"},
        {"statement": "The owner will call Wren tomorrow.", "speaker": "Quill", "segments": [3],
         "kind": "directive", "basis": "commitment", "heard": "I will call Wren tomorrow",
         "else_kind": "prediction"},
        {"statement": "Set a timer.", "speaker": "Quill", "segments": [4], "kind": "directive",
         "basis": "wake_word", "heard": "Computer, set a timer", "else_kind": "assertion"},
    ]
}  # fmt: skip


class World:
    def __init__(self, tmp_path: Path, *, threshold: float | None = None):
        self.root = tmp_path / "ws"
        self.scripts = tmp_path / "scripts"
        self.database = db.Database(tmp_path / "transcript.sqlite")
        self.store = Store(self.database)
        self.store.add_source(SOURCE, "l", created_by="test", kind="bee")
        self.store.update_source(SOURCE, {"title": "A walk", "published_at": "2026-10-20"})
        self.store.replace_segments(SOURCE, SEGMENTS)
        section: dict = {"model": MODEL, "section_chars": 10_000, "owner_first_name": "Quill"}
        if threshold is not None:
            section["directive_threshold"] = threshold
        self.config = {"claims": section, "llm": {}}
        folder = self.scripts / claims.STAGE
        folder.mkdir(parents=True)
        (folder / f"{SOURCE}-s0.json").write_text(
            json.dumps({"reply": json.dumps(REPLY), "tokens_in": 100, "tokens_out": 20})
        )

    def client(self) -> llm.Client:
        return llm_client(
            self.root,
            self.config,
            transport=llm.ScriptedTransport(self.scripts),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

    def judge(self, probabilities: list[float]) -> judge.Judge:
        made, _ = judge.make(
            self.config, self.client(), RecordingTransport([noul_reply(p) for p in probabilities])
        )
        assert made is not None
        return made

    def form(self, label: str) -> None:
        form.rule(self.store, SOURCE, label, created_by="owner")

    def guided(self) -> None:
        """The source speaks in guidance, and the model replies with each directive's basis."""
        self.store.replace_segments(SOURCE, GUIDED_SEGMENTS)
        (self.scripts / claims.STAGE / f"{SOURCE}-s0.json").write_text(
            json.dumps({"reply": json.dumps(GUIDED_REPLY), "tokens_in": 100, "tokens_out": 20})
        )

    def kept(self) -> list[tuple]:
        rows = self.store.claims(SOURCE, include_directives=True)
        return [(row["kind"], row["addressee"]) for row in rows]


@pytest.fixture
def world(tmp_path):
    made = World(tmp_path)
    yield made
    made.database.close()


def test_a_bee_source_uses_the_bee_prompt_and_a_video_the_plain_one():
    assert claims.prompt("bee").id == "claims_bee" and claims.prompt("youtube").id == "claims"
    assert claims.prompt("vimeo").id == claims.prompt("text").id == "claims"
    assert claims.prompt().id == "claims"  # the default callers had
    bee = prompts.load("claims_bee")
    assert {"form", "owner"} <= set(bee.placeholders())
    assert "form" not in prompts.load("claims").placeholders()
    assert bee.output_schema == "claims_reply" and bee.config_section == "claims"


def test_a_monologue_candidate_below_the_threshold_keeps_its_ordinary_kind(world):
    world.form("monologue")
    made = world.judge([0.95, 0.7, 0.1])  # one question per candidate, in order
    out = claims.build(world.store, world.client(), SOURCE, created_by="test", judge=made)
    assert (out["claims"], out["directives"], out["directives_demoted"], out["gate"]) == (
        4, 2, 1, "jev",
    )  # fmt: skip
    rows = world.store.claims(SOURCE, include_directives=True)
    assert [(row["kind"], row["addressee"], row["heard"]) for row in rows] == [
        ("assertion", None, None),
        ("directive", "computer", "Computer, remind me to call Wren"),
        ("directive", "self", "I should buy milk on the way"),
        ("assertion", None, None),  # 0.1 < 0.6: kept under its else_kind
    ]
    bodies = [body for _, body in made.transport.bodies]
    assert [body["state"]["utterance"] for body in bodies] == [
        "Computer, remind me to call Wren", "I should buy milk on the way", "go for it",
    ]  # fmt: skip
    assert bodies[0]["questions"]["acted_on"]["type"] == "noul"
    provenance = json.loads(world.store.artifact(SOURCE, claims.NODE)["provenance"])
    assert provenance["form"] == "monologue" and provenance["directives"]["asked"] == 3
    assert out["cost_usd"] > 0  # the model read plus the three Jev questions


def test_the_threshold_is_read_from_config(tmp_path):
    world = World(tmp_path, threshold=0.75)
    try:
        world.form("monologue")
        made = world.judge([0.95, 0.7, 0.1])
        out = claims.build(world.store, world.client(), SOURCE, created_by="test", judge=made)
        assert (out["directives"], out["directives_demoted"]) == (1, 2)
        assert claims.settings(world.config)["directive_threshold"] == 0.75
        assert claims.settings({"claims": {"model": MODEL}})["directive_threshold"] == 0.6
    finally:
        world.database.close()


def test_in_company_only_an_addressed_utterance_is_a_directive(world):
    world.form("dialogue")
    made = world.judge([])  # never asked
    out = claims.build(world.store, world.client(), SOURCE, created_by="test", judge=made)
    assert (out["directives"], out["directives_demoted"], out["gate"]) == (1, 2, "wake word")
    assert made.transport.bodies == []
    rows = world.store.claims(SOURCE, include_directives=True)
    assert [row["kind"] for row in rows] == ["assertion", "directive", "prediction", "assertion"]
    assert rows[1]["addressee"] == "computer"


def test_in_a_monologue_guidance_and_plain_instructions_are_for_computer(world):
    world.guided()
    world.form("monologue")
    made = world.judge([0.9, 0.8, 0.9, 0.9, 0.9])  # Jev checks every candidate
    out = claims.build(world.store, world.client(), SOURCE, created_by="test", judge=made)
    assert (out["directives"], out["directives_demoted"], out["gate"]) == (5, 0, "jev")
    assert world.kept() == [
        ("directive", "computer"),  # "we should further explore and write down"
        ("directive", "computer"),  # "there should be a chapter on that", on an Unknown line
        ("directive", "computer"),  # a plain instruction, no wake word
        ("directive", "self"),  # a first-person promise stays a note to the owner
        ("directive", "computer"),  # the wake word
    ]
    state = made.transport.bodies[0][1]["state"]
    assert (state["basis"], state["form"]) == ("guidance", "monologue")


def test_in_company_guidance_is_for_computer_and_a_plain_instruction_is_a_claim(world):
    world.guided()
    world.form("dialogue")
    made = world.judge([0.9, 0.2])  # Jev is asked about the two guidance lines only
    out = claims.build(world.store, world.client(), SOURCE, created_by="test", judge=made)
    assert (out["directives"], out["directives_demoted"], out["gate"]) == (2, 3, "jev")
    assert world.kept() == [
        ("directive", "computer"),  # guidance Jev kept
        ("assertion", None),  # guidance Jev left out: its ordinary kind
        ("assertion", None),  # a plain instruction is said to the person in the room
        ("prediction", None),  # so is a promise
        ("directive", "computer"),  # the wake word, not checked
    ]
    assert [body["state"]["utterance"] for _, body in made.transport.bodies] == [
        "we should further explore and write down", "there should be a chapter on that",
    ]  # fmt: skip


def test_in_a_media_recording_only_the_wake_word_is_a_directive(world):
    world.guided()
    world.form("media")
    made = world.judge([])  # never asked
    out = claims.build(world.store, world.client(), SOURCE, created_by="test", judge=made)
    assert (out["directives"], out["directives_demoted"], out["gate"]) == (1, 4, "wake word")
    assert world.kept()[-1] == ("directive", "computer") and made.transport.bodies == []


def test_the_gate_not_the_model_decides_the_addressee(world):
    # a reply that calls guidance a note to self is overruled by its basis
    claim = GUIDED_REPLY["claims"][0] | {"text": "x", "addressee": "self"}
    (kept,), stats = directives.gate(None, [claim], form="monologue", threshold=0.6, tag_prefix="t")
    assert (kept["kind"], kept["addressee"], stats["gate"]) == ("directive", "computer", "off")
    assert "basis" not in kept


def test_the_bee_prompt_asks_for_the_basis_and_reads_unknown_lines_as_the_owners():
    for prompt_id in ("claims_bee", claims.RETRY_PROMPT_ID):
        prompt = prompts.load(prompt_id)
        assert prompt.version == 2
        for word in ('"guidance"', '"instruction"', '"commitment"', '"wake_word"',
                     "we should", "the next step is", "there should be", "Unknown"):  # fmt: skip
            assert word in prompt.body, (prompt_id, word)


def test_without_jev_monologue_candidates_stand_and_the_pass_says_so(world):
    world.form("monologue")
    out = claims.build(world.store, world.client(), SOURCE, created_by="test", judge=None)
    assert (out["directives"], out["directives_demoted"], out["gate"]) == (3, 0, "off")
    assert (
        json.loads(world.store.artifact(SOURCE, claims.NODE)["provenance"])["directives"]["gate"]
        == "off"
    )


def test_an_unclassified_bee_source_is_read_as_company(world):
    out = claims.build(world.store, world.client(), SOURCE, created_by="test", judge=None)
    assert (out["directives"], out["gate"]) == (1, "wake word")
    inputs = json.loads(world.store.artifact(SOURCE, claims.NODE)["inputs"])
    assert inputs["form"] == "unknown"
    # classifying the source is a change of input: the claims read stale
    world.form("monologue")
    (entry,) = claims.status(world.store, world.config, [SOURCE])
    assert (entry.state, entry.reason) == ("stale", "form changed")


def test_the_form_and_owner_reach_the_bee_prompt(world):
    world.form("monologue")
    client = world.client()
    claims.build(world.store, client, SOURCE, created_by="test", judge=None)
    cached = next(client.cache_dir.glob("*.json")).read_text()
    assert "FORM: monologue" in cached and "OWNER: Quill" in cached


def test_a_rebuild_keeps_the_verdict_on_the_same_directive(world):
    world.form("dialogue")
    claims.build(world.store, world.client(), SOURCE, created_by="test")
    (row,) = world.store.directives(SOURCE)
    out = directives.verdict(
        world.store, row["id"], "confirm", answered_through="phone page", created_by="owner"
    )
    assert out == {"id": row["id"], "verdict": "confirm"}
    with pytest.raises(ValueError):
        directives.verdict(world.store, row["id"], "maybe", answered_through="p", created_by="o")
    with pytest.raises(KeyError):
        directives.verdict(world.store, "clm_nope", "confirm", answered_through="p", created_by="o")
    # the rebuild: new claim ids, the same words as heard, the verdict in force unchanged
    world.store.mark_stale(SOURCE, claims.NODE)
    claims.build(world.store, world.client(), SOURCE, created_by="test")
    (again,) = world.store.directives(SOURCE)
    assert again["id"] != row["id"]
    (listed,) = directives.listing(world.store, source_id=SOURCE)
    assert (listed["state"], listed["heard"], listed["addressee"], listed["start_seconds"]) == (
        "confirm", "Computer, remind me to call Wren", "computer", 1.0,
    )  # fmt: skip
    assert listed["title"] == "A walk" and listed["date"] == "2026-10-20"
    assert listed["text"] == "Remind the owner to call Wren."
    # the ruling is append-only: a change is a new row, the newest in force
    directives.verdict(
        world.store, again["id"], "decline", answered_through="phone page", created_by="owner"
    )
    assert directives.listing(world.store, state="decline")[0]["id"] == again["id"]
    assert directives.listing(world.store, state="open") == []
    assert directives.summary(world.store) == {
        "directives": 1,
        "states": {"confirm": 0, "decline": 1, "open": 0},
        "addressees": {"computer": 1, "self": 0},
    }


def test_the_subject_is_the_source_the_words_and_the_addressee():
    same = directives.subject_sha256("bee:1", "Computer, remind me", "computer")
    assert same == directives.subject_sha256("bee:1", "computer remind me!", "computer")
    assert same != directives.subject_sha256("bee:1", "Computer, remind me", "self")
    assert same != directives.subject_sha256("bee:2", "Computer, remind me", "computer")


def test_the_other_passes_never_see_a_directive(world):
    world.form("monologue")
    claims.build(world.store, world.client(), SOURCE, created_by="test", judge=None)
    assert world.store.claim_count(SOURCE) == 4
    assert [row["kind"] for row in world.store.claims(SOURCE)] == ["assertion"]
    assert len(world.store.claims(SOURCE, include_directives=True)) == 4
    # the concept and moment passes read `Store.claims`: one claim is all they would send
    config = world.config | {
        "concepts": {"model": MODEL, "min_sections": 1, "min_claims": 1, "batch_claims": 100},
        "moments": {"model": MODEL, "max_moments": 3},
    }
    assert concepts._batches(world.store.claims(SOURCE), 100) == [world.store.claims(SOURCE)]
    variables = moments._variables(world.store.source(SOURCE), world.store.claims(SOURCE), 3)
    assert "Remind the owner" not in variables["claims"] and "A choice" in variables["claims"]
    del config


def test_a_video_source_gets_no_directive_fields(tmp_path):
    database = db.Database(tmp_path / "t.sqlite")
    store = Store(database)
    try:
        store.add_source(VIDEO, "https://www.youtube.com/watch?v=" + VIDEO, created_by="t")
        store.replace_segments(VIDEO, SEGMENTS)
        scripts = tmp_path / "scripts" / claims.STAGE
        scripts.mkdir(parents=True)
        (scripts / f"{VIDEO}-s0.json").write_text(
            json.dumps({"reply": json.dumps(REPLY), "tokens_in": 100, "tokens_out": 20})
        )
        client = llm_client(
            tmp_path / "ws",
            {"claims": {"model": MODEL, "section_chars": 10_000}, "llm": {}},
            transport=llm.ScriptedTransport(tmp_path / "scripts"),
        )
        out = claims.build(store, client, VIDEO, created_by="test")
        assert (out["directives"], out["gate"]) == (0, "none")
        kinds = [row["kind"] for row in store.claims(VIDEO, include_directives=True)]
        assert kinds == ["assertion", "directive", "directive", "directive"]  # as the model said
        assert "form" not in json.loads(store.artifact(VIDEO, claims.NODE)["inputs"])
    finally:
        database.close()
