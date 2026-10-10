"""Review page (corpuslm.review_page): cards, the page and the decisions file, for a Bee-shaped
source (kind 'bee', no anchor link, an 'Unknown' speaker as a review item)."""

from __future__ import annotations

import json

import pytest
from review_kinds import (
    BEE_KIND,
    BEE_SOURCE,
    LINKED_KIND,
    OWNER,
    SECOND_BEE_SOURCE,
    SUMMARY_HEADINGS,
    add_corpus_of_bee_sources,
    ambiguity,
    bee_speaker_map,
    bee_transcript,
    write_source,
)

from corpuslm import db, review_page
from corpuslm.store import Store


@pytest.fixture
def database(tmp_path):
    d = db.Database(tmp_path / "transcript.sqlite")
    yield d
    d.close()


@pytest.fixture
def sources_directory(tmp_path):
    return tmp_path / "sources"


@pytest.fixture
def corpus(database, sources_directory):
    """Two Bee recordings that both mishear 'wren'; the first also has an 'Unknown' speaker."""
    corpus_id = add_corpus_of_bee_sources(Store(database), "Walks", [BEE_SOURCE, SECOND_BEE_SOURCE])
    write_source(sources_directory, BEE_SOURCE, bee_transcript(), bee_speaker_map())
    second = bee_transcript(ambiguities=[ambiguity("amb-0001", 2, "Wren", "Wren", ["Wren", "Ren"])])
    write_source(sources_directory, SECOND_BEE_SOURCE, second)
    return corpus_id


def build(database, sources_directory, kind=BEE_KIND):
    return review_page.page_data(database, sources_directory, "Walks", kind=kind)


def test_one_card_per_distinct_span_across_sources(database, sources_directory, corpus):
    data = build(database, sources_directory)
    ambiguity_cards = [c for c in data["cards"] if c["kind"] == "ambiguity"]
    assert len(ambiguity_cards) == 1
    card = ambiguity_cards[0]
    assert card["count"] == 2 and card["span_norm"] == "wren"
    assert [(i["video_id"], i["key"], i["at"]) for i in card["items"]] == [
        (BEE_SOURCE, "amb-0001", "0:05"),
        (SECOND_BEE_SOURCE, "amb-0001", "0:09"),
    ]
    # the recognizer's line, the heard words in bold
    assert card["context"] == "we should call **wren**"
    assert data["collection"] == f"reviews/{corpus}/items"


def test_a_kind_without_links_gives_time_labels_only(database, sources_directory, corpus):
    data = build(database, sources_directory)
    card = next(c for c in data["cards"] if c["kind"] == "ambiguity")
    speaker = next(c for c in data["cards"] if c["kind"] == "speaker" and c["role"] == "Unknown")
    assert card["first"]["url"] is None and all(i["url"] is None for i in card["items"])
    assert speaker["url"] is None and speaker["at"] == "0:05"
    html = review_page.render(data)
    assert "url ? `<a href=" in html and "<span>${esc(at)}</span>" in html
    assert "file://" not in html
    # no kind footnote, so no empty note paragraph under the status line
    assert '<p class="note">' not in html


def test_a_kind_with_links_keeps_the_original_link_script_and_its_footnote(
    database, sources_directory, corpus
):
    data = build(database, sources_directory, LINKED_KIND)
    card = next(c for c in data["cards"] if c["kind"] == "ambiguity")
    assert card["first"]["url"] == f"https://example.test/{BEE_SOURCE}?t=5s"
    html = review_page.render(data, links_note=LINKED_KIND.links_note)
    assert "url ? `<a href=" not in html and "const link = (url, at) => `<a href=" in html
    assert '<p class="note">Links open the player.</p>' in html


def test_the_unknown_speaker_is_an_open_card_with_the_names_heard_and_offered(
    database, sources_directory, corpus
):
    data = build(database, sources_directory)
    speakers = {c["role"]: c for c in data["cards"] if c["kind"] == "speaker"}
    unknown = speakers["Unknown"]
    assert (unknown["open"], unknown["resolved"], unknown["key"]) == (True, False, "spk-1")
    assert unknown["names"] == ["Ren", OWNER]
    assert speakers[OWNER]["open"] is False
    assert data["open_items"] == 3  # two ambiguities and the speaker
    assert data["open_cards"] == 2  # one card per span, plus the speaker


