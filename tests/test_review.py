"""Review (corpuslm.review): verdicts, the correction memory and the notes round trip, for a
source kind with no link (Bee) and one with a link."""

from __future__ import annotations

import pytest
from review_kinds import (
    BEE_KIND,
    BEE_SOURCE,
    LINKED_KIND,
    OWNER,
    ambiguity,
    bee_speaker_map,
    bee_transcript,
)

from corpuslm import db, review
from corpuslm.store import Store

SOURCE = {"id": BEE_SOURCE}


@pytest.fixture
def database(tmp_path):
    d = db.Database(tmp_path / "transcript.sqlite")
    Store(d).add_source(BEE_SOURCE, "transcripts/a.md", created_by="test", kind="bee")
    yield d
    d.close()


def test_a_kind_without_links_renders_time_labels_only():
    lines = review.render_review(
        bee_transcript(), bee_speaker_map(), source_row=SOURCE, kind=BEE_KIND
    )
    text = "\n".join(lines)
    assert '> [!question] amb-0001 · 0:05 · heard "wren"' in text
    assert "](" not in text and "file://" not in text
    # the 'Unknown' speaker is a review item, offered the names heard and the names the map
    # carries, never its candidates
    assert "> [!question] spk-1 · who is Unknown? Heard: Ren (Ren, are you there?)" in text
    assert text.index("> - [ ] Ren\n") < text.index(f"> - [ ] {OWNER}\n", text.index("spk-1"))
    assert "Summary" not in text and "Kiln Day" not in text and "> - [ ] other: ___" in text
    # the owner's label resolved by default is not asked about
    assert f"who is {OWNER}?" not in text


def test_a_kind_with_links_links_the_time_label():
    lines = review.render_review(
        bee_transcript(), bee_speaker_map(), source_row=SOURCE, kind=LINKED_KIND
    )
    assert f"[0:05](https://example.test/{BEE_SOURCE}?t=5s)" in "\n".join(lines)


def test_resolved_items_move_to_a_collapsed_callout():
    done = ambiguity("amb-0001", 1, "wren", "Wren", ["Wren"], status="resolved", resolution="Ren")
    lines = review.render_review(
        bee_transcript(ambiguities=[done]),
        bee_speaker_map(unknown_status="AUTHOR", unknown_name="Wren"),
        source_row=SOURCE,
        kind=BEE_KIND,
    )
    text = "\n".join(lines)
    assert "_No open items._" in text and "> [!done]- Resolved" in text
    assert '> - amb-0001 → "Ren"' in text and '> - Unknown → "Wren" (owner)' in text


def test_nothing_to_review_says_so():
    assert review.render_review(None, None, source_row=SOURCE, kind=BEE_KIND) == [
        "_No review items._",
        "",
    ]


def test_two_boxes_checked_are_reported_by_key_and_left_alone():
    note = "\n".join(
        [
            '> [!question] amb-0001 · 0:05 · heard "wren"',
            "> We should call **Wren**",
            "> - [x] Wren",
            "> - [x] Ren",
            "> - [ ] other: ___",
            "",
            '> [!question] amb-0002 · 00:09 · heard "kiln"',
            "> - [ ] Kiln",
            "> - [x] other: Kilns",
            "",
            "> [!question] spk-1 · who is Unknown?",
            "> - [x] skip",
        ]
    )
    parsed = review.parse_note(note)
    assert parsed["ambiguous"] == ["amb-0001"]
    assert parsed["items"] == {"amb-0001": None, "amb-0002": "Kilns", "spk-1": "skip"}


def test_an_untouched_other_is_no_verdict():
    parsed = review.parse_note("> [!question] amb-0001 · x\n> - [x] other: ___\n")
    assert parsed["items"] == {"amb-0001": None}


def test_a_scheme_prefix_and_a_bee_label_are_read_from_a_note():
    note = "\n".join(
        [
            "---",
            'speaker_Speaker 2: "Wren"',
            'speaker_Unknown: "Moss \\"the\\" owl"',
            "---",
            "> [!question] who-1 · who is Speaker 2?",
        ]
    )
    # the default pattern is YouTube's labels only; a kind with spaced labels widens it
    assert review.parse_note(note)["speakers"] == {}
    parsed = review.parse_note(note, role_pattern=BEE_KIND.role_pattern)
    assert parsed["speakers"] == {"Speaker 2": "Wren", "Unknown": 'Moss "the" owl'}
    assert "who-1" not in review.parse_note(note)["items"]
    assert "who-1" in review.parse_note(note, review_key_prefix="who")["items"]


