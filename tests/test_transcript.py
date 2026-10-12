"""Transcript document and its schema (`src/corpuslm/transcript.py`, `schemas.py`):
deterministic, same bytes in, same bytes out (golden files, captured from ytlm before the
move), and stated as properties over any timed text: cues come out sorted and disjoint, each
entry starts where its source segment starts, every SRT time reads back to the millisecond.
Sanitized synthetic text only."""

import html
import json
import re
from pathlib import Path

import pytest
from hypothesis import given, strategies

from corpuslm import schemas, transcript
from corpuslm.model import Segment

GOLDEN = Path(__file__).parent / "golden"
CAPTION = {"language": "en", "generated": None, "fetcher": "supadata"}


def _document(**over):
    base = {
        "video_id": "any-source",
        "schema": "ytlm.transcript/1",
        "state": "RAW",
        "caption": {"language": "en", "generated": False, "fetcher": "supadata"},
        "speaker_source": "",
        "segments": [
            {"idx": 0, "t0": 0.0, "t1": 1.5, "speaker": "", "text": "hello"},
            {"idx": 1, "t0": 1.5, "t1": 3.0, "speaker": "", "text": "world"},
        ],
        "words": [],
    }
    return base | over


def test_from_segments_flattens_whitespace_unescapes_and_clamps_overlaps():
    document = transcript.from_segments(
        "x",
        (
            Segment(0.0, 3.0, "hello\nthere &amp; welcome"),
            Segment(2.0, 4.0, "  second   cue "),
            Segment(4.0, 4.0, ""),
        ),
        CAPTION,
        "ytlm.transcript/1",
    )
    assert [segment["text"] for segment in document["segments"]] == [
        "hello there & welcome",
        "second cue",
    ]
    assert document["segments"][0]["t1"] == 2.0  # clamped to the next t0
    assert document["segments"][1] == {
        "idx": 1,
        "t0": 2.0,
        "t1": 4.0,
        "speaker": "",
        "text": "second cue",
    }
    assert (
        document["state"] == "RAW"
        and document["video_id"] == "x"
        and document["schema"] == "ytlm.transcript/1"
    )


def test_from_segments_takes_stored_segment_mappings_and_keeps_their_speaker():
    stored = [
        {"idx": 0, "t0": 0.0, "t1": 2.0, "speaker": "Ann", "text": "one"},
        {"idx": 1, "t0": 2.0, "t1": 3.0, "speaker": "", "text": "two"},
    ]
    document = transcript.from_segments(
        "s", stored, CAPTION, "beelm.transcript/1", speaker_source="diarized"
    )
    assert [segment["speaker"] for segment in document["segments"]] == ["Ann", ""]
    assert document["speaker_source"] == "diarized"
    assert transcript.loads(transcript.dumps(document)) == document


def test_golden_bytes_of_edge_cases_are_unchanged():
    """Captured from ytlm.transcript before the move: out-of-order, empty, entity-laden,
    backwards and overlapping cues."""
    track = (
        Segment(5.0, 5.0, "later &amp; <b>x</b>"),
        Segment(0.0, 3.0, "hello\nthere &#39;q&#39;  é"),
        Segment(2.0, 4.0, "  second   cue "),
        Segment(4.0, 4.0, ""),
        Segment(3.0004, 2.0, "backwards"),
    )
    document = transcript.from_segments("edgeCase_-1", track, CAPTION, "ytlm.transcript/1")
    assert transcript.dumps(document) == (GOLDEN / "edge_cases.transcript.json").read_text()
    assert transcript.to_srt(document) == (GOLDEN / "edge_cases.srt").read_text()


def test_dumps_is_canonical_and_loads_round_trips_it():
    document = _document()
    text = transcript.dumps(document)
    assert text.endswith("}\n") and list(json.loads(text)) == sorted(document)
    assert transcript.loads(text) == document
    assert transcript.dumps(transcript.loads(text)) == text


