"""Form classification (`src/corpuslm/form.py`): the count proposes, Jev decides, both are
recorded, the owner's ruling wins. Synthetic text only; no judge is ever called."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_judge import RecordingTransport, choice_reply

from corpuslm import db, form, judge, llm
from corpuslm.store import Store

SOURCE = "bee:7"
SEGMENTS = [
    {"idx": 0, "t0": 0.0, "t1": 1.0, "speaker": "Quill", "text": "remind me to call Wren"},
    {"idx": 1, "t0": 1.0, "t1": 2.0, "speaker": "Unknown", "text": "(a door)"},
    {"idx": 2, "t0": 2.0, "t1": 3.0, "speaker": "Media", "text": "and now the news"},
    {"idx": 3, "t0": 3.0, "t1": 4.0, "speaker": "Noise", "text": "..."},
    {"idx": 4, "t0": 4.0, "t1": 5.0, "speaker": "Quill", "text": "I will do it tomorrow"},
]


@pytest.fixture
def store(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    made = Store(database)
    made.add_source(SOURCE, "l", created_by="test", kind="bee")
    made.replace_segments(SOURCE, SEGMENTS)
    yield made
    database.close()


def judge_with(tmp_path, replies):
    client = llm.Client(tmp_path, {"llm": {}}, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    made, _ = judge.make({}, client, RecordingTransport(replies))
    return made


def test_the_count_leaves_out_the_labels_that_name_nobody():
    assert form.named_speakers(SEGMENTS) == ["Quill"]
    assert form.propose(SEGMENTS) == (1, "monologue")
    two = SEGMENTS + [{"idx": 5, "t0": 5, "t1": 6, "speaker": "Wren", "text": "yes"}]
    assert form.propose(two) == (2, "dialogue")
    three = two + [{"idx": 6, "t0": 6, "t1": 7, "speaker": "Teo", "text": "no"}]
    assert form.propose(three) == (3, "group")
    assert form.propose([]) == (0, "monologue")
    unnamed = [{"idx": 0, "t0": 0, "t1": 1, "speaker": label, "text": "x"} for label in (
        "unclear", "noise", "media", "other", "Someone else", "Speaker 2", "",
    )]  # fmt: skip
    assert form.named_speakers(unnamed) == []


def test_the_count_proposes_jev_decides_and_a_disagreement_is_a_card(store, tmp_path):
    made = judge_with(tmp_path, [choice_reply("media", {"media": 0.8, "monologue": 0.2})])
    record = form.classify(store, made, SOURCE, created_by="test")
    assert (record["count"], record["proposed"], record["jev"], record["label"]) == (
        1, "monologue", "media", "media",
    )  # fmt: skip
    assert record["disagree"] and record["cost_usd"] > 0 and not record["cached"]
    stored = store.source_form(SOURCE)
    assert stored is not None and stored["label"] == "media" and stored["jev"] == "media"
    assert stored["jev_probabilities"] == {
        "monologue": 0.2, "dialogue": 0.0, "group": 0.0, "media": 0.8,
    }  # fmt: skip
    (card,) = form.cards(store, [SOURCE])
    assert (card["source_id"], card["proposed"], card["jev"]) == (SOURCE, "monologue", "media")
    assert form.label_of(store, SOURCE) == "media"
    # what Jev was shown: the count, the proposal, the named speakers and sampled turns
    body = made.transport.bodies[0][1]
    assert body["state"]["speaker_count"] == 1 and body["state"]["proposed"] == "monologue"
    assert body["state"]["named_speakers"] == ["Quill"] and len(body["state"]["turns"]) == 5
    assert set(body["questions"]["form"]["criteria"]) == set(form.LABELS)


def test_an_agreement_is_no_card(store, tmp_path):
    made = judge_with(tmp_path, [choice_reply("monologue", {"monologue": 0.9})])
    record = form.classify(store, made, SOURCE, created_by="test")
    assert record["label"] == "monologue" and not record["disagree"]
    assert form.cards(store, [SOURCE]) == []


def test_without_jev_the_counts_label_stands(store):
    record = form.classify(store, None, SOURCE, created_by="test")
    assert (record["jev"], record["label"], record["disagree"]) == (None, "monologue", False)
    assert record["cost_usd"] == 0.0 and store.source_form(SOURCE)["label"] == "monologue"


def test_the_owners_ruling_stands_through_a_reclassification(store, tmp_path):
    form.classify(store, None, SOURCE, created_by="test")
    assert form.rule(store, SOURCE, "dialogue", created_by="owner")["label"] == "dialogue"
    with pytest.raises(ValueError):
        form.rule(store, SOURCE, "song", created_by="owner")
    made = judge_with(tmp_path, [choice_reply("media", {"media": 0.8})])
    record = form.classify(store, made, SOURCE, created_by="test")
    assert (record["jev"], record["owner"], record["label"]) == ("media", "dialogue", "dialogue")
    assert not record["disagree"] and form.cards(store, [SOURCE]) == []
    assert form.label_of(store, SOURCE) == "dialogue"


def test_an_unclassified_source_has_no_label(store):
    assert store.source_form(SOURCE) is None and form.label_of(store, SOURCE) is None
    assert form.cards(store, [SOURCE]) == []
