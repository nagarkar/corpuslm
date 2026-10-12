"""AI cleanup: the batching, reply bounds and edits of the step that asks a model to clean a source.

The AI cleanup is the step after the basic cleanup (corpuslm.basic_cleanup). This module
holds its source-agnostic mechanics and calls no model.

Batches: fixed input-token budgets cut on segment boundaries, the previous batch's tail
repeated read-only, a running label summary carried forward. Replies: edits only (changed
segments by index), a label per segment, consequential ambiguities, label-to-name bindings.
Bounds are strict: an index outside the batch is a BatchError; an edit that would remove a
protected span is declined and counted; segment boundaries never change.

How a source labels its speakers is a `SpeakerScheme` (corpuslm.speaker_map), passed as
`scheme=`. With fixed labels the model never assigns or changes a label: a reply naming a
different label than the segment carries is a BatchError (and `check_bounds` rejects it
before the reply is cached); it may still propose name bindings and edits.

The stored keys of a document keep their legacy names (`role`, `role_confidence`, `raw`,
`amb-NNNN`, `passes.pass3`): renaming them would change stored bytes and force a paid rebuild.

corpuslm ships `cleanup_bee_v1.md` for the Bee scheme among its built-in prompts
(corpuslm.prompts); a scheme's `prompt_id` names the prompt a source's cleanup uses.

Never imports: db, store, clikit, any model transport.

Decisions:
- Segments are never re-cut. Why: stable segments keep anchors, SRT cues, FTS rows and review
  keys stable. Enforced by:
  tests/test_ai_cleanup.py::test_apply_never_changes_segment_boundaries_and_protects_spans.
- A batch is the unit of cache key, retry and spend; batches cut on segment boundaries with a
  read-only overlap. Enforced by:
  tests/test_ai_cleanup.py::test_batches_cut_on_segment_boundaries_with_read_only_overlap.
- Replies carry edits only: untouched segments cost no output tokens.
- A stray edit index is dropped and counted, never a failed transcript; a stray label,
  ambiguity or binding index is an error. Enforced by:
  tests/test_ai_cleanup.py::test_apply_rejects_an_index_outside_the_batch_and_protects_spans.
- With fixed labels a segment's `role` is the source's own speaker label. Enforced by:
  tests/test_ai_cleanup.py::test_fixed_labels_are_adopted_from_the_speakers.
- A fixed-label scheme rejects any reply that assigns or changes a label. Enforced by:
  tests/test_ai_cleanup.py::test_fixed_labels_reject_a_reply_that_relabels_a_speaker.
- New ambiguities are capped per source; past the cap the model's pick stands. Enforced by:
  tests/test_ai_cleanup.py::test_cap_ambiguities_keeps_the_least_confident_pending.

Terms:
- **AI cleanup**: the step after the basic cleanup that asks a model, batch by batch, for edits,
  speaker label assignments, ambiguities and name bindings. Stored under the node names `pass3`
  (ytlm) and `model_cleanup` (beelm).
- **batch**: the segments one model call edits, plus a read-only tail of the previous batch.
  Never called a window.
- **candidate**: one possible wording of an ambiguous span; the words as heard are always among
  them. A review card offers the candidates, the pick and the words as heard
  (`review.candidates_offered`).
- **ambiguity**: a consequential span the model was unsure of (a name, a term, or words that
  change meaning), with its candidates, probabilities and pick. Filler and punctuation are
  never ambiguities. Review key `amb-NNNN`.
- **anchor**: a verbatim quote string-matched against segment text; a binding or ambiguity
  without a matching anchor is dropped and counted.
- **pick**: the candidate the model chose and applied at once.
"""

from __future__ import annotations

from dataclasses import dataclass

from .speaker_map import SpeakerScheme


class BatchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Batch:
    number: int
    start: int  # first editable segment index (inclusive)
    end: int  # last editable segment index (exclusive)
    context: tuple[int, int]  # read-only tail of the previous batch [a, b)

    @property
    def tag_suffix(self) -> str:
        return f"b{self.number}"


def adopt_fixed_labels(document: dict) -> None:
    """With fixed labels the segment's `role` is the source's own speaker label: set it, where
    the segment has a speaker and no role yet, so the batches show it and the speaker map
    lists it. Segments with no speaker stay unlabelled."""
    for segment in document["segments"]:
        if segment.get("speaker") and not segment.get("role"):
            segment["role"] = segment["speaker"]


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4 + 1)


def batches(document: dict, *, batch_tokens: int, overlap_segments: int) -> list[Batch]:
    segments = document["segments"]
    out: list[Batch] = []
    start = 0
    number = 0
    while start < len(segments):
        used = 0
        end = start
        while end < len(segments):
            cost = estimate_tokens(segments[end]["text"]) + 4
            if end > start and used + cost > batch_tokens:
                break
            used += cost
            end += 1
        context = (max(0, start - overlap_segments), start) if start else (0, 0)
        out.append(Batch(number=number, start=start, end=end, context=context))
        start = end
        number += 1
    return out


def render(document: dict, batch: Batch, *, scheme: SpeakerScheme) -> str:
    """The batch as `[index] text` lines, read-only context first. With fixed labels each line
    also shows its label (`[index] label: text`), because the model must see them."""

    def line(segment_index: int, mark: str) -> str:
        segment = document["segments"][segment_index]
        label = f"{segment['role']}: " if scheme.fixed_labels and segment.get("role") else ""
        return f"[{segment_index}] {mark}{label}{segment['text']}"

    lines = [line(segment_index, "(context) ") for segment_index in range(*batch.context)]
    lines += [line(segment_index, "") for segment_index in range(batch.start, batch.end)]
    return "\n".join(lines)


def role_summary(document: dict, *, upto: int, bindings: list[dict], scheme: SpeakerScheme) -> str:
    counts: dict[str, int] = {}
    for segment in document["segments"][:upto]:
        role = segment.get("role")
        if role:
            counts[role] = counts.get(role, 0) + 1
    names = {binding["role"]: binding["name"] for binding in bindings if binding.get("name")}
    if not counts and not names:
        return "(none yet)"
    parts = []
    for role in _summary_order(scheme, [*counts, *names]):
        role_phrase = f"{role}: {counts.get(role, 0)} segments so far"
        if role in names:
            role_phrase += f", named {names[role]}"
        parts.append(role_phrase)
    return "; ".join(parts)


def _summary_order(scheme: SpeakerScheme, present: list[str]) -> list[str]:
    """Scheme labels first, in the scheme's order (the unresolved one included); for fixed
    labels, any other label after them in the order first seen."""
    seen = list(dict.fromkeys(present))
    ordered = [label for label in scheme.labels if label in seen]
    if scheme.fixed_labels:
        ordered += [label for label in seen if label not in scheme.labels]
    return ordered