def test_summary_headings_and_vocabulary_terms_are_never_offered_as_speaker_names(
    database, sources_directory, corpus
):
    data = build(database, sources_directory)
    offered = {name for c in data["cards"] if c["kind"] == "speaker" for name in c["names"]}
    assert offered == {"Ren", OWNER}
    assert not offered & set(SUMMARY_HEADINGS)


def test_a_name_the_owner_already_picked_is_offered_for_the_other_labels(
    database, sources_directory, corpus
):
    Store(database).append_speaker_verdict(
        source_id=BEE_SOURCE, role="Speaker 2", name="Fern", created_by="test"
    )
    unknown = next(
        c for c in build(database, sources_directory)["cards"] if c.get("role") == "Unknown"
    )
    assert unknown["names"] == ["Ren", OWNER, "Fern"]


def test_a_kind_can_add_names_to_the_speaker_picker(database, sources_directory, corpus):
    from dataclasses import replace

    kind = replace(BEE_KIND, extra_speaker_names=lambda source: ["Moss", "Fern"])
    unknown = next(
        c for c in build(database, sources_directory, kind)["cards"] if c.get("role") == "Unknown"
    )
    assert unknown["names"] == ["Ren", OWNER, "Moss", "Fern"]


def test_an_author_named_speaker_is_not_to_do_and_is_listed_first(
    database, sources_directory, corpus
):
    write_source_again(
        sources_directory, bee_speaker_map(unknown_status="AUTHOR", unknown_name="Fern")
    )
    data = build(database, sources_directory)
    unknown = next(c for c in data["cards"] if c.get("role") == "Unknown")
    assert (unknown["open"], unknown["resolved"], unknown["current"]) == (False, True, "Fern")
    assert unknown["names"][0] == "Fern"
    assert data["open_items"] == 2
    # a pick the author made is never turned into a verdict by the page
    verdicts, _ = review_page.decisions_to_verdicts(
        database,
        sources_directory,
        "Walks",
        {"decisions": {unknown["id"]: {"verdict": "Moss"}}},
        kind=BEE_KIND,
    )
    assert verdicts == []


def write_source_again(sources_directory, speaker_map):
    (sources_directory / BEE_SOURCE / "speaker_map.json").write_text(json.dumps(speaker_map))


def test_a_resolved_item_never_rejoins_an_open_card(database, sources_directory, corpus):
    done = ambiguity("amb-0001", 1, "wren", "Wren", ["Wren"], status="resolved", resolution="Ren")
    (sources_directory / BEE_SOURCE / "transcript.json").write_text(
        json.dumps(bee_transcript(ambiguities=[done]))
    )
    cards = [c for c in build(database, sources_directory)["cards"] if c["kind"] == "ambiguity"]
    assert sorted((c["resolved"], c["count"]) for c in cards) == [(False, 1), (True, 1)]
    open_card = next(c for c in cards if not c["resolved"])
    assert [i["video_id"] for i in open_card["items"]] == [SECOND_BEE_SOURCE]


def test_capped_and_expired_items_stay_off_the_page(database, sources_directory, corpus):
    capped = ambiguity("amb-0001", 1, "wren", "Wren", ["Wren"], status="capped")
    expired = ambiguity("amb-0002", 2, "kiln", "kiln", ["kiln"], status="expired")
    (sources_directory / BEE_SOURCE / "transcript.json").write_text(
        json.dumps(bee_transcript(ambiguities=[capped, expired]))
    )
    spans = [
        c["span"] for c in build(database, sources_directory)["cards"] if c["kind"] == "ambiguity"
    ]
    assert spans == ["Wren"]  # only the second recording's


