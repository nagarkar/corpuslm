"""Review apply (corpuslm.review_apply): the saved answers of every source kind land as verdicts
and are deleted, with no model. One corpus holds a YouTube source and a Bee source; sanitized
fixtures."""

from __future__ import annotations

import pytest
from review_kinds import (
    BEE_KIND,
    BEE_SOURCE,
    OWNER,
    YOUTUBE_SOURCE,
    add_youtube_source,
    bee_speaker_map,
    bee_transcript,
    write_source,
    youtube_speaker_map,
    youtube_transcript,
)

from corpuslm import db, review_apply, review_kinds, review_page
from corpuslm.review_page import KindSetup
from corpuslm.store import Store

AT = "2026-10-09T10:00:00Z"


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
def corpus_id(database, kinds):
    store = Store(database)
    corpus = store.add_corpus({"name": "Mixed"}, created_by="test")
    add_youtube_source(store, corpus)
    store.add_source(BEE_SOURCE, f"transcripts/{BEE_SOURCE}.md", created_by="test", kind="bee")
    store.add_member(corpus, BEE_SOURCE, "author", created_by="test")
    write_source(
        kinds["youtube"].sources_directory,
        YOUTUBE_SOURCE,
        youtube_transcript(),
        youtube_speaker_map(),
    )
    write_source(kinds["bee"].sources_directory, BEE_SOURCE, bee_transcript(), bee_speaker_map())
    return corpus


def cards(database, kinds):
    return review_page.page_data_by_kind(database, kinds, "Mixed")["cards"]


def answer(database, corpus_id, card, verdict):
    Store(database).save_review_answer(corpus_id, card["id"], verdict, AT, created_by="test")


def apply(database, kinds, **more):
    return review_apply.apply(database, kinds, created_by="test", **more)


def test_the_page_reads_each_source_with_its_own_kind(database, kinds, corpus_id):
    found = cards(database, kinds)
    ambiguity = next(c for c in found if c["kind"] == "ambiguity")
    urls = {i["video_id"]: i["url"] for i in ambiguity["items"]}
    assert urls[YOUTUBE_SOURCE] == f"https://www.youtube.com/watch?v={YOUTUBE_SOURCE}&t=12s"
    assert urls[BEE_SOURCE] is None
    assert ambiguity["count"] == 2  # one card, both kinds' places
    speakers = {c["video_id"]: c for c in found if c["kind"] == "speaker" and c["open"]}
    assert set(speakers) == {YOUTUBE_SOURCE, BEE_SOURCE}


def test_landed_answers_are_deleted_and_unknown_ones_stay(database, kinds, corpus_id):
    store = Store(database)
    found = cards(database, kinds)
    ambiguity = next(c for c in found if c["kind"] == "ambiguity")
    answer(database, corpus_id, ambiguity, "Wren")
    store.save_review_answer(corpus_id, "card-gone", "x", AT, created_by="test")
    store.save_review_answer(corpus_id, "card-undone", "", AT, created_by="test")
    report = apply(database, kinds)
    assert (report.answers_saved, report.verdicts_to_land, report.verdicts_landed) == (2, 2, 2)
    assert report.unknown_cards == 1 and report.sources_waiting_for_ai_cleanup == 2
    left = [a["card_id"] for a in store.review_answers(corpus_id)]
    assert left == ["card-gone"]  # landed and undone answers are gone, the unknown one stays
    assert store.current_review_verdicts(BEE_SOURCE)["amb-0001"]["verdict"] == "Wren"
    assert store.current_review_verdicts(YOUTUBE_SOURCE)["amb-0001"]["verdict"] == "Wren"
    # applying again lands nothing new
    again = apply(database, kinds)
    assert (again.verdicts_to_land, again.answers_cleared) == (0, 0)