def check_bounds(
    batch: Batch, reply: dict, *, scheme: SpeakerScheme, document: dict | None = None
) -> None:
    """Every index the reply names must be editable in this batch, and an ambiguity's
    probabilities line up with its candidates. Runs before the reply is cached: a bad reply
    is never served from cache. With fixed labels and the transcript `t`, a label that
    differs from the segment's own is rejected here too."""
    editable = range(batch.start, batch.end)
    visible = range(batch.context[0], batch.end) if batch.context[1] else editable
    # Edits outside the editable range are dropped and counted at apply: a single stray index
    # on a four-hour video failed the whole video and re-billed thirty batches on retry.
    for index_raw in reply.get("roles") or {}:
        # A label on a read-only context line is harmless and common (the model labels
        # everything it sees); it is ignored at apply, not an error.
        if int(index_raw) not in visible:
            raise ValueError(
                f"roles: index {index_raw} is outside batch {batch.number} "
                f"[{batch.context[0]}, {batch.end})"
            )
    if scheme.fixed_labels and document is not None:
        for index_raw, label in (reply.get("roles") or {}).items():
            segment_index = int(index_raw)
            if segment_index in editable and label[0] != document["segments"][segment_index].get(
                "role"
            ):
                raise ValueError(
                    f"roles: {label[0]!r} would relabel segment {segment_index} from "
                    f"{document['segments'][segment_index].get('role')!r}; the labels are fixed"
                )
    for item in reply.get("ambiguities") or []:
        if int(item["idx"]) not in editable:
            raise ValueError(
                f"ambiguities: index {item['idx']} is outside batch {batch.number} "
                f"[{batch.start}, {batch.end})"
            )
    # A binding is evidence (an anchor quote on the line where a name is said), so a context
    # line the model saw is good evidence: speakers often name themselves just before a
    # batch starts.
    for item in reply.get("bindings") or []:
        if int(item["idx"]) not in visible:
            raise ValueError(
                f"bindings: index {item['idx']} is outside batch {batch.number} "
                f"[{batch.context[0]}, {batch.end})"
            )
    for ambiguity in reply.get("ambiguities") or []:
        probs = ambiguity.get("probabilities")
        if probs is None:
            continue  # a prompt v1 reply: one confidence for the pick
        if len(probs) != len(ambiguity["candidates"]):
            raise ValueError(
                f"ambiguities: idx {ambiguity['idx']} needs one probability per candidate"
            )
        if sum(probs) <= 0:
            raise ValueError(f"ambiguities: idx {ambiguity['idx']} probabilities sum to zero")
        if ambiguity["pick"] not in ambiguity["candidates"]:
            raise ValueError(
                f"ambiguities: idx {ambiguity['idx']} pick is not one of its candidates"
            )