def test_decisions_map_back_to_one_verdict_per_key(database, sources_directory, corpus):
    data = build(database, sources_directory)
    card = next(c for c in data["cards"] if c["kind"] == "ambiguity")
    speaker = next(c for c in data["cards"] if c.get("role") == "Unknown")
    verdicts, unknown = review_page.decisions_to_verdicts(
        database,
        sources_directory,
        "Walks",
        {
            "corpus_id": corpus,
            "decisions": {
                card["id"]: {"verdict": " Ren "},
                speaker["id"]: {"verdict": "Wren"},
                "nope": {"verdict": "x"},
                "blank": {"verdict": ""},
            },
        },
        kind=BEE_KIND,
    )
    assert unknown == ["nope"]
    assert [(v["video_id"], v["key"], v["kind"], v["verdict"]) for v in verdicts] == [
        (BEE_SOURCE, "amb-0001", "ambiguity", "Ren"),
        (SECOND_BEE_SOURCE, "amb-0001", "ambiguity", "Ren"),
        (BEE_SOURCE, "spk-1", "speaker", "Wren"),
    ]
    assert verdicts[2]["role"] == "Unknown"
    with pytest.raises(ValueError, match="not"):
        review_page.decisions_to_verdicts(
            database,
            sources_directory,
            "Walks",
            {"corpus_id": "cor-other", "decisions": {}},
            kind=BEE_KIND,
        )


def test_split_cuts_by_whole_corpora_and_never_trims(database, sources_directory, corpus):
    store = Store(database)
    add_corpus_of_bee_sources(store, "Drives", [])
    document = review_page.workspace_data(database, sources_directory, kind=BEE_KIND)
    assert [c["corpus"] for c in document["corpora"]] == ["Walks", "Drives"]  # open items first
    assert review_page.split(document, page_bytes=10**7) == [document]
    parts = review_page.split(document, page_bytes=1)
    assert [[c["corpus"] for c in p["corpora"]] for p in parts] == [["Walks"], ["Drives"]]
    assert sum(len(c["cards"]) for p in parts for c in p["corpora"]) == len(
        document["corpora"][0]["cards"]
    )


def test_pages_are_written_atomically_one_file_per_part(
    database, sources_directory, corpus, tmp_path
):
    add_corpus_of_bee_sources(Store(database), "Drives", [])
    document = review_page.workspace_data(database, sources_directory, kind=BEE_KIND)
    written = review_page.write_pages(document, tmp_path / "out" / "page.html", page_bytes=1)
    assert [w["cards"] for w in written] == [3, 0]
    assert [w["path"].rsplit("/", 1)[-1] for w in written] == ["page.html", "page-2.html"]
    text = (tmp_path / "out" / "page.html").read_text(encoding="utf-8")
    assert "<title>Corpus review (1/2)</title>" in text
    assert (
        json.loads(text.split('id="data">')[1].split("</script>")[0])["corpora"][0]["corpus"]
        == "Walks"
    )


def test_the_page_script_has_a_local_backend_beside_the_artifact_one(
    database, sources_directory, corpus
):
    html = review_page.render(build(database, sources_directory))
    assert "async function localDatabase()" in html
    assert "await claude.use('db')" in html  # the artifact path is still there
    assert "Tell Claude to apply the review." in html


def test_a_workspace_page_lists_only_the_corpora_of_its_kind(database, sources_directory, corpus):
    from review_kinds import LINKED_KIND

    store = Store(database)
    store.add_source("yt:1", "https://example.test/1", created_by="test", kind="youtube")
    other = store.add_corpus({"name": "Talks"}, created_by="test")
    store.add_member(other, "yt:1", "author", created_by="test")
    store.add_corpus({"name": "Empty"}, created_by="test")

    def names(kind):
        data = review_page.workspace_data(database, sources_directory, kind=kind)
        return [c["corpus"] for c in data["corpora"]]

    assert sorted(names(BEE_KIND)) == ["Empty", "Walks"]
    assert sorted(names(LINKED_KIND)) == ["Empty", "Talks"]


def test_a_kind_with_its_own_schema_reads_documents_the_shared_one_rejects(
    database, sources_directory, corpus
):
    from dataclasses import replace

    from corpuslm import schemas, transcript

    document = bee_transcript()
    for seg in document["segments"]:
        seg["role"] = seg["speaker"]  # a label the shared schema does not list
    (sources_directory / BEE_SOURCE / "transcript.json").write_text(json.dumps(document))
    with pytest.raises(schemas.SchemaError):
        build(database, sources_directory)
    kind = replace(BEE_KIND, refined_schema=transcript.with_free_labels())
    assert any(c["kind"] == "ambiguity" for c in build(database, sources_directory, kind)["cards"])