def test_srt_timestamps_and_wrapping():
    document = transcript.from_segments(
        "x",
        (
            Segment(1.2, 3.36, "All right, so here we are, in front of the elephants"),
            Segment(3661.5, 3662.0, "later"),
        ),
        CAPTION,
        "ytlm.transcript/1",
    )
    assert transcript.to_srt(document, max_line_chars=42) == (
        "1\n00:00:01,200 --> 00:00:03,360\n"
        "All right, so here we are, in front of the\nelephants\n\n"
        "2\n01:01:01,500 --> 01:01:02,000\nlater\n"
    )


def test_timestamp_reads_minutes_and_hours():
    assert transcript.timestamp(65) == "1:05"
    assert transcript.timestamp(3725.9) == "1:02:05"


@pytest.mark.parametrize("name", schemas.names())
def test_every_schema_property_has_a_description(name):
    assert schemas.undescribed(schemas.schema(name)) == [], name


def test_a_broken_document_is_rejected_naming_the_property():
    transcript.dumps(_document())
    with pytest.raises(schemas.SchemaError, match="segments"):
        transcript.dumps(_document(segments="nope"))
    with pytest.raises(schemas.SchemaError, match="state"):
        transcript.dumps(_document(state="SHINY"))
    with pytest.raises(schemas.SchemaError, match=r"schema: .*does not match"):
        transcript.dumps(_document(schema="not a schema id"))
    broken = _document()
    broken["segments"][0]["t1"] = "soon"
    with pytest.raises(schemas.SchemaError, match=r"segments\.0\.t1"):
        transcript.loads(json.dumps(broken))


def test_a_refined_schema_adds_a_tools_own_rules():
    refined = json.loads(json.dumps(schemas.schema("transcript")))
    refined["properties"]["video_id"]["pattern"] = "^[A-Za-z0-9_-]{11}$"
    with pytest.raises(schemas.SchemaError, match="video_id"):
        transcript.dumps(_document(), refined_schema=refined)
    transcript.dumps(_document(video_id="abcdefghijk"), refined_schema=refined)


def test_semantic_checks_follow_the_schema():
    bad_order = _document(
        segments=[
            {"idx": 0, "t0": 5.0, "t1": 6.0, "speaker": "", "text": "b"},
            {"idx": 1, "t0": 1.0, "t1": 2.0, "speaker": "", "text": "a"},
        ]
    )
    with pytest.raises(schemas.SchemaError, match="sorted"):
        schemas.validate("transcript", bad_order)
    bad_span = _document(segments=[{"idx": 0, "t0": 2.0, "t1": 1.0, "speaker": "", "text": "x"}])
    with pytest.raises(schemas.SchemaError, match="t1"):
        schemas.validate("transcript", bad_span)
    bad_idx = _document(segments=[{"idx": 4, "t0": 0.0, "t1": 1.0, "speaker": "", "text": "x"}])
    with pytest.raises(schemas.SchemaError, match="position"):
        schemas.validate("transcript", bad_idx)

    def ambiguity(key, index, span):
        return {
            "key": key,
            "idx": index,
            "span": span,
            "candidates": ["a", "b"],
            "pick": "a",
            "confidence": 0.4,
            "status": "pending",
        }

    with pytest.raises(schemas.SchemaError, match="amb-0001"):
        schemas.validate(
            "transcript",
            _document(
                ambiguities=[ambiguity("amb-0001", 0, "hello"), ambiguity("amb-0001", 1, "world")]
            ),
        )
    with pytest.raises(schemas.SchemaError, match="anchor"):
        schemas.validate("transcript", _document(ambiguities=[ambiguity("amb-0001", 0, "goodbye")]))
    with pytest.raises(schemas.SchemaError, match="does not exist"):
        schemas.validate("transcript", _document(ambiguities=[ambiguity("amb-0001", 9, "hello")]))


