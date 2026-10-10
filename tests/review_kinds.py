"""Review kinds and a Bee-shaped fixture for the review tests: a kind with no anchor link (a
`file://` link fails on a phone) and a YouTube-like kind with one, over the same documents."""

from __future__ import annotations

import json
from pathlib import Path

from corpuslm.review import ReviewKind
from corpuslm.speaker_map import bee_scheme
from corpuslm.store import Store

OWNER = "Quill"
# What a Bee-shaped source offers as protected vocabulary: its summary's section headings and one
# conversation-specific heading. None of them is a name.
SUMMARY_HEADINGS = ["Summary", "Key Takeaways", "Action Items", "Atmosphere", "Kiln Day"]
BEE_SOURCE = "bee:me:2026-10-03T09:14:00-07:00"
SECOND_BEE_SOURCE = "bee:me:2026-10-04T18:02:00-07:00"

BEE_KIND = ReviewKind(
    name="bee",
    anchor_url=lambda source, seconds: None,
    source_label=lambda source: source.get("title") or source["id"],
    scheme=bee_scheme(OWNER),
    role_pattern=r"[A-Za-z_0-9 ]+",
)
LINKED_KIND = ReviewKind(
    name="youtube",
    anchor_url=lambda source, seconds: f"https://example.test/{source['id']}?t={int(seconds)}s",
    source_label=lambda source: source.get("title", ""),
    scheme=bee_scheme(OWNER),
    links_note="Links open the player.",
)


def segment(index: int, t0: float, text: str, *, raw: str = "", speaker: str = "") -> dict:
    return {
        "idx": index,
        "t0": t0,
        "t1": t0 + 4.0,
        "speaker": speaker,
        "text": text,
        **({"raw": raw} if raw else {}),
    }


def ambiguity(key: str, idx: int, span: str, pick: str, candidates: list[str], **more) -> dict:
    return {
        "key": key,
        "idx": idx,
        "span": span,
        "pick": pick,
        "candidates": candidates,
        "status": "pending",
        "confidence": 0.5,
        **more,
    }


def bee_transcript(*, ambiguities: list[dict] | None = None) -> dict:
    return {
        "video_id": BEE_SOURCE,  # the legacy stored key for the source's id
        "schema": "beelm.transcript/1",
        "state": "LLM-CLEANED",
        "caption": {"language": "en", "generated": True, "fetcher": "bee"},
        "speaker_source": "diarized",
        "words": [],
        "segments": [
            segment(0, 0.0, "Quill here, notes from the walk.", speaker=OWNER),
            segment(
                1,
                5.0,
                "We should call Wren about the kiln.",
                raw="we should call wren",
                speaker="Unknown",
            ),
            segment(2, 9.5, "Wren said the kiln is ready.", speaker="Unknown"),
        ],
        "ambiguities": ambiguities
        if ambiguities is not None
        else [ambiguity("amb-0001", 1, "wren", "Wren", ["Wren", "Ren"])],
    }


def bee_speaker_map(*, unknown_status: str = "NEEDS_AUTHOR", unknown_name: str = "") -> dict:
    unknown = {"name": unknown_name, "status": unknown_status, "key": "spk-1", "idx": 1}
    if unknown_status == "NEEDS_AUTHOR":
        unknown["anchor"] = "Ren (Ren, are you there?)"
    return {
        "source_id": BEE_SOURCE,
        "candidates": SUMMARY_HEADINGS,
        "roles": {OWNER: {"name": OWNER, "status": "RESOLVED_FROM_METADATA"}, "Unknown": unknown},
    }


def write_source(
    sources_directory: Path, source_id: str, transcript: dict, speaker_map: dict | None = None
) -> None:
    folder = sources_directory / source_id
    folder.mkdir(parents=True)
    (folder / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
    if speaker_map is not None:
        (folder / "speaker_map.json").write_text(json.dumps(speaker_map), encoding="utf-8")


def add_corpus_of_bee_sources(store: Store, name: str, source_ids: list[str]) -> str:
    corpus_id = store.add_corpus({"name": name}, created_by="test")
    for source_id in source_ids:
        store.add_source(source_id, f"transcripts/{source_id}.md", created_by="test", kind="bee")
        store.add_member(corpus_id, source_id, "author", created_by="test")
    return corpus_id
