"""Speaker map: who each speaker label is, from a source's own data and a model's anchored evidence.

A source labels its speakers in its own way, and the way is a `SpeakerScheme`: the label
vocabulary, the label that means "nobody identified", how a label resolves to a name without
any evidence, the prompt that cleans transcripts of that source, whether the labels are fixed
(a diarization the model must not change) and the prefix of the review keys. YouTube's scheme
(HOST, GUEST_n, `YOUTUBE`) and Bee's (`bee_scheme`) are both built here.

Deterministic. Candidate names come from the source's vocabulary; labels come from the
transcript's segments (`role`); label-to-name bindings come from the AI cleanup's
anchored dialogue evidence (corpuslm.ai_cleanup). A label resolves by default when the
scheme says so (RESOLVED_FROM_METADATA); one consistent confident binding is INFERRED; a label
with no binding, a weak one or conflicting ones is NEEDS_AUTHOR with a stable review key
`<prefix>-N`, and the label is used in the transcript meanwhile. Owner verdicts arrive as
AUTHOR rows and outrank everything here.

Never imports: db, store, clikit, any model transport.

Decisions:
- A scheme is data, not code paths: the functions here read it and never name a source.
  Enforced by: tests/test_speaker_map.py::test_the_bee_scheme_resolves_only_the_owner_label.
- Fixed labels are never assigned or changed by a model; a model may only propose names for
  them. Enforced by:
  tests/test_ai_cleanup.py::test_fixed_labels_reject_a_reply_that_relabels_a_speaker.
- A label with no binding, a weak binding or a conflict is NEEDS_AUTHOR and goes to review
  without blocking. Enforced by:
  tests/test_speaker_map.py::test_a_label_with_no_binding_or_a_conflict_needs_the_owner.
- A binding below LOW_CONFIDENCE is a question for the owner, not a fact.

Terms:
- **speaker scheme**: how one source labels its speakers and resolves a label to a name.
- **fixed labels**: labels that come from the source (a diarization) and that no model may
  assign or change.
- **speaker label**: the label a source gives a segment's speaker, stored as the segment's
  `role` (the stored key keeps its name).
- **speaker map**: per source, label to name with status `RESOLVED_FROM_METADATA`, `INFERRED`,
  `AUTHOR` (the owner's) or `NEEDS_AUTHOR`.
- **needs-owner status**: speaker map status for a label no one could name with confidence;
  becomes a review item. Stored as `NEEDS_AUTHOR`; the owner's own status is stored as
  `AUTHOR`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

LOW_CONFIDENCE = 0.6  # below this a binding goes to the owner


@dataclass(frozen=True, slots=True)
class SpeakerScheme:
    labels: tuple[str, ...]  # the vocabulary, in display order; may be empty when fixed
    unresolved_label: str | None  # the label meaning "nobody identified"; None if there is none
    resolve_default: Callable[[str, dict], str | None]  # (label, source) -> name or None
    prompt_id: str  # the cleanup prompt for sources that use this scheme
    fixed_labels: bool  # labels come from the source; a model may not assign or change them
    review_key_prefix: str = "spk"  # review keys are `<prefix>-N`
    source_id_key: str = "video_id"  # the map's key for the source's id (stored bytes)


def ordered_labels(scheme: SpeakerScheme, present: Iterable[str]) -> list[str]:
    """The labels in `present`, scheme labels first in scheme order (never the unresolved
    one), then, for fixed labels only, any other label in the order first seen."""
    seen = list(dict.fromkeys(present))
    out = [label for label in scheme.labels if label != scheme.unresolved_label and label in seen]
    if scheme.fixed_labels:
        out += [
            label
            for label in seen
            if label not in scheme.labels and label != scheme.unresolved_label
        ]
    return out


def labels_present(document: dict, *, scheme: SpeakerScheme) -> list[str]:
    return ordered_labels(
        scheme, (segment.get("role") for segment in document["segments"] if segment.get("role"))
    )


def build_map(
    source_row: dict,
    document: dict,
    bindings: list[dict],
    *,
    candidates: list[str],
    scheme: SpeakerScheme,
    owner_names: dict | None = None,
) -> dict:
    """`author` maps label to name for AUTHOR-ratified verdicts."""
    owner_names = owner_names or {}
    roles: dict[str, dict] = {}
    key_n = 0
    for role in labels_present(document, scheme=scheme):
        if role in owner_names:
            roles[role] = {"name": owner_names[role], "status": "AUTHOR"}
            continue
        names = {}
        for binding in bindings:
            if binding.get("role") == role and binding.get("name"):
                names.setdefault(binding["name"], binding)
        if len(names) == 1:
            name, binding = next(iter(names.items()))
            confidence = float(binding.get("confidence", 0.8))
            if confidence >= LOW_CONFIDENCE:
                roles[role] = {
                    "name": name,
                    "status": "INFERRED",
                    "confidence": confidence,
                    "anchor": binding.get("anchor", ""),
                    "idx": int(binding.get("idx", 0)),
                }
                continue
            # a weak binding is a question for the owner, not a fact
        if len(names) == 0:
            default = scheme.resolve_default(role, source_row)
            if default:
                roles[role] = {"name": default, "status": "RESOLVED_FROM_METADATA"}
                continue
        key_n += 1
        entry = {"name": "", "status": "NEEDS_AUTHOR", "key": f"{scheme.review_key_prefix}-{key_n}"}
        if names:
            entry["anchor"] = "; ".join(
                f"{name} ({binding.get('anchor', '')})" for name, binding in names.items()
            )
        roles[role] = entry
    return {
        scheme.source_id_key: source_row["id"],
        "candidates": list(dict.fromkeys(candidates)),
        "roles": roles,
    }


def apply_map(document: dict, speaker_map: dict, *, scheme: SpeakerScheme) -> dict:
    for segment in document["segments"]:
        role = segment.get("role")
        if not role:
            continue
        entry = speaker_map["roles"].get(role)
        if scheme.unresolved_label is not None and role == scheme.unresolved_label:
            segment["speaker"] = ""
        elif entry and entry.get("name"):
            segment["speaker"] = entry["name"]
        else:
            segment["speaker"] = role
    return document


def review_items(speaker_map: dict) -> list[str]:
    return [
        entry["key"]
        for entry in speaker_map["roles"].values()
        if entry.get("status") == "NEEDS_AUTHOR"
    ]


def display_names(speaker_map: dict) -> dict[str, str]:
    """label to what a note shows: the name, else the label."""
    return {role: (entry.get("name") or role) for role, entry in speaker_map["roles"].items()}


def bee_scheme(owner_first_name: str) -> SpeakerScheme:
    """Bee's speaker labels are its own diarization, so they are fixed. The label equal to the
    owner's first name resolves to that name; every other label ('Unknown', 'Speaker 2', ...)
    is unresolved and becomes a review item for the owner."""

    def resolve_default(label: str, source_row: dict) -> str | None:
        return owner_first_name if owner_first_name and label == owner_first_name else None

    return SpeakerScheme(
        labels=(),
        unresolved_label=None,
        resolve_default=resolve_default,
        prompt_id="cleanup_bee",
        fixed_labels=True,
        source_id_key="source_id",
    )


def _host_is_the_channel(label: str, source_row: dict) -> str | None:
    return source_row.get("channel_title") if label == "HOST" else None


# YouTube's labels: HOST, GUEST_1 to GUEST_6 and UNKNOWN; HOST resolves to the channel title.
YOUTUBE = SpeakerScheme(
    labels=("HOST", "GUEST_1", "GUEST_2", "GUEST_3", "GUEST_4", "GUEST_5", "GUEST_6", "UNKNOWN"),
    unresolved_label="UNKNOWN",
    resolve_default=_host_is_the_channel,
    prompt_id="cleanup",
    fixed_labels=False,
)
