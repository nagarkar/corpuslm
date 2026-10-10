"""Review kinds (corpuslm.review_kinds): the two source kinds' hooks, built here so one server can
serve both. Sanitized fixtures only."""

from __future__ import annotations

import json

import pytest

from corpuslm import review_kinds, transcript
from corpuslm.speaker_map import YOUTUBE


def test_a_youtube_card_links_to_the_watch_url_at_the_second():
    kind = review_kinds.youtube_review()
    assert kind.anchor_url({"id": "aaaaaaaaaaa"}, 12.7) == (
        "https://www.youtube.com/watch?v=aaaaaaaaaaa&t=12s"
    )
    assert kind.source_label({"title": "A talk"}) == "A talk"
    assert kind.scheme is YOUTUBE and kind.links_note.startswith("Timestamps open")
    assert kind.stale_nodes == ("pass3", "speaker_map")


def test_a_bee_card_has_no_link_and_the_picker_offers_the_owner():
    kind = review_kinds.bee_review("Quill")
    source = {"id": "bee:1", "title": "A walk", "published_at": "2026-10-03T09:14:00-07:00"}
    assert kind.anchor_url(source, 5.0) is None
    assert kind.source_label(source) == "A walk · 2026-10-03"
    assert kind.source_label({"id": "bee:1", "title": "", "published_at": ""}) == "bee:1"
    assert kind.extra_speaker_names(source) == ["Quill"]
    assert review_kinds.bee_review("").extra_speaker_names(source) == []
    assert kind.stale_nodes == ()


def test_bee_transcripts_are_read_with_free_speaker_labels():
    from review_kinds import bee_transcript

    kind = review_kinds.bee_review("Quill")
    document = bee_transcript()
    document["video_id"] = "bee:1"  # Bee's own source ids
    document["segments"][1]["role"] = "Speaker 2"
    text = json.dumps(document)
    assert transcript.loads(text, refined_schema=kind.refined_schema)["segments"]
    with pytest.raises(ValueError):
        transcript.loads(text)  # the shared schema knows only HOST, GUEST_n and UNKNOWN


def test_the_kinds_for_a_settings_file_decline_a_kind_with_no_review_kind():
    kinds = review_kinds.kinds_for_settings({"youtube": "", "bee": "Quill"})
    assert set(kinds) == {"youtube", "bee"}
    with pytest.raises(ValueError, match="no review kind for source kind 'podcast'"):
        review_kinds.kinds_for_settings({"podcast": ""})
