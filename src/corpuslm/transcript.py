"""Transcript: deterministic transforms from timed text to a transcript document to SRT.

A transcript document is a dict (the `transcript.json` file of a source): `segments` carry
idx, t0, t1, speaker, text; `words` is empty until an audio step exists; `speaker` is '' until
role labelling; `state` is RAW until clean-up runs (corpuslm.cleanup). `from_segments` builds
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
- Every SRT entry is numbered and starts at its segment's start. Enforced by:
  tests/test_transcript.py::test_every_srt_entry_is_numbered_and_timed_from_its_segment.

Terms:
- **segment**: one timed unit of transcript text (`idx`, `t0`, `t1`). Never re-cut;
  edits stay inside it.
- **SRT**: the subtitle file derived deterministically from segments.
- **transcript document**: the dict of one source's transcript: segments with speaker
  fields, state `RAW`, `DETERMINISTIC-ONLY` or `LLM-CLEANED`.
"""

from __future__ import annotations

import html
import json
import re
import textwrap
from collections.abc import Iterable, Mapping
from typing import Any

from . import schemas

_WS = re.compile(r"\s+")


def _cue(seg: Any) -> tuple[float, float, str, str]:
    """(t0, t1, text, speaker) of a Segment or of a stored segment mapping."""
    if isinstance(seg, Mapping):
        return float(seg["t0"]), float(seg["t1"]), seg["text"], seg.get("speaker", "")
    return float(seg.t0), float(seg.t1), seg.text, ""


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
    for seg in segments:
        t0, t1, raw_text, speaker = _cue(seg)
        text = _WS.sub(" ", html.unescape(raw_text)).strip()
        if not text:
            continue
        cues.append([t0, float(max(t1, t0)), text, speaker])
    cues.sort(key=lambda c: c[0])
    # Auto-captions roll: each cue's end overlaps the next cue's start.
    # Clamp so a segment never outlives its successor; SRT and search
    # both want disjoint spans.
    for i in range(len(cues) - 1):
        if cues[i][1] > cues[i + 1][0]:
            cues[i][1] = cues[i + 1][0]
    return {
        # Legacy name: the YouTube tool's key; see the module docstring.
        "video_id": source_id,
        "schema": schema_id,
        "state": "RAW",
        "caption": dict(caption),
        "speaker_source": speaker_source,
        "segments": [
            {"idx": i, "t0": round(t0, 3), "t1": round(t1, 3), "speaker": speaker, "text": text}
            for i, (t0, t1, text, speaker) in enumerate(cues)
        ],
        "words": [],
    }


def dumps(t: dict, *, refined_schema: dict | None = None) -> str:
    """Canonical bytes: stable key order, so staleness is byte comparison. Validated at
    write; a tool passes its refined schema."""
    schemas.validate("transcript", t, refined_schema=refined_schema)
    return json.dumps(t, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def loads(text: str, *, refined_schema: dict | None = None) -> dict:
    """Validated at read: a hand-broken file is rejected naming the property."""
    t = json.loads(text)
    schemas.validate("transcript", t, refined_schema=refined_schema)
    return t


def srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(t: dict, *, max_line_chars: int = 42) -> str:
    out = []
    for seg in t["segments"]:
        lines = textwrap.wrap(seg["text"], width=max_line_chars) or [seg["text"]]
        out.append(
            f"{seg['idx'] + 1}\n{srt_time(seg['t0'])} --> {srt_time(seg['t1'])}\n"
            + "\n".join(lines)
            + "\n"
        )
    return "\n".join(out)


def timestamp(seconds: float) -> str:
    """Human timestamp for notes and search hits: m:ss or h:mm:ss."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