def test_an_unknown_schema_name_is_named():
    with pytest.raises(schemas.SchemaError, match="nope"):
        schemas.schema("nope")


# ---- properties -------------------------------------------------------------

_seconds = strategies.floats(min_value=0, max_value=36_000, allow_nan=False, allow_infinity=False)
_text = strategies.text(alphabet=strategies.sampled_from(list("ab cé\n\t&;amp")), max_size=30)
_segments = strategies.lists(
    strategies.builds(
        lambda start_seconds, duration_seconds, text: Segment(
            start_seconds, start_seconds + duration_seconds, text
        ),
        _seconds,
        _seconds,
        _text,
    ),
    max_size=25,
)


def _kept(segments):
    return [
        segment for segment in segments if re.sub(r"\s+", " ", html.unescape(segment.text)).strip()
    ]


@given(segments=_segments)
def test_from_segments_yields_sorted_disjoint_valid_segments_that_start_where_their_source_does(
    segments,
):
    document = transcript.from_segments("vid00000000", segments, CAPTION, "ytlm.transcript/1")
    out = document["segments"]
    assert [segment["idx"] for segment in out] == list(range(len(out)))
    assert sorted(round(segment.start_seconds, 3) for segment in _kept(segments)) == [
        segment["t0"] for segment in out
    ]
    for previous, following in zip(out, out[1:], strict=False):
        assert previous["t0"] <= following["t0"] and previous["t1"] <= following["t0"]
    for segment in out:
        assert (
            segment["t0"] <= segment["t1"]
            and segment["text"] == segment["text"].strip()
            and "  " not in segment["text"]
        )
    transcript.loads(transcript.dumps(document))


@given(milliseconds=strategies.integers(min_value=0, max_value=100 * 3_600_000 - 1))
def test_an_srt_time_reads_back_to_the_millisecond(milliseconds):
    match = re.fullmatch(
        r"(\d{2,}):(\d{2}):(\d{2}),(\d{3})", transcript.srt_time(milliseconds / 1000)
    )
    assert match
    hours, minutes, seconds, milliseconds_part = (int(group) for group in match.groups())
    assert minutes < 60 and seconds < 60
    assert ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds_part == milliseconds


@given(segments=_segments, width=strategies.integers(min_value=10, max_value=80))
def test_every_srt_entry_is_numbered_and_timed_from_its_segment(segments, width):
    document = transcript.from_segments("vid00000000", segments, CAPTION, "ytlm.transcript/1")
    blocks = [
        block for block in transcript.to_srt(document, max_line_chars=width).split("\n\n") if block
    ]
    assert len(blocks) == len(document["segments"])
    for block, segment in zip(blocks, document["segments"], strict=True):
        number, times, *lines = block.rstrip("\n").split("\n")
        assert number == str(segment["idx"] + 1)
        assert (
            times
            == f"{transcript.srt_time(segment['t0'])} --> {transcript.srt_time(segment['t1'])}"
        )
        # wrapping moves line breaks, never a character
        assert "".join(lines).replace(" ", "") == segment["text"].replace(" ", "")
        assert all(len(line) <= width for line in lines) or lines == [segment["text"]]


def test_free_labels_accept_a_label_the_shared_schema_does_not_list():
    document = transcript.from_segments(
        "any", [{"t0": 0, "t1": 1, "text": "hello", "speaker": "Quill"}], CAPTION, "x.transcript/1"
    )
    document["segments"][0]["role"] = "Quill"
    with pytest.raises(schemas.SchemaError, match="role"):
        transcript.dumps(document)
    free = transcript.with_free_labels()
    assert (
        transcript.loads(transcript.dumps(document, refined_schema=free), refined_schema=free)
        == document
    )
    assert (
        "enum"
        in schemas.schema("transcript")["properties"]["segments"]["items"]["properties"]["role"]
    )  # the shared schema itself is untouched
