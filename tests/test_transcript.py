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
from hypothesis import given
from hypothesis import strategies as st

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
    t = transcript.from_segments(
        "x",
        (
            Segment(0.0, 3.0, "hello\nthere &amp; welcome"),
            Segment(2.0, 4.0, "  second   cue "),
            Segment(4.0, 4.0, ""),
        ),
        CAPTION,
        "ytlm.transcript/1",
    )
    assert [s["text"] for s in t["segments"]] == ["hello there & welcome", "second cue"]
    assert t["segments"][0]["t1"] == 2.0  # clamped to the next t0
    assert t["segments"][1] == {"idx": 1, "t0": 2.0, "t1": 4.0, "speaker": "", "text": "second cue"}
    assert t["state"] == "RAW" and t["video_id"] == "x" and t["schema"] == "ytlm.transcript/1"


def test_from_segments_takes_stored_segment_mappings_and_keeps_their_speaker():
    stored = [
        {"idx": 0, "t0": 0.0, "t1": 2.0, "speaker": "Ann", "text": "one"},
        {"idx": 1, "t0": 2.0, "t1": 3.0, "speaker": "", "text": "two"},
    ]
    t = transcript.from_segments(
        "s", stored, CAPTION, "beelm.transcript/1", speaker_source="diarized"
    )
    assert [s["speaker"] for s in t["segments"]] == ["Ann", ""]
    assert t["speaker_source"] == "diarized"
    assert transcript.loads(transcript.dumps(t)) == t


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
    t = transcript.from_segments("edgeCase_-1", track, CAPTION, "ytlm.transcript/1")
    assert transcript.dumps(t) == (GOLDEN / "edge_cases.transcript.json").read_text()
    assert transcript.to_srt(t) == (GOLDEN / "edge_cases.srt").read_text()


def test_dumps_is_canonical_and_loads_round_trips_it():
    t = _document()
    text = transcript.dumps(t)
    assert text.endswith("}\n") and list(json.loads(text)) == sorted(t)
    assert transcript.loads(text) == t
    assert transcript.dumps(transcript.loads(text)) == text


def test_srt_timestamps_and_wrapping():
    t = transcript.from_segments(
        "x",
        (
            Segment(1.2, 3.36, "All right, so here we are, in front of the elephants"),
            Segment(3661.5, 3662.0, "later"),
        ),
        CAPTION,
        "ytlm.transcript/1",
    )
    assert transcript.to_srt(t, max_line_chars=42) == (
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

    def ambiguity(key, idx, span):
        return {
            "key": key,
            "idx": idx,
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

_seconds = st.floats(min_value=0, max_value=36_000, allow_nan=False, allow_infinity=False)
_text = st.text(alphabet=st.sampled_from(list("ab cé\n\t&;amp")), max_size=30)
_segments = st.lists(
    st.builds(lambda t0, d, text: Segment(t0, t0 + d, text), _seconds, _seconds, _text),
    max_size=25,
)


def _kept(segments):
    return [s for s in segments if re.sub(r"\s+", " ", html.unescape(s.text)).strip()]


@given(segments=_segments)
def test_from_segments_yields_sorted_disjoint_valid_segments_that_start_where_their_source_does(
    segments,
):
    t = transcript.from_segments("vid00000000", segments, CAPTION, "ytlm.transcript/1")
    out = t["segments"]
    assert [s["idx"] for s in out] == list(range(len(out)))
    assert sorted(round(s.t0, 3) for s in _kept(segments)) == [s["t0"] for s in out]
    for a, b in zip(out, out[1:], strict=False):
        assert a["t0"] <= b["t0"] and a["t1"] <= b["t0"]
    for s in out:
        assert s["t0"] <= s["t1"] and s["text"] == s["text"].strip() and "  " not in s["text"]
    transcript.loads(transcript.dumps(t))


@given(ms=st.integers(min_value=0, max_value=100 * 3_600_000 - 1))
def test_an_srt_time_reads_back_to_the_millisecond(ms):
    m = re.fullmatch(r"(\d{2,}):(\d{2}):(\d{2}),(\d{3})", transcript.srt_time(ms / 1000))
    assert m
    h, mi, s, frac = (int(x) for x in m.groups())
    assert mi < 60 and s < 60
    assert ((h * 60 + mi) * 60 + s) * 1000 + frac == ms


@given(segments=_segments, width=st.integers(min_value=10, max_value=80))
def test_every_srt_entry_is_numbered_and_timed_from_its_segment(segments, width):
    t = transcript.from_segments("vid00000000", segments, CAPTION, "ytlm.transcript/1")
    blocks = [b for b in transcript.to_srt(t, max_line_chars=width).split("\n\n") if b]
    assert len(blocks) == len(t["segments"])
    for block, seg in zip(blocks, t["segments"], strict=True):
        number, times, *lines = block.rstrip("\n").split("\n")
        assert number == str(seg["idx"] + 1)
        assert times == f"{transcript.srt_time(seg['t0'])} --> {transcript.srt_time(seg['t1'])}"
        # wrapping moves line breaks, never a character
        assert "".join(lines).replace(" ", "") == seg["text"].replace(" ", "")
        assert all(len(line) <= width for line in lines) or lines == [seg["text"]]
