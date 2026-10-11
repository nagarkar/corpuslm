"""Accept suggestions (corpuslm.review_accept_suggestions): open ambiguity cards answered with
the AI cleanup's recorded suggestion, saved as the review page would save them, never over an
existing answer. Sanitized synthetic text only."""

from __future__ import annotations

import json
from datetime import date

import pytest
from review_kinds import (
    BEE_KIND,
    YOUTUBE_SOURCE,
    add_corpus_of_bee_sources,
    add_youtube_source,
    ambiguity,
    bee_speaker_map,
    bee_transcript,
    segment,
    write_source,
    youtube_transcript,
)

from corpuslm import (
    cli,
    db,
    model_suggestions,
    review_accept_suggestions,
    review_kinds,
    review_page,
    review_server,
)
from corpuslm.review_page import KindSetup
from corpuslm.store import Store

BEE_SOURCE = "bee:101"
SECOND_BEE_SOURCE = "bee:102"
THIRD_BEE_SOURCE = "bee:103"
CUTOFF = date(2026, 7, 1)
AT = "2026-10-09T10:00:00Z"


def pick(key, index, span, candidate, others=("Other",), **more):
    return ambiguity(key, index, span, candidate, [candidate, *others], **more)


@pytest.fixture
def database(tmp_path):
    d = db.Database(tmp_path / "transcript.sqlite")
    yield d
    d.close()


@pytest.fixture
def kinds(tmp_path):
    return {
        "youtube": KindSetup(review_kinds.youtube_review(), tmp_path / "videos"),
        "bee": KindSetup(BEE_KIND, tmp_path / "transcripts"),
    }


@pytest.fixture
def world(database, kinds):
    """One Bee corpus: an early source, a middle one and a late one (after the cutoff), with one
    open card for each way a card can be answered or left alone; and a mixed corpus."""
    store = Store(database)
    corpus = add_corpus_of_bee_sources(
        store, "Notes", [BEE_SOURCE, SECOND_BEE_SOURCE, THIRD_BEE_SOURCE]
    )
    dates = {
        BEE_SOURCE: "2026-03-01T09:00:00.000-08:00",
        SECOND_BEE_SOURCE: "2026-04-01T09:00:00.000-07:00",
        THIRD_BEE_SOURCE: "2026-10-05T09:00:00.000-07:00",
    }
    for source_id, published in dates.items():
        store.update_source(source_id, {"published_at": published})
    early = [
        pick("amb-0001", 1, "wren", "Wren", ("Ren",)),  # answered
        pick("amb-0002", 1, "kiln", "kiln", ("kill",)),  # no recorded suggestion
        pick("amb-0003", 2, "glaze", "glaze", ("glays",)),  # already answered
        pick("amb-0004", 2, "ember", "Ember", ("Amber",)),  # differs from the middle source
        pick("amb-0005", 0, "slip", "slip", ("slap",)),  # shared with the late source
        pick("amb-0006", 0, "clay", "clay", ("claye",), status="resolved", resolution="clay"),
    ]
    middle = [pick("amb-0001", 1, "ember", "Amber", ("Ember",))]
    late = [pick("amb-0001", 1, "slip", "slip", ("slap",))]
    for source_id, items in (
        (BEE_SOURCE, early),
        (SECOND_BEE_SOURCE, middle),
        (THIRD_BEE_SOURCE, late),
    ):
        document = bee_transcript(ambiguities=items) | {
            "video_id": source_id,
            "segments": [
                segment(i, i * 5.0, "wren kiln glaze ember slip clay here", speaker="Unknown")
                for i in range(3)
            ],
        }
        write_source(kinds["bee"].sources_directory, source_id, document, bee_speaker_map())
        recorded = [i for i in items if i["span"] != "kiln"]
        store.replace_model_suggestions(
            source_id,
            model_suggestions.suggestions_of(recorded),
            artifact_sha256="b" * 64,
            model="model",
            created_by="test",
        )
    return store, corpus