def test_a_verdict_given_again_adds_no_row(database):
    kwargs = dict(
        source_id=BEE_SOURCE, key="amb-0001", kind="ambiguity", verdict="Ren", span="wren"
    )
    review.record_verdict(database, **kwargs)
    review.record_verdict(database, **kwargs)
    assert database.one("SELECT COUNT(*) AS n FROM review_verdicts")["n"] == 1
    review.record_verdict(database, **{**kwargs, "verdict": "Wren"})
    assert review.verdicts_for(database, BEE_SOURCE)["amb-0001"]["verdict"] == "Wren"
    assert database.one("SELECT COUNT(*) AS n FROM review_verdicts")["n"] == 2


def test_an_ambiguity_verdict_becomes_a_correction_memory_rule(database):
    review.record_verdict(
        database,
        source_id=BEE_SOURCE,
        key="amb-0001",
        kind="ambiguity",
        verdict="Ren",
        span="Wren!",
    )
    rule = review.corrections(database)["wren"]
    assert (rule["verdict"], rule["kind"], rule["origin_source"]) == ("Ren", "term", BEE_SOURCE)
    review.record_verdict(
        database,
        source_id=BEE_SOURCE,
        key="amb-0002",
        kind="ambiguity",
        verdict="skip",
        span="kiln",
    )
    assert "kiln" not in review.corrections(database)


def test_a_speaker_verdict_lands_a_name_for_the_label(database):
    review.record_verdict(
        database, source_id=BEE_SOURCE, key="spk-1", kind="speaker", verdict="Wren", role="Unknown"
    )
    assert Store(database).current_speaker_names(BEE_SOURCE) == {"Unknown": "Wren"}


def test_reconcile_carries_a_verdict_and_auto_resolves_from_the_memory():
    document = bee_transcript(
        ambiguities=[
            ambiguity("amb-0001", 1, "wren", "Wren", ["Wren", "Ren"]),
            ambiguity("amb-0002", 2, "kiln", "kiln", ["kiln", "Kiln"]),
        ]
    )
    verdicts = {"amb-0001": {"kind": "ambiguity", "span": "wren", "verdict": "Ren"}}
    memory = {"kiln": {"verdict": "Kiln", "origin_source": BEE_SOURCE, "created_at": "2026-10-01"}}
    counts = review.reconcile(document, verdicts, memory)
    assert (counts["carried"], counts["auto_resolved"], counts["unplaced"]) == (1, 1, 0)
    assert document["segments"][1]["text"] == "We should call Ren about the kiln."
    assert document["segments"][2]["text"] == "Wren said the Kiln is ready."
    assert document["ambiguities"][1]["origin"] == f"{BEE_SOURCE} 2026-10-01"


def test_a_verdict_with_a_vanished_span_is_unplaced():
    verdicts = {"amb-0009": {"kind": "ambiguity", "span": "gone words", "verdict": "Else"}}
    counts = review.reconcile(bee_transcript(), verdicts, {})
    assert counts["unplaced"] == 1
    assert counts["unplaced_items"] == [
        {"key": "amb-0009", "span": "gone words", "verdict": "Else"}
    ]


def test_a_verdict_the_text_already_carries_is_applied_not_lost():
    verdicts = {"amb-0009": {"kind": "ambiguity", "span": "gone", "verdict": "kiln"}}
    counts = review.reconcile(bee_transcript(), verdicts, {})
    assert (counts["already_applied"], counts["unplaced"]) == (1, 0)


def test_candidates_offered_merge_candidates_that_differ_only_in_case_or_punctuation():
    a = ambiguity("amb-0001", 0, "wren", "Wren,", ["wren", "Ren", "REN."])
    assert review.candidates_offered(a) == ["Wren,", "Ren"]


def test_verdict_kind_treats_names_as_terms():
    assert review.verdict_kind("Quill") == "term"
    assert review.verdict_kind("") == "reading"