def apply(
    document: dict, batch: Batch, reply: dict, *, next_key: int, scheme: SpeakerScheme
) -> tuple[dict, dict]:
    """Apply one batch's reply in place. `next_key` is the next ambiguity number for this
    source (keys are amb-NNNN, stable within the source). Returns (t, stats)."""
    segments = document["segments"]
    editable = range(batch.start, batch.end)

    def check(index_raw, what: str, allowed: range = editable) -> int:
        try:
            segment_index = int(index_raw)
        except (TypeError, ValueError) as error:
            raise BatchError(f"{what}: index {index_raw!r} is not an integer") from error
        if segment_index not in allowed:
            raise BatchError(
                f"{what}: index {segment_index} is outside batch {batch.number} "
                f"[{allowed.start}, {allowed.stop})"
            )
        return segment_index

    stats = {
        "edits": 0,
        "protected_violations": 0,
        "roles": 0,
        "ambiguities": 0,
        "bindings": [],
        "dropped_ambiguities": 0,
        "dropped_bindings": 0,
    }
    for index_raw, text in (reply.get("edits") or {}).items():
        if int(index_raw) not in editable:
            stats["stray_edits"] = stats.get("stray_edits", 0) + 1
            continue
        segment_index = check(index_raw, "edits")
        segment = segments[segment_index]
        new = str(text).strip()
        if not new:
            stats["protected_violations"] += 0
            continue
        if any(span not in new for span in segment.get("protected") or []):
            stats["protected_violations"] += 1
            continue
        if new != segment["text"]:
            segment.setdefault("raw", segment["text"])
            segment["text"] = new
            stats["edits"] += 1
    for index_raw, label in (reply.get("roles") or {}).items():
        if int(index_raw) in range(*batch.context):
            stats["context_roles_ignored"] = stats.get("context_roles_ignored", 0) + 1
            continue
        segment_index = check(index_raw, "roles")
        role, confidence = label[0], float(label[1])
        segment = segments[segment_index]
        if scheme.fixed_labels:
            if role != segment.get("role"):
                raise BatchError(
                    f"roles: {role!r} would relabel segment {segment_index} "
                    f"from {segment.get('role')!r}; "
                    "the labels are fixed"
                )
            continue  # a confirmation changes nothing
        if role not in scheme.labels:
            raise BatchError(f"roles: {role!r} is not a role label")
        segment["role"] = role
        segment["role_confidence"] = round(confidence, 3)
        if role != scheme.unresolved_label and not segment.get("speaker"):
            segment["speaker"] = role
        stats["roles"] += 1
    ambiguities = document.setdefault("ambiguities", [])
    for ambiguity in reply.get("ambiguities") or []:
        segment_index = check(ambiguity["idx"], "ambiguities")
        text = segments[segment_index]["text"]
        if (
            ambiguity["span"] not in text
            and ambiguity["pick"] not in text
            and not any(candidate in text for candidate in ambiguity["candidates"])
        ):
            # no anchor in the segment, counted
            stats["dropped_ambiguities"] += 1
            continue
        item = {
            "key": f"amb-{next_key:04d}",
            "idx": segment_index,
            "span": ambiguity["span"],
            "candidates": list(ambiguity["candidates"]),
            "pick": ambiguity["pick"],
            "confidence": round(float(ambiguity.get("confidence", 0)), 3),
            "status": "pending",
        }
        if ambiguity.get("probabilities") is not None:
            total = sum(ambiguity["probabilities"])
            item["probabilities"] = [
                round(probability / total, 3) for probability in ambiguity["probabilities"]
            ]
            item["confidence"] = item["probabilities"][
                ambiguity["candidates"].index(ambiguity["pick"])
            ]
        ambiguities.append(item)
        next_key += 1
        stats["ambiguities"] += 1
    known = {segment.get("role") for segment in segments}
    for binding in reply.get("bindings") or []:
        segment_index = check(
            binding["idx"],
            "bindings",
            range(batch.context[0], batch.end) if batch.context[1] else editable,
        )
        if scheme.fixed_labels and binding["role"] not in known:
            stats["dropped_bindings"] += 1  # a name for a label no segment carries
        elif binding["anchor"] and binding["anchor"] in segments[segment_index]["text"]:
            stats["bindings"].append(
                {
                    "role": binding["role"],
                    "name": binding["name"],
                    "idx": segment_index,
                    "anchor": binding["anchor"],
                    "confidence": round(float(binding.get("confidence", 0.8)), 3),
                }
            )
        else:
            stats["dropped_bindings"] += 1
    stats["next_key"] = next_key
    return document, stats


def drop_unanchored(document: dict) -> int:
    """After the memory patch and the verdicts, a pending item whose span, pick and
    candidates no longer appear in its segment (an overlapping substitution rewrote it) has
    nothing to ask about: dropped and counted, never a failed transcript."""
    kept, dropped = [], 0
    for ambiguity in document.get("ambiguities") or []:
        text = document["segments"][ambiguity["idx"]]["text"]
        needles = [
            ambiguity["span"],
            ambiguity["pick"],
            *ambiguity["candidates"],
            ambiguity.get("resolution", ""),
        ]
        if any(number and number in text for number in needles):
            kept.append(ambiguity)
        else:
            dropped += 1
    document["ambiguities"] = kept
    return dropped


def cap_ambiguities(document: dict, *, max_new: int) -> int:
    """Beyond the cap the model's pick stands and the item is marked capped, lowest
    confidence first kept pending. Returns the capped count."""
    pending = [
        ambiguity
        for ambiguity in document.get("ambiguities") or []
        if ambiguity["status"] == "pending"
    ]
    if max_new <= 0 or len(pending) <= max_new:
        return 0
    pending.sort(key=lambda ambiguity: ambiguity["confidence"])
    capped = 0
    for ambiguity in pending[max_new:]:
        ambiguity["status"] = "capped"
        capped += 1
    return capped