def cards(database, kinds, name="Notes"):
    return {
        c["span_norm"]: c
        for c in review_page.page_data_by_kind(database, kinds, name)["cards"]
        if c["kind"] == "ambiguity"
    }


def accept(database, kinds, **more):
    return review_accept_suggestions.accept(database, kinds, kind="bee", before=CUTOFF, **more)


def test_the_answer_equals_the_suggestion_and_shows_on_the_page(database, kinds, world, tmp_path):
    store, corpus = world
    report = accept(database, kinds)
    answered = store.review_answer(corpus, cards(database, kinds)["wren"]["id"])
    assert answered is not None and answered["verdict"] == "Wren"
    assert report.cards_answered == report.cards_to_answer == 2  # wren and glaze
    # the page's server hands the answer back as it hands any tap
    served = review_server.Answers(tmp_path / "transcript.sqlite").list(f"reviews/{corpus}/items")
    assert sorted(d["data"]["verdict"] for d in served) == ["Wren", "glaze"]
    # nothing is landed: the card is still open and no verdict row exists
    assert not cards(database, kinds)["wren"]["resolved"]
    assert store.current_review_verdicts(BEE_SOURCE) == {}


def test_every_way_a_card_is_left_open_is_counted(database, kinds, world):
    store, corpus = world
    found = cards(database, kinds)
    store.save_review_answer(corpus, found["glaze"]["id"], "glays", AT, created_by="owner")
    report = accept(database, kinds)
    assert report.cards_considered == 5  # wren, kiln, glaze, ember, slip (clay is resolved)
    assert report.cards_answered == 1
    assert report.cards_skipped_already_answered == 1
    assert report.cards_skipped_no_recorded_suggestion == 1
    assert report.cards_skipped_suggestions_differ == 1
    assert report.cards_skipped_a_source_not_before_the_cutoff == 1
    assert report.cards_left_alone_with_a_source_of_another_kind == 0


def test_an_existing_answer_is_never_overwritten(database, kinds, world):
    store, corpus = world
    found = cards(database, kinds)
    store.save_review_answer(corpus, found["wren"]["id"], "Ren", AT, created_by="owner")
    store.save_review_answer(corpus, found["glaze"]["id"], "", AT, created_by="owner")  # undone
    accept(database, kinds)
    assert store.review_answer(corpus, found["wren"]["id"])["verdict"] == "Ren"
    assert store.review_answer(corpus, found["glaze"]["id"])["verdict"] == ""
    assert store.review_answer(corpus, found["wren"]["id"])["answered_at"] == AT


def test_speaker_cards_and_resolved_cards_are_never_touched(database, kinds, world):
    store, corpus = world
    accept(database, kinds)
    data = review_page.page_data_by_kind(database, kinds, "Notes")
    untouched = [c["id"] for c in data["cards"] if c["kind"] == "speaker" or c["resolved"]]
    assert untouched
    assert all(store.review_answer(corpus, card_id) is None for card_id in untouched)
    assert len(store.review_answers(corpus)) == 2


def test_a_card_with_a_source_on_or_after_the_cutoff_is_left_open(database, kinds, world):
    store, corpus = world
    accept(database, kinds)
    assert store.review_answer(corpus, cards(database, kinds)["slip"]["id"]) is None
    later = review_accept_suggestions.accept(database, kinds, kind="bee", before=date(2026, 11, 1))
    assert later.cards_skipped_a_source_not_before_the_cutoff == 0
    assert later.cards_answered == 1  # slip; wren and glaze were answered by the first run
    assert later.cards_skipped_already_answered == 2


def test_a_source_with_no_recording_date_is_not_before_the_cutoff(database, kinds, world):
    store, corpus = world
    store.update_source(BEE_SOURCE, {"published_at": "UNKNOWN"})
    report = accept(database, kinds)
    assert report.cards_answered == 0
    assert report.cards_skipped_a_source_not_before_the_cutoff >= 1


