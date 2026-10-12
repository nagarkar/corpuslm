"""Transcript: deterministic transforms from timed text to a transcript document to SRT.

A transcript document is a dict (the `transcript.json` file of a source): `segments` carry
idx, t0, t1, speaker, text; `words` is empty until an audio step exists; `speaker` is '' until
role labelling; `state` is RAW until cleanup runs (corpuslm.basic_cleanup). `from_segments` builds
the document from any source's timed text, `dumps` writes its canonical bytes, `loads` reads
and validates, `to_srt` derives the subtitle file.

What a tool adds stays in the tool: ytlm checks the 11-character YouTube video id, names the
schema `ytlm.transcript/1` and turns a caption track into `from_segments` input; beelm names
`beelm.transcript/1`.

Never imports: db, store, clikit.

Decisions:
- The in-file key for the source id stays `video_id`, a legacy of the YouTube tool: renaming
  it would change stored bytes and force a model rebuild.
- `dumps` is canonical (stable key order, one trailing newline), so staleness is a byte
  comparison; a document is validated at write and at read. Enforced by:
  tests/test_transcript.py::test_dumps_is_canonical_and_loads_round_trips_it.
- A source with fixed labels gets a schema whose segment `role` is any label
  (`with_free_labels`). Enforced by:
  tests/test_transcript.py::test_free_labels_accept_a_label_the_shared_schema_does_not_list.
- Every SRT entry is numbered and starts at its segment's start. Enforced by:
  tests/test_transcript.py::test_every_srt_entry_is_numbered_and_timed_from_its_segment.

Terms:
- **SRT**: the subtitle file derived deterministically from segments.
- **transcript document**: the dict of one source's transcript: segments with speaker
  fields, state `RAW`, `DETERMINISTIC-ONLY` or `LLM-CLEANED`.
"""

from __future__ import annotations

import copy
import html
import json
import re
import textwrap
from collections.abc import Iterable, Mapping
from typing import Any

from . import schemas

_WS = re.compile(r"\s+")


def _cue(segment: Any) -> tuple[float, float, str, str]:
    """(t0, t1, text, speaker) of a Segment or of a stored segment mapping."""
    if isinstance(segment, Mapping):
        return (
            float(segment["t0"]),
            float(segment["t1"]),
            segment["text"],
            segment.get("speaker", ""),
        )
    return float(segment.start_seconds), float(segment.end_seconds), segment.text, ""


def from_segments(
    source_id: str,
    segments: Iterable[Any],
    caption: Mapping[str, Any],
    schema_id: str,
    *,
    speaker_source: str = "",
) -> dict:
    """The RAW transcript document of one source. `segments` are `corpuslm.model.Segment`s
    or mappings with t0, t1, text and optionally speaker; `caption` is the provenance block
    (language, generated, fetcher); `schema_id` is the writing tool's (`ytlm.transcript/1`)."""
    cues = []
    for segment in segments:
        start_seconds, end_seconds, raw_text, speaker = _cue(segment)
        text = _WS.sub(" ", html.unescape(raw_text)).strip()
        if not text:
            continue
        cues.append([start_seconds, float(max(end_seconds, start_seconds)), text, speaker])
    cues.sort(key=lambda cue: cue[0])
    # Auto-captions roll: each cue's end overlaps the next cue's start.
    # Clamp so a segment never outlives its successor; SRT and search
    # both want disjoint spans.
    for cue_index in range(len(cues) - 1):
        if cues[cue_index][1] > cues[cue_index + 1][0]:
            cues[cue_index][1] = cues[cue_index + 1][0]
    return {
        # Legacy name: the YouTube tool's key; see the module docstring.
        "video_id": source_id,
        "schema": schema_id,
        "state": "RAW",
        "caption": dict(caption),
        "speaker_source": speaker_source,
        "segments": [
            {
                "idx": cue_index,
                "t0": round(start_seconds, 3),
                "t1": round(end_seconds, 3),
                "speaker": speaker,
                "text": text,
            }
            for cue_index, (start_seconds, end_seconds, text, speaker) in enumerate(cues)
        ],
        "words": [],
    }


def with_free_labels(refined_schema: dict | None = None) -> dict:
    """The transcript schema (or a tool's refinement of it) with a segment's `role` free to be
    any label. The shared schema lists YouTube's labels; a source with fixed labels (Bee's
    diarization) carries the source's own."""
    free = copy.deepcopy(
        refined_schema if refined_schema is not None else schemas.schema("transcript")
    )
    role = free["properties"]["segments"]["items"]["properties"]["role"]
    role.pop("enum", None)
    return free


def dumps(document: dict, *, refined_schema: dict | None = None) -> str:
    """Canonical bytes: stable key order, so staleness is byte comparison. Validated at
    write; a tool passes its refined schema."""
    schemas.validate("transcript", document, refined_schema=refined_schema)
    return json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def loads(text: str, *, refined_schema: dict | None = None) -> dict:
    """Validated at read: a hand-broken file is rejected naming the property."""
    document = json.loads(text)
    schemas.validate("transcript", document, refined_schema=refined_schema)
    return document


def srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def to_srt(document: dict, *, max_line_chars: int = 42) -> str:
    out = []
    for segment in document["segments"]:
        lines = textwrap.wrap(segment["text"], width=max_line_chars) or [segment["text"]]
        out.append(
            f"{segment['idx'] + 1}\n{srt_time(segment['t0'])} --> {srt_time(segment['t1'])}\n"
            + "\n".join(lines)
            + "\n"
        )
    return "\n".join(out)


def timestamp(seconds: float) -> str:
    """Human timestamp for notes and search hits: m:ss or h:mm:ss."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, whole_seconds = divmod(remainder, 60)
    return (
        f"{hours}:{minutes:02d}:{whole_seconds:02d}" if hours else f"{minutes}:{whole_seconds:02d}"
    )