def test_a_landed_answer_marks_the_kinds_stale_nodes(database, kinds, corpus_id):
    store = Store(database)
    for node in ("pass3", "speaker_map", "model_cleanup"):
        for source in (YOUTUBE_SOURCE, BEE_SOURCE):
            store.record_artifact(
                source,
                node,
                path="",
                sha256="",
                state="built",
                inputs={},
                provenance={},
                created_by="test",
            )
    ambiguity = next(c for c in cards(database, kinds) if c["kind"] == "ambiguity")
    answer(database, corpus_id, ambiguity, "Ren")
    apply(database, kinds)
    state = {
        (s, n): (store.artifact(s, n) or {})["state"]
        for s in (YOUTUBE_SOURCE, BEE_SOURCE)
        for n in ("pass3", "speaker_map", "model_cleanup")
    }
    assert state[(YOUTUBE_SOURCE, "pass3")] == "stale"
    assert state[(YOUTUBE_SOURCE, "speaker_map")] == "stale"
    assert state[(BEE_SOURCE, "speaker_map")] == "built"  # Bee rebuilds from the answer itself
    assert state[(YOUTUBE_SOURCE, "model_cleanup")] == "built"


def test_a_speaker_answer_calls_the_kinds_rebuilder_and_only_for_its_kind(
    database, kinds, corpus_id
):
    speaker = next(
        c for c in cards(database, kinds) if c["kind"] == "speaker" and c["video_id"] == BEE_SOURCE
    )
    answer(database, corpus_id, speaker, "Fern")
    called: dict[str, set[str]] = {}

    def rebuild(sources):
        called["bee"] = set(sources)
        return len(sources)

    report = apply(database, kinds, rebuilders={"bee": rebuild})
    assert called == {"bee": {BEE_SOURCE}} and report.speaker_maps_rebuilt == 1
    assert Store(database).current_speaker_names(BEE_SOURCE) == {"Unknown": "Fern"}


def test_a_name_the_owner_picked_is_never_overwritten(database, kinds, corpus_id):
    speaker = next(
        c for c in cards(database, kinds) if c["kind"] == "speaker" and c["video_id"] == BEE_SOURCE
    )
    write_source_map_named(kinds["bee"].sources_directory)
    answer(database, corpus_id, speaker, "Moss")
    report = apply(database, kinds)
    assert report.verdicts_landed == 0 and report.unknown_cards == 1
    assert Store(database).current_speaker_names(BEE_SOURCE) == {}


def write_source_map_named(directory):
    import json

    path = directory / BEE_SOURCE / "speaker_map.json"
    path.write_text(json.dumps(bee_speaker_map(unknown_status="AUTHOR", unknown_name="Fern")))


def test_only_one_kind_leaves_a_card_that_spans_kinds_for_the_other(database, kinds, corpus_id):
    ambiguity = next(c for c in cards(database, kinds) if c["kind"] == "ambiguity")
    answer(database, corpus_id, ambiguity, "Wren")
    report = apply(database, kinds, only_kinds=["bee"])
    store = Store(database)
    assert report.verdicts_landed == 1
    assert "amb-0001" in store.current_review_verdicts(BEE_SOURCE)
    assert store.current_review_verdicts(YOUTUBE_SOURCE) == {}
    assert len(store.review_answers(corpus_id)) == 1  # the YouTube place still needs it
    apply(database, kinds, only_kinds=["youtube"])
    assert store.review_answers(corpus_id) == []


def test_a_dry_run_counts_and_writes_nothing(database, kinds, corpus_id):
    ambiguity = next(c for c in cards(database, kinds) if c["kind"] == "ambiguity")
    answer(database, corpus_id, ambiguity, "Wren")
    report = apply(database, kinds, dry_run=True)
    assert (report.verdicts_to_land, report.verdicts_landed, report.answers_cleared) == (2, 0, 0)
    store = Store(database)
    assert len(store.review_answers(corpus_id)) == 1
    assert store.current_review_verdicts(BEE_SOURCE) == {}


def test_a_failed_landing_leaves_every_answer(database, kinds, corpus_id, monkeypatch):
    ambiguity = next(c for c in cards(database, kinds) if c["kind"] == "ambiguity")
    answer(database, corpus_id, ambiguity, "Wren")

    def explode(*args, **more):
        raise RuntimeError("boom")

    monkeypatch.setattr(review_apply, "land_page_verdicts", explode)
    with pytest.raises(RuntimeError):
        apply(database, kinds)
    assert len(Store(database).review_answers(corpus_id)) == 1


def test_the_owner_name_is_offered_for_the_unnamed_label(database, kinds, corpus_id):
    speaker = next(
        c for c in cards(database, kinds) if c["kind"] == "speaker" and c["video_id"] == BEE_SOURCE
    )
    assert OWNER in speaker["names"]
