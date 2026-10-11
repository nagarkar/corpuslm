"""Review kinds: what differs per source kind on a review surface, for the two kinds there are.

`youtube_review()` and `bee_review(owner_first_name)` build the `corpuslm.review.ReviewKind` of
each source kind a tool files in the shared database, so one review server can serve every kind
without importing ytlm or beelm. `kinds_for_settings` builds the kind of each source kind a
settings file names (`corpuslm.review_settings`).

Never imports: cli, clikit, db, any model transport.

Decisions:
- A YouTube card links to the watch URL at the second; the page footnote says how to force the
  browser instead of the app. Enforced by:
  tests/test_review_kinds.py::test_a_youtube_card_links_to_the_watch_url_at_the_second.
- A Bee card has no link (a `file://` link fails on a phone): a time label stands alone, the
  card names the recording by title and day, and the picker offers the owner's first name.
  Enforced by:
  tests/test_review_kinds.py::test_a_bee_card_has_no_link_and_the_picker_offers_the_owner.
- Bee's transcript documents are read with Bee's refinement of the transcript schema: its source
  ids are `bee:<number>` and a segment's label is Bee's own speaker label. Enforced by:
  tests/test_review_kinds.py::test_bee_transcripts_are_read_with_free_speaker_labels.
- A Bee answer lands only through a process that can rebuild what it changes: the kind names
  its tool's verb (`rebuilt_by`), and `corpuslm review apply` leaves a Bee answer unlanded where
  beelm's rebuilder is not registered. Enforced by:
  tests/test_review_apply.py::test_a_kind_without_its_rebuilder_leaves_its_answers_unlanded.
- A source kind outside the two has no review kind; the settings file may not name it. Enforced
  by: tests/test_review_settings.py::test_an_unknown_source_kind_is_named_and_declined.

Terms:
- **review kind**: how one source kind links, labels and names speakers on review surfaces;
  `youtube_review()` and `bee_review()` build them.
"""

from __future__ import annotations

from collections.abc import Mapping

from . import transcript
from .review import ReviewKind
from .speaker_map import YOUTUBE, bee_scheme

BEE_SCHEMA_ID = "beelm.transcript/1"
BEE_LINKS_NOTE = "Times count from the start of each recording; Bee conversations have no link."
YOUTUBE_LINKS_NOTE = (
    "Timestamps open the YouTube app, which ignores a second jump "
    "into a video it already has open. To hear a second spot: close the app's player "
    "and tap again, or long-press the link and open it in a new tab (the browser "
    "player seeks every time)."
)
# Source kinds a review kind exists for.
KINDS = ("youtube", "bee")
# The verb that carries a Bee answer into the files (a speaker name rebuilds the map, the
# transcript and the subtitles), which only beelm can do.
BEE_REBUILT_BY = "./beelm.sh review apply"
# The artifacts of a YouTube source that go stale when the owner answers one of its cards: the
# next run rebuilds them (the cleanup pass patches the transcript at the answer).
YOUTUBE_STALE_NODES = ("pass3", "speaker_map")
# ytlm stores the AI cleanup under its first name (`pass3`); beelm's is `model_cleanup`.
YOUTUBE_AI_CLEANUP_NODE = "pass3"


def youtube_anchor_url(video: dict, seconds: float) -> str:
    # A plain watch URL. On the phone the YouTube app claims it and ignores a second link to a
    # video it already has open; the browser player cannot be used instead: the artifact's CSP
    # allows no YouTube iframe, and a top-level embed opens without the referrer YouTube
    # demands (error 153, 2026-09-22).
    return f"https://www.youtube.com/watch?v={video['id']}&t={int(seconds)}s"


def youtube_review() -> ReviewKind:
    return ReviewKind(
        name="youtube",
        anchor_url=youtube_anchor_url,
        source_label=lambda video: video.get("title", ""),
        scheme=YOUTUBE,
        links_note=YOUTUBE_LINKS_NOTE,
        stale_nodes=YOUTUBE_STALE_NODES,
        ai_cleanup_node=YOUTUBE_AI_CLEANUP_NODE,
    )


def bee_refined_schema() -> dict:
    """The transcript schema with Bee's source id and schema id, and a segment label free to be
    Bee's own speaker label."""
    refined = transcript.with_free_labels()
    refined["$id"] = BEE_SCHEMA_ID
    properties = refined["properties"]
    properties["video_id"] = {
        "type": "string",
        "description": "Bee source id, `bee:<conversation id>` (the key keeps its legacy name).",
        "pattern": "^bee:[0-9]+$",
    }
    properties["schema"] = {
        "type": "string",
        "description": "Schema id and version.",
        "const": BEE_SCHEMA_ID,
    }
    return refined


def bee_review(owner_first_name: str = "") -> ReviewKind:
    """No link to a moment, the recording's title and day as the label, the owner's first name
    offered in the speaker picker."""

    def label(source_row: dict) -> str:
        day = str(source_row.get("published_at") or "")[:10]
        return " · ".join(
            part for part in (source_row.get("title") or source_row["id"], day) if part
        )

    return ReviewKind(
        name="bee",
        anchor_url=lambda source, seconds: None,
        source_label=label,
        scheme=bee_scheme(owner_first_name),
        links_note=BEE_LINKS_NOTE,
        extra_speaker_names=lambda source: [owner_first_name] if owner_first_name else [],
        role_pattern=r"[A-Za-z_0-9 ]+",
        refined_schema=bee_refined_schema(),
        rebuilt_by=BEE_REBUILT_BY,
    )


def kinds_for_settings(owner_first_names: Mapping[str, str]) -> dict[str, ReviewKind]:
    """The review kind of each source kind named, by source kind. Declines a kind there is none
    for."""
    unknown = sorted(set(owner_first_names) - set(KINDS))
    if unknown:
        raise ValueError(
            f"no review kind for source kind {unknown[0]!r}; known: {', '.join(KINDS)}"
        )
    kinds: dict[str, ReviewKind] = {}
    for name, owner in owner_first_names.items():
        kinds[name] = youtube_review() if name == "youtube" else bee_review(owner)
    return kinds


__all__ = [
    "BEE_LINKS_NOTE",
    "BEE_REBUILT_BY",
    "BEE_SCHEMA_ID",
    "KINDS",
    "YOUTUBE_LINKS_NOTE",
    "YOUTUBE_AI_CLEANUP_NODE",
    "YOUTUBE_STALE_NODES",
    "bee_refined_schema",
    "bee_review",
    "kinds_for_settings",
    "youtube_anchor_url",
    "youtube_review",
]