def test_a_card_without_a_recorded_suggestion_or_with_differing_ones_is_left_open(
    database, kinds, world
):
    store, corpus = world
    accept(database, kinds)
    found = cards(database, kinds)
    for span in ("kiln", "ember"):
        assert store.review_answer(corpus, found[span]["id"]) is None


def test_a_suggestion_for_other_words_is_not_a_suggestion_for_the_card(database, kinds, world):
    store, corpus = world
    # the recorded row for amb-0001 now says it was about other words
    wrong = model_suggestions.suggestions_of([pick("amb-0001", 1, "heron", "Heron")])
    store.replace_model_suggestions(
        BEE_SOURCE,
        wrong,
        artifact_sha256="c" * 64,
        model="model",
        created_by="test",
    )
    report = accept(database, kinds)
    assert report.cards_answered == 0
    assert store.review_answers(corpus) == []


def test_a_card_that_spans_another_kind_is_left_alone(database, kinds, tmp_path):
    store = Store(database)
    corpus = store.add_corpus({"name": "Mixed"}, created_by="test")
    add_youtube_source(store, corpus)
    store.add_source(BEE_SOURCE, "transcripts/x.md", created_by="test", kind="bee")
    store.add_member(corpus, BEE_SOURCE, "author", created_by="test")
    for source_id in (YOUTUBE_SOURCE, BEE_SOURCE):
        store.update_source(source_id, {"published_at": "2026-01-01"})
    write_source(kinds["youtube"].sources_directory, YOUTUBE_SOURCE, youtube_transcript())
    write_source(kinds["bee"].sources_directory, BEE_SOURCE, bee_transcript())
    for source_id in (YOUTUBE_SOURCE, BEE_SOURCE):
        store.replace_model_suggestions(
            source_id,
            model_suggestions.suggestions_of(
                [pick("amb-0001", 1, "wren", "Wren", ("Ren",))],
            ),
            artifact_sha256="d" * 64,
            model="model",
            created_by="test",
        )
    report = accept(database, kinds)
    assert report.cards_left_alone_with_a_source_of_another_kind == 1
    assert report.cards_considered == 0 and store.review_answers(corpus) == []


def test_a_dry_run_counts_and_writes_nothing(database, kinds, world):
    store, corpus = world
    report = accept(database, kinds, dry_run=True)
    assert report.cards_to_answer == 2 and report.cards_answered == 0
    assert store.review_answers(corpus) == []


@pytest.fixture
def config(tmp_path, world, database):
    database.close()
    path = tmp_path / "review.toml"
    path.write_text(
        f'database = "{tmp_path / "transcript.sqlite"}"\n'
        f'[kinds.youtube]\ndirectory = "{tmp_path / "videos"}"\n'
        f'[kinds.bee]\ndirectory = "{tmp_path / "transcripts"}"\nowner_first_name = "Quill"\n'
    )
    return path


def run(capsys, *argv):
    code = cli.main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_the_verb_takes_a_kind_and_a_date_and_prints_counts(config, capsys):
    base = ["review", "accept-suggestions", "--config", str(config), "--kind", "bee"]
    code, board = run(capsys, *base, "--before", "2026-07-01", "--dry-run")
    assert code == 0 and board["cards_to_answer"] == 2 and board["cards_answered"] == 0
    assert "Wren" not in json.dumps(board)
    code, board = run(capsys, *base, "--before", "2026-07-01")
    assert code == 0 and board["cards_answered"] == 2
    code, again = run(capsys, *base, "--before", "2026-07-01")
    assert again["cards_answered"] == 0 and again["cards_skipped_already_answered"] == 2


def test_a_bad_date_or_kind_is_named_and_exits_one(config, capsys):
    base = ["review", "accept-suggestions", "--config", str(config)]
    code, board = run(capsys, *base, "--kind", "bee", "--before", "July")
    assert code == 1 and "--before" in board["error"]
    code, board = run(capsys, *base, "--kind", "podcast", "--before", "2026-07-01")
    assert code == 1 and "podcast" in board["error"]
