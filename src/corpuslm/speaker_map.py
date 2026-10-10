"""Speaker map: who each speaker label is, from a source's own data and a model's anchored evidence.

A source labels its speakers in its own way, and the way is a `SpeakerScheme`: the label
vocabulary, the label that means "nobody identified", how a label resolves to a name without
any evidence, the prompt that cleans transcripts of that source, whether the labels are fixed
(a diarization the model must not change) and the prefix of the review keys. YouTube's scheme
(HOST, GUEST_n, `YOUTUBE`) and Bee's (`bee_scheme`) are both built here.

Deterministic. Candidate names come from the source's vocabulary; labels come from the
transcript's segments (`role`); label-to-name bindings come from the model clean-up's
anchored dialogue evidence (corpuslm.model_cleanup). A label resolves by default when the
scheme says so (RESOLVED_FROM_METADATA); one consistent confident binding is INFERRED; a label
with no binding, a weak one or conflicting ones is NEEDS_AUTHOR with a stable review key
`<prefix>-N`, and the label is used in the transcript meanwhile. Author verdicts arrive as
AUTHOR rows and outrank everything here.

Never imports: db, store, clikit, any model transport.

Decisions:
- A scheme is data, not code paths: the functions here read it and never name a source.
  Enforced by: tests/test_speaker_map.py::test_the_bee_scheme_resolves_only_the_owner_label.
- Fixed labels are never assigned or changed by a model; a model may only propose names for
  them. Enforced by:
  tests/test_model_cleanup.py::test_fixed_labels_reject_a_reply_that_relabels_a_speaker.
- A label with no binding, a weak binding or a conflict is NEEDS_AUTHOR and goes to review
  without blocking. Enforced by:
  tests/test_speaker_map.py::test_a_label_with_no_binding_or_a_conflict_needs_the_author.
- A binding below LOW_CONFIDENCE is a question for the author, not a fact.

Terms:
- **speaker scheme**: how one source labels its speakers and resolves a label to a name.
- **fixed labels**: labels that come from the source (a diarization) and that no model may
  assign or change.
- **label**: a speaker label per segment, stored as the segment's `role` (the stored key keeps
  its name).
- **speaker map**: per source, label to name with status `RESOLVED_FROM_METADATA`, `INFERRED`,
  `AUTHOR` or `NEEDS_AUTHOR`.
- **NEEDS_AUTHOR**: speaker map status for a label no one could name with confidence; becomes
  a review item.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

LOW_CONFIDENCE = 0.6  # below this a binding goes to the author


@dataclass(frozen=True, slots=True)
class SpeakerScheme:
    labels: tuple[str, ...]  # the vocabulary, in display order; may be empty when fixed
    unresolved_label: str | None  # the label meaning "nobody identified"; None if there is none
    resolve_default: Callable[[str, dict], str | None]  # (label, source) -> name or None
    prompt_id: str  # the clean-up prompt for sources that use this scheme
    fixed_labels: bool  # labels come from the source; a model may not assign or change them
    review_key_prefix: str = "spk"  # review keys are `<prefix>-N`
    source_id_key: str = "video_id"  # the map's key for the source's id (stored bytes)


def ordered_labels(scheme: SpeakerScheme, present: Iterable[str]) -> list[str]:
    """The labels in `present`, scheme labels first in scheme order (never the unresolved
    one), then, for fixed labels only, any other label in the order first seen."""
    seen = list(dict.fromkeys(present))
    out = [x for x in scheme.labels if x != scheme.unresolved_label and x in seen]
    if scheme.fixed_labels:
        out += [x for x in seen if x not in scheme.labels and x != scheme.unresolved_label]
    return out


def labels_present(t: dict, *, scheme: SpeakerScheme) -> list[str]:
    return ordered_labels(scheme, (seg.get("role") for seg in t["segments"] if seg.get("role")))


def build_map(
    source: dict,
    t: dict,
    bindings: list[dict],
    *,
    candidates: list[str],
    scheme: SpeakerScheme,
    author: dict | None = None,
) -> dict:
    """`author` maps label to name for AUTHOR-ratified verdicts."""
    author = author or {}
    roles: dict[str, dict] = {}
    key_n = 0
    for role in labels_present(t, scheme=scheme):
        if role in author:
            roles[role] = {"name": author[role], "status": "AUTHOR"}
            continue
        names = {}
        for b in bindings:
            if b.get("role") == role and b.get("name"):
                names.setdefault(b["name"], b)
        if len(names) == 1:
            name, b = next(iter(names.items()))
            confidence = float(b.get("confidence", 0.8))
            if confidence >= LOW_CONFIDENCE:
                roles[role] = {
                    "name": name,
                    "status": "INFERRED",
                    "confidence": confidence,
                    "anchor": b.get("anchor", ""),
                    "idx": int(b.get("idx", 0)),
                }
                continue
            # a weak binding is a question for the author, not a fact
        if len(names) == 0:
            default = scheme.resolve_default(role, source)
            if default:
                roles[role] = {"name": default, "status": "RESOLVED_FROM_METADATA"}
                continue
        key_n += 1
        entry = {"name": "", "status": "NEEDS_AUTHOR", "key": f"{scheme.review_key_prefix}-{key_n}"}
        if names:
            entry["anchor"] = "; ".join(f"{n} ({b.get('anchor', '')})" for n, b in names.items())
        roles[role] = entry
    return {
        scheme.source_id_key: source["id"],
        "candidates": list(dict.fromkeys(candidates)),
        "roles": roles,
    }


def apply_map(t: dict, m: dict, *, scheme: SpeakerScheme) -> dict:
    for seg in t["segments"]:
        role = seg.get("role")
        if not role:
            continue
        entry = m["roles"].get(role)
        if scheme.unresolved_label is not None and role == scheme.unresolved_label:
            seg["speaker"] = ""
        elif entry and entry.get("name"):
            seg["speaker"] = entry["name"]
        else:
            seg["speaker"] = role
    return t


def review_items(m: dict) -> list[str]:
    return [e["key"] for e in m["roles"].values() if e.get("status") == "NEEDS_AUTHOR"]


def display_names(m: dict) -> dict[str, str]:
    """label to what a note shows: the name, else the label."""
    return {role: (e.get("name") or role) for role, e in m["roles"].items()}


def bee_scheme(owner_first_name: str) -> SpeakerScheme:
    """Bee's speaker labels are its own diarization, so they are fixed. The label equal to the
    owner's first name resolves to that name; every other label ('Unknown', 'Speaker 2', ...)
    is unresolved and becomes a review item for the owner."""

    def resolve_default(label: str, source: dict) -> str | None:
        return owner_first_name if owner_first_name and label == owner_first_name else None

    return SpeakerScheme(
        labels=(),
        unresolved_label=None,
        resolve_default=resolve_default,
        prompt_id="cleanup_bee",
        fixed_labels=True,
        source_id_key="source_id",
    )


def _host_is_the_channel(label: str, source: dict) -> str | None:
    return source.get("channel_title") if label == "HOST" else None


# YouTube's labels: HOST, GUEST_1 to GUEST_6 and UNKNOWN; HOST resolves to the channel title.
YOUTUBE = SpeakerScheme(
    labels=("HOST", "GUEST_1", "GUEST_2", "GUEST_3", "GUEST_4", "GUEST_5", "GUEST_6", "UNKNOWN"),
    unresolved_label="UNKNOWN",
    resolve_default=_host_is_the_channel,
    prompt_id="cleanup",
    fixed_labels=False,
)
