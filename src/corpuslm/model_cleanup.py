"""Model clean-up: the batching, reply bounds and edits of the model's residual clean-up pass.

The model clean-up is the pass after the deterministic ones (corpuslm.cleanup). This module
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
(corpuslm.prompts); a scheme's `prompt_id` names the prompt a source's clean-up uses.

Never imports: db, store, clikit, any model transport.

Decisions:
- Segments are never re-cut. Why: stable segments keep anchors, SRT cues, FTS rows and review
  keys stable. Enforced by:
  tests/test_model_cleanup.py::test_apply_never_changes_segment_boundaries_and_protects_spans.
- A batch is the unit of cache key, retry and spend; batches cut on segment boundaries with a
  read-only overlap. Enforced by:
  tests/test_model_cleanup.py::test_batches_cut_on_segment_boundaries_with_read_only_overlap.
- Replies carry edits only: untouched segments cost no output tokens.
- A stray edit index is dropped and counted, never a failed transcript; a stray label,
  ambiguity or binding index is an error. Enforced by:
  tests/test_model_cleanup.py::test_apply_rejects_an_index_outside_the_batch_and_protects_spans.
- A fixed-label scheme rejects any reply that assigns or changes a label. Enforced by:
  tests/test_model_cleanup.py::test_fixed_labels_reject_a_reply_that_relabels_a_speaker.
- New ambiguities are capped per source; past the cap the model's pick stands. Enforced by:
  tests/test_model_cleanup.py::test_cap_ambiguities_keeps_the_least_confident_pending.

Terms:
- **batch**: the segments one model call edits, plus a read-only tail of the previous batch.
  Never called a window.
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
    n: int
    start: int  # first editable segment index (inclusive)
    end: int  # last editable segment index (exclusive)
    context: tuple[int, int]  # read-only tail of the previous batch [a, b)

    @property
    def tag_suffix(self) -> str:
        return f"b{self.n}"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4 + 1)


def batches(t: dict, *, batch_tokens: int, overlap_segments: int) -> list[Batch]:
    segments = t["segments"]
    out: list[Batch] = []
    start = 0
    n = 0
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
        out.append(Batch(n=n, start=start, end=end, context=context))
        start = end
        n += 1
    return out


def render(t: dict, w: Batch, *, scheme: SpeakerScheme) -> str:
    """The batch as `[index] text` lines, read-only context first. With fixed labels each line
    also shows its label (`[index] label: text`), because the model must see them."""

    def line(i: int, mark: str) -> str:
        seg = t["segments"][i]
        label = f"{seg['role']}: " if scheme.fixed_labels and seg.get("role") else ""
        return f"[{i}] {mark}{label}{seg['text']}"

    lines = [line(i, "(context) ") for i in range(*w.context)]
    lines += [line(i, "") for i in range(w.start, w.end)]
    return "\n".join(lines)


def role_summary(t: dict, *, upto: int, bindings: list[dict], scheme: SpeakerScheme) -> str:
    counts: dict[str, int] = {}
    for seg in t["segments"][:upto]:
        role = seg.get("role")
        if role:
            counts[role] = counts.get(role, 0) + 1
    names = {b["role"]: b["name"] for b in bindings if b.get("name")}
    if not counts and not names:
        return "(none yet)"
    parts = []
    for role in _summary_order(scheme, [*counts, *names]):
        bit = f"{role}: {counts.get(role, 0)} segments so far"
        if role in names:
            bit += f", named {names[role]}"
        parts.append(bit)
    return "; ".join(parts)


def _summary_order(scheme: SpeakerScheme, present: list[str]) -> list[str]:
    """Scheme labels first, in the scheme's order (the unresolved one included); for fixed
    labels, any other label after them in the order first seen."""
    seen = list(dict.fromkeys(present))
    ordered = [x for x in scheme.labels if x in seen]
    if scheme.fixed_labels:
        ordered += [x for x in seen if x not in scheme.labels]
    return ordered


def check_bounds(w: Batch, reply: dict, *, scheme: SpeakerScheme, t: dict | None = None) -> None:
    """Every index the reply names must be editable in this batch, and an ambiguity's
    probabilities line up with its candidates. Runs before the reply is cached: a bad reply
    is never served from cache. With fixed labels and the transcript `t`, a label that
    differs from the segment's own is rejected here too."""
    editable = range(w.start, w.end)
    visible = range(w.context[0], w.end) if w.context[1] else editable
    # Edits outside the editable range are dropped and counted at apply: a single stray index
    # on a four-hour video failed the whole video and re-billed thirty batches on retry.
    for idx_raw in reply.get("roles") or {}:
        # A label on a read-only context line is harmless and common (the model labels
        # everything it sees); it is ignored at apply, not an error.
        if int(idx_raw) not in visible:
            raise ValueError(
                f"roles: index {idx_raw} is outside batch {w.n} [{w.context[0]}, {w.end})"
            )
    if scheme.fixed_labels and t is not None:
        for idx_raw, label in (reply.get("roles") or {}).items():
            idx = int(idx_raw)
            if idx in editable and label[0] != t["segments"][idx].get("role"):
                raise ValueError(
                    f"roles: {label[0]!r} would relabel segment {idx} from "
                    f"{t['segments'][idx].get('role')!r}; the labels are fixed"
                )
    for item in reply.get("ambiguities") or []:
        if int(item["idx"]) not in editable:
            raise ValueError(
                f"ambiguities: index {item['idx']} is outside batch {w.n} [{w.start}, {w.end})"
            )
    # A binding is evidence (an anchor quote on the line where a name is said), so a context
    # line the model saw is good evidence: speakers often name themselves just before a
    # batch starts.
    for item in reply.get("bindings") or []:
        if int(item["idx"]) not in visible:
            raise ValueError(
                f"bindings: index {item['idx']} is outside batch {w.n} [{w.context[0]}, {w.end})"
            )
    for a in reply.get("ambiguities") or []:
        probs = a.get("probabilities")
        if probs is None:
            continue  # a prompt v1 reply: one confidence for the pick
        if len(probs) != len(a["candidates"]):
            raise ValueError(f"ambiguities: idx {a['idx']} needs one probability per candidate")
        if sum(probs) <= 0:
            raise ValueError(f"ambiguities: idx {a['idx']} probabilities sum to zero")
        if a["pick"] not in a["candidates"]:
            raise ValueError(f"ambiguities: idx {a['idx']} pick is not one of its candidates")


def apply(
    t: dict, w: Batch, reply: dict, *, next_key: int, scheme: SpeakerScheme
) -> tuple[dict, dict]:
    """Apply one batch's reply in place. `next_key` is the next ambiguity number for this
    source (keys are amb-NNNN, stable within the source). Returns (t, stats)."""
    segments = t["segments"]
    editable = range(w.start, w.end)

    def check(idx_raw, what: str, allowed: range = editable) -> int:
        try:
            idx = int(idx_raw)
        except (TypeError, ValueError) as e:
            raise BatchError(f"{what}: index {idx_raw!r} is not an integer") from e
        if idx not in allowed:
            raise BatchError(
                f"{what}: index {idx} is outside batch {w.n} [{allowed.start}, {allowed.stop})"
            )
        return idx

    stats = {
        "edits": 0,
        "protected_violations": 0,
        "roles": 0,
        "ambiguities": 0,
        "bindings": [],
        "dropped_ambiguities": 0,
        "dropped_bindings": 0,
    }
    for idx_raw, text in (reply.get("edits") or {}).items():
        if int(idx_raw) not in editable:
            stats["stray_edits"] = stats.get("stray_edits", 0) + 1
            continue
        idx = check(idx_raw, "edits")
        seg = segments[idx]
        new = str(text).strip()
        if not new:
            stats["protected_violations"] += 0
            continue
        if any(span not in new for span in seg.get("protected") or []):
            stats["protected_violations"] += 1
            continue
        if new != seg["text"]:
            seg.setdefault("raw", seg["text"])
            seg["text"] = new
            stats["edits"] += 1
    for idx_raw, label in (reply.get("roles") or {}).items():
        if int(idx_raw) in range(*w.context):
            stats["context_roles_ignored"] = stats.get("context_roles_ignored", 0) + 1
            continue
        idx = check(idx_raw, "roles")
        role, confidence = label[0], float(label[1])
        seg = segments[idx]
        if scheme.fixed_labels:
            if role != seg.get("role"):
                raise BatchError(
                    f"roles: {role!r} would relabel segment {idx} from {seg.get('role')!r}; "
                    "the labels are fixed"
                )
            continue  # a confirmation changes nothing
        if role not in scheme.labels:
            raise BatchError(f"roles: {role!r} is not a role label")
        seg["role"] = role
        seg["role_confidence"] = round(confidence, 3)
        if role != scheme.unresolved_label and not seg.get("speaker"):
            seg["speaker"] = role
        stats["roles"] += 1
    ambiguities = t.setdefault("ambiguities", [])
    for a in reply.get("ambiguities") or []:
        idx = check(a["idx"], "ambiguities")
        text = segments[idx]["text"]
        if (
            a["span"] not in text
            and a["pick"] not in text
            and not any(c in text for c in a["candidates"])
        ):
            # no anchor in the segment, counted
            stats["dropped_ambiguities"] += 1
            continue
        item = {
            "key": f"amb-{next_key:04d}",
            "idx": idx,
            "span": a["span"],
            "candidates": list(a["candidates"]),
            "pick": a["pick"],
            "confidence": round(float(a.get("confidence", 0)), 3),
            "status": "pending",
        }
        if a.get("probabilities") is not None:
            total = sum(a["probabilities"])
            item["probabilities"] = [round(p / total, 3) for p in a["probabilities"]]
            item["confidence"] = item["probabilities"][a["candidates"].index(a["pick"])]
        ambiguities.append(item)
        next_key += 1
        stats["ambiguities"] += 1
    known = {s.get("role") for s in segments}
    for b in reply.get("bindings") or []:
        idx = check(b["idx"], "bindings", range(w.context[0], w.end) if w.context[1] else editable)
        if scheme.fixed_labels and b["role"] not in known:
            stats["dropped_bindings"] += 1  # a name for a label no segment carries
        elif b["anchor"] and b["anchor"] in segments[idx]["text"]:
            stats["bindings"].append(
                {
                    "role": b["role"],
                    "name": b["name"],
                    "idx": idx,
                    "anchor": b["anchor"],
                    "confidence": round(float(b.get("confidence", 0.8)), 3),
                }
            )
        else:
            stats["dropped_bindings"] += 1
    stats["next_key"] = next_key
    return t, stats


def drop_unanchored(t: dict) -> int:
    """After the memory patch and the verdicts, a pending item whose span, pick and
    candidates no longer appear in its segment (an overlapping substitution rewrote it) has
    nothing to ask about: dropped and counted, never a failed transcript."""
    kept, dropped = [], 0
    for a in t.get("ambiguities") or []:
        text = t["segments"][a["idx"]]["text"]
        needles = [a["span"], a["pick"], *a["candidates"], a.get("resolution", "")]
        if any(n and n in text for n in needles):
            kept.append(a)
        else:
            dropped += 1
    t["ambiguities"] = kept
    return dropped


def cap_ambiguities(t: dict, *, max_new: int) -> int:
    """Beyond the cap the model's pick stands and the item is marked capped, lowest
    confidence first kept pending. Returns the capped count."""
    pending = [a for a in t.get("ambiguities") or [] if a["status"] == "pending"]
    if max_new <= 0 or len(pending) <= max_new:
        return 0
    pending.sort(key=lambda a: a["confidence"])
    capped = 0
    for a in pending[max_new:]:
        a["status"] = "capped"
        capped += 1
    return capped
