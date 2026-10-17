"""Review: verdicts, the correction memory and the notes round trip, for any source kind.

A review item is one question for the owner about a source: `amb-NNNN` (a word the cleanup
model was unsure of) or `<prefix>-N` (a speaker label nobody could name; the prefix is the
`SpeakerScheme`'s). The owner answers in a vault note (checkbox callouts and frontmatter
speaker properties) or on the phone page (corpuslm.review_page); both land in the same
verdict rows.

What differs per source kind is a `ReviewKind`, injected, never hard-wired: `anchor_url` (a link
to the moment in the source, or None when the source cannot be linked), `source_label` (the
text a card or note shows for a source), the `SpeakerScheme`, and the page footnote about the
links. Both kinds live in corpuslm.review_kinds; Bee's has no links: a `file://` link fails on a
phone, so a time label stands alone.

Export: `render_review` writes one callout per open item, each with a checkbox list (the
candidates, `other: ___`, `skip`). Apply: `parse_note` reads a note back. ONE checked box is the
verdict; none is untouched; more than one is reported by key and left alone; free text after
`other:` is the owner's own, verbatim. `record_verdict` lands every verdict as a row with
provenance, and every ambiguity verdict that is not `skip` becomes a rule in the
correction memory keyed by the normalized span, so the same question is not asked twice.
`reconcile` re-applies recorded verdicts to a rebuilt transcript by key and span; a verdict
whose span is gone is counted as unplaced and kept.

The stored keys of a transcript keep their legacy names (`passes.pass3`, `role`, `raw`,
`amb-NNNN`): renaming them would change stored bytes.

Never imports: cli, clikit, any model transport.

Decisions:
- Hooks are data, not code paths: these functions read a `ReviewKind` and never name a source
  kind. Enforced by: tests/test_review.py::test_a_kind_without_links_renders_time_labels_only.
- One box is the verdict, none is untouched, more than one is reported by key and left alone,
  and text after `other:` is taken verbatim. Enforced by:
  tests/test_review.py::test_two_boxes_checked_are_reported_by_key_and_left_alone.
- A source with no link shows a time label only. Enforced by:
  tests/test_review.py::test_a_kind_without_links_renders_time_labels_only.
- An answer given again adds no row; a changed answer is a new row and the newest is in force.
  Enforced by: tests/test_review.py::test_a_verdict_given_again_adds_no_row.
- Every ambiguity verdict that is not `skip` becomes a correction-memory rule keyed by the
  normalized span. Enforced by:
  tests/test_review.py::test_an_ambiguity_verdict_becomes_a_correction_memory_rule.
- A verdict whose span the rebuilt transcript no longer carries is unplaced and kept, never
  dropped. Enforced by: tests/test_review.py::test_a_verdict_with_a_vanished_span_is_unplaced.

Terms:
- **correction memory**: the append-only rules, newest per normalized span in force, that
  settle an ambiguity without asking again.
- **review item**: one question for the owner, with a stable key. The open ones form the
  review queue.
- **verdict**: the owner's answer to a review item (an option, `other`, `skip`, a name).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import clock
from .db import loads, now_iso
from .identity import normalize_span, subject_sha256
from .speaker_map import SpeakerScheme
from .store import Store
from .transcript import timestamp

OTHER = "other: ___"
SKIP = "skip"

DEFAULT_ROLE_PATTERN = r"[A-Z_0-9]+"


@dataclass(frozen=True, slots=True)
class ReviewKind:
    """What differs per source kind on a review surface (see the module docstring)."""

    name: str  # the kind as the sources table stores it: 'youtube', 'bee'
    anchor_url: Callable[[dict, float], str | None]  # (source, seconds) -> link or None
    source_label: Callable[[dict], str]  # (source) -> the text a card or note shows
    scheme: SpeakerScheme
    links_note: str = ""  # a page footnote about the links; empty when there are none
    extra_speaker_names: Callable[[dict], list[str]] = lambda source: []  # picker additions
    # How a speaker label is spelled in a note's flat frontmatter key (`speaker_<label>`).
    role_pattern: str = DEFAULT_ROLE_PATTERN
    # The tool's refinement of the transcript schema, when its documents are not valid under the
    # shared one (Bee's speaker labels are its own); None reads them with the shared schema.
    refined_schema: dict | None = None
    # The artifacts of a source that go stale when the owner answers one of its cards, so the
    # tool's next run rebuilds them; empty when the tool rebuilds from the answer itself.
    stale_nodes: tuple[str, ...] = ()
    # The artifact node holding the AI cleanup's document (`transcript.json`'s ambiguities
    # before any later step), where its suggestions are read from.
    ai_cleanup_node: str = "model_cleanup"
    # The verb of the tool that carries this kind's landed answers into its files (a speaker
    # name rebuilds a map the tool owns). Empty when marking `stale_nodes` is all it takes.
    # `corpuslm review apply` lands the kind's answers only where the tool registered a rebuilder
    # (`corpuslm.review_rebuilders`); otherwise it leaves them unlanded and names this verb.
    rebuilt_by: str = ""


def item_pattern(review_key_prefix: str = "spk") -> re.Pattern[str]:
    prefix = re.escape(review_key_prefix)
    return re.compile(rf"^> \[!question\] (?P<key>amb-\d{{4,}}|{prefix}-\d+)\b")


_ITEM = item_pattern()
_BOX = re.compile(r"^> - \[(?P<mark>[ xX])\] (?P<label>.*)$")

# ---- export ----------------------------------------------------------------


def speaker_names(
    entry: dict, speaker_map: dict, kind_names: list[str], picked: Iterable[str]
) -> list[str]:
    """The names the picker offers for one speaker label, only from sources of real names:
    the name the map already holds for this label, the names the model proposed (the heard
    anchor), the names the other labels of this source already carry (a model's binding, the
    owner's pick, a scheme default), the kind's own (the owner's first name) and the names the
    owner already picked. Never the map's `candidates` (the source's protected vocabulary: a
    title's or a summary's capitalised words are not names)."""
    names: list[str] = []

    def add(name: object) -> None:
        text = str(name or "").strip()
        if text and text not in names:
            names.append(text)

    add(entry.get("name"))
    if entry.get("status") == "NEEDS_AUTHOR" and entry.get("anchor"):
        for candidate_name in re.findall(r"([^;(]+) \(", entry["anchor"]):
            add(candidate_name)
    for other in (speaker_map.get("roles") or {}).values():
        add(other.get("name"))
    for candidate_name in kind_names:
        add(candidate_name)
    for candidate_name in picked:
        add(candidate_name)
    return names


def moment_label(kind: ReviewKind, source_row: dict, seconds: float) -> str:
    """A time label, linked to the moment when the kind can link to it (`[00:12](url)`),
    else the label alone."""
    link = kind.anchor_url(source_row, seconds)
    return f"[{timestamp(seconds)}]({link})" if link else timestamp(seconds)


def render_review(
    document: dict | None, speaker_map: dict | None, *, source_row: dict, kind: ReviewKind
) -> list[str]:
    """Markdown lines for the note's Review section (Live Preview shape)."""
    lines: list[str] = []
    pending: list[str] = []
    resolved: list[str] = []
    segments = (document or {}).get("segments", [])
    for ambiguity in (document or {}).get("ambiguities") or []:
        segment = (
            segments[ambiguity["idx"]]
            if ambiguity["idx"] < len(segments)
            else {"t0": 0, "text": ""}
        )
        if ambiguity["status"] == "pending":
            context = segment["text"].replace(ambiguity["pick"], f"**{ambiguity['pick']}**", 1)
            moment = moment_label(kind, source_row, segment["t0"])
            pending += [
                f'> [!question] {ambiguity["key"]} · {moment} · heard "{ambiguity["span"]}"',
                f"> {context}",
            ]
            for candidate in ambiguity["candidates"]:
                pending.append(f"> - [ ] {candidate}")
            pending += [f"> - [ ] {OTHER}", f"> - [ ] {SKIP}", ""]
        elif ambiguity["status"] in ("resolved", "auto_resolved"):
            origin = (
                f" (auto-resolved from {ambiguity.get('origin', '')})"
                if ambiguity["status"] == "auto_resolved"
                else ""
            )
            resolved.append(
                f"> - {ambiguity['key']} → "
                f'"{ambiguity.get("resolution", ambiguity["pick"])}"{origin}'
            )
        elif ambiguity["status"] == "capped":
            resolved.append(
                f'> - {ambiguity["key"]} → "{ambiguity["pick"]}" (capped: the model\'s pick '
                "stands; raise [cleanup].max_new_ambiguities to review it)"
            )
        elif ambiguity["status"] == "expired":
            resolved.append(f'> - {ambiguity["key"]} → "{ambiguity["pick"]}" (expired, unreviewed)')
    for role, entry in ((speaker_map or {}).get("roles") or {}).items():
        if entry.get("status") == "NEEDS_AUTHOR":
            heard = f" Heard: {entry['anchor']}" if entry.get("anchor") else ""
            pending += [f"> [!question] {entry['key']} · who is {role}?{heard}"]
            for name in speaker_names(
                entry, speaker_map or {}, kind.extra_speaker_names(source_row), ()
            ):
                pending.append(f"> - [ ] {name}")
            pending += [f"> - [ ] {OTHER}", f"> - [ ] {SKIP}", ""]
        elif entry.get("status") == "AUTHOR":
            resolved.append(f'> - {role} → "{entry["name"]}" (owner)')
    unplaced = ((document or {}).get("passes", {}).get("pass3", {}) or {}).get("unplaced") or []
    if unplaced:
        # Owner work is never dropped silently. A verdict whose span the
        # rebuilt transcript no longer carries is shown here, with what it said.
        pending += ["> [!warning] Unplaced verdicts (the transcript changed under them)"]
        pending += [
            f'> - {unplaced_verdict["key"]} heard "{unplaced_verdict["span"]}" '
            f'→ "{unplaced_verdict["verdict"]}"; re-answer '
            "the new item if it reappears"
            for unplaced_verdict in unplaced
        ]
        pending.append("")
    if not pending and not resolved:
        return ["_No review items._", ""]
    lines += pending or ["_No open items._", ""]
    if resolved:
        lines += ["> [!done]- Resolved"] + resolved + [""]
    return lines


# ---- scan back -------------------------------------------------------------


def parse_note(
    text: str,
    *,
    review_key_prefix: str = "spk",
    role_pattern: str = DEFAULT_ROLE_PATTERN,
) -> dict:
    """Verdicts the owner left in a note: `items` key → verdict (or None
    untouched), `ambiguous` keys with more than one box, `speakers` role →
    name from frontmatter. The prefix and the label pattern are the source kind's
    (`ReviewKind.scheme.review_key_prefix`, `ReviewKind.role_pattern`)."""
    item_line = item_pattern(review_key_prefix)
    # Flat frontmatter keys, one per label, so Obsidian's Properties panel edits
    # them (it does not edit nested objects).
    front_speaker = re.compile(rf'^speaker_(?P<role>{role_pattern}): "(?P<name>.*)"$')
    items: dict[str, str | None] = {}
    ambiguous: list[str] = []
    speakers: dict[str, str] = {}
    in_front = False
    key = None
    checked: list[str] = []
    seen_any = False

    def close() -> None:
        nonlocal key, checked, seen_any
        if key is None:
            return
        if len(checked) > 1:
            ambiguous.append(key)
            items[key] = None
        elif len(checked) == 1:
            label = checked[0]
            if label.startswith("other:"):
                custom = label[len("other:") :].strip()
                items[key] = custom if custom and custom != "___" else None
            else:
                items[key] = label
        else:
            items[key] = None
        key, checked, seen_any = None, [], False

    for line_number, line in enumerate(text.splitlines()):
        if line_number == 0 and line.strip() == "---":
            in_front = True
            continue
        if in_front:
            if line.strip() == "---":
                in_front = False
                continue
            item_match = front_speaker.match(line)
            if item_match:
                speakers[item_match.group("role")] = item_match.group("name").replace('\\"', '"')
            continue
        item_match = item_line.match(line)
        if item_match:
            close()
            key = item_match.group("key")
            continue
        if key is not None:
            box_match = _BOX.match(line)
            if box_match:
                seen_any = True
                if box_match.group("mark") in "xX":
                    checked.append(box_match.group("label").strip())
                continue
            if not line.startswith(">"):
                close()
    close()
    return {"items": items, "ambiguous": ambiguous, "speakers": speakers}


# ---- verdict store and correction memory -----------------------------------


def record_verdict(
    db,
    *,
    source_id: str,
    key: str,
    kind: str,
    verdict: str,
    span: str = "",
    role: str = "",
    created_by: str = "cli",
    answered_through: str = "review file",
    index: int | None = None,
) -> None:
    """Append the owner's verdict (decisions are append-only: an answer
    given again, or changed, is a new row and the newest is in force)."""
    store = Store(db)
    subject = subject_sha256(kind=kind, source_id=source_id, span=span, role=role)
    current = store.current_review_verdicts(source_id).get(key)
    if not (
        current
        and (current["verdict"], current["subject_sha256"], current["source"])
        == (verdict, subject, answered_through)
    ):
        # The same answer landing again (a re-run apply) adds no row.
        store.append_review_verdict(
            source_id=source_id,
            key=key,
            kind=kind,
            verdict=verdict,
            span=span,
            role=role,
            answered_through=answered_through,
            subject_sha256=subject,
            metadata=None if index is None else {"idx": int(index)},
            created_by=created_by,
        )
    if kind == "speaker" and verdict != SKIP:
        if store.current_speaker_names(source_id).get(role) != verdict:
            store.append_speaker_verdict(
                source_id=source_id, role=role, name=verdict, created_by=created_by
            )
    if kind == "ambiguity" and verdict != SKIP and span:
        remember(
            db,
            span=span,
            verdict=verdict,
            origin_source=source_id,
            origin_key=key,
            created_by=created_by,
        )


def remember(
    db, *, span: str, verdict: str, origin_source: str, origin_key: str, created_by: str = "cli"
) -> None:
    """A rule in the correction memory, appended; the newest per span is in force."""
    norm = normalize_span(span)
    if not norm:
        return
    store = Store(db)
    kind = verdict_kind(verdict)
    current = store.current_corrections().get(norm)
    if current and (current["verdict"], current["kind"]) == (verdict, kind):
        return  # the rule in force already says this
    store.append_correction(
        span_norm=norm,
        verdict=verdict,
        kind=kind,
        origin_source=origin_source,
        origin_key=origin_key,
        created_by=created_by,
    )


def verdict_kind(verdict: str) -> str:
    """`term` when the corrected words carry a name or a word the English dictionary
    does not know (a Sanskrit term, a proper noun); `reading` (the stored name of the
    ordinary-correction kind) for ordinary words. Both kinds are applied only where the AI
    cleanup flags the same span again (`reconcile`); neither is substituted blindly
    (corpuslm.basic_cleanup decisions)."""
    from .basic_cleanup import is_dictionary_word

    words = [word.strip(".,;:!?\"'()") for word in verdict.split()]
    words = [word for word in words if word]
    if not words:
        return "reading"
    if any(word[:1].isupper() for word in words):
        return "term"
    if any(not is_dictionary_word(word) for word in words if word.isalpha()):
        return "term"
    return "reading"


def rekind_corrections(db) -> int:
    """Re-classify every rule in force with the current rule; a changed kind
    is written forward as a new row. Returns the number changed."""
    store = Store(db)
    changed = 0
    for correction in store.current_corrections().values():
        kind = verdict_kind(correction["verdict"])
        if kind != correction["kind"]:
            store.append_correction(
                span_norm=correction["span_norm"],
                verdict=correction["verdict"],
                kind=kind,
                origin_source=correction["origin_source"],
                origin_key=correction["origin_key"],
                created_by="rekind",
            )
            changed += 1
    return changed


def corrections(db) -> dict[str, dict]:
    return Store(db).current_corrections()


def verdicts_for(db, source_id: str) -> dict[str, dict]:
    return Store(db).current_review_verdicts(source_id)


# ---- reconcile after a rebuild ----------------------------------------------------


def candidates_offered(ambiguity: dict) -> list[str]:
    """The candidates a card offers: the AI cleanup's pick, its candidates and the
    words as heard. Candidates that differ only in case or punctuation are one
    candidate, spelled as the first of them (the pick's spelling when it is
    one): a pick with a comma once matched no candidate at all (2026-09-29)."""
    out: dict[str, str] = {}
    for candidate in (ambiguity["pick"], *ambiguity["candidates"], ambiguity["span"]):
        out.setdefault(normalize_span(candidate) or candidate, candidate)
    return list(out.values())


def apply_resolution(document: dict, ambiguity: dict, resolution: str) -> None:
    if resolution == SKIP:
        return
    segment = document["segments"][ambiguity["idx"]]
    for needle in (ambiguity["pick"], ambiguity["span"], *ambiguity["candidates"]):
        if needle and needle in segment["text"]:
            segment.setdefault("raw", segment["text"])
            segment["text"] = segment["text"].replace(needle, resolution, 1)
            break


def reconcile(
    document: dict,
    verdicts: dict[str, dict],
    memory: dict[str, dict],
    *,
    now: datetime | None = None,
) -> dict:
    """Apply the owner's own verdicts (by key, span must still match) and
    the correction memory to a freshly built model-cleaned transcript, stamped
    `now` (default: the clock). Returns counts: carried, unplaced,
    auto_resolved."""
    now = now or clock.utc_now()
    counts: dict = {
        "carried": 0,
        "unplaced": 0,
        "auto_resolved": 0,
        "unplaced_items": [],
        "already_applied": 0,
    }
    corpus_text = "\n".join(segment["text"] for segment in document["segments"])
    by_norm: dict[str, dict] = {}
    for ambiguity in document.get("ambiguities") or []:
        by_norm.setdefault(normalize_span(ambiguity["span"]), ambiguity)
    placed: set[str] = set()
    for key, verdict in verdicts.items():
        if verdict["kind"] != "ambiguity":
            continue
        target = None
        candidate = next(
            (
                ambiguity
                for ambiguity in document.get("ambiguities") or []
                if ambiguity["key"] == key
            ),
            None,
        )
        if candidate and normalize_span(candidate["span"]) == normalize_span(
            verdict["span"] or candidate["span"]
        ):
            target = candidate
        elif verdict["span"] and normalize_span(verdict["span"]) in by_norm:
            target = by_norm[normalize_span(verdict["span"])]
        if target is None or target["key"] in placed:
            # The basic cleanup's correction memory may already have written this correction
            # into the transcript, so the model never asked again: applied,
            # not lost (owner work is never dropped silently). Checked
            # near the segment the verdict was given on when that is known.
            index = (loads(verdict.get("metadata"), {}) or {}).get("idx")
            if isinstance(index, int):
                near = " ".join(
                    segment["text"]
                    for segment in document["segments"][max(0, index - 2) : index + 3]
                )
            else:
                near = corpus_text
            if verdict["verdict"] != SKIP and verdict["verdict"] in near:
                counts["already_applied"] += 1
                continue
            counts["unplaced"] += 1
            counts["unplaced_items"].append(
                {"key": key, "span": verdict.get("span", ""), "verdict": verdict["verdict"]}
            )
            continue
        placed.add(target["key"])
        target["status"] = "resolved"
        target["resolution"] = verdict["verdict"]
        target["resolved_at"] = verdict.get("applied_at") or now_iso(now)
        apply_resolution(document, target, verdict["verdict"])
        counts["carried"] += 1
    today = now.astimezone(UTC).strftime("%Y-%m-%d")
    for ambiguity in document.get("ambiguities") or []:
        if ambiguity["status"] != "pending" or ambiguity["key"] in placed:
            continue
        hit = memory.get(normalize_span(ambiguity["span"]))
        if not hit:
            continue
        ambiguity["status"] = "auto_resolved"
        ambiguity["resolution"] = hit["verdict"]
        ambiguity["origin"] = f"{hit['origin_source']} {hit.get('created_at', today)[:10]}"
        ambiguity["resolved_at"] = now_iso(now)
        apply_resolution(document, ambiguity, hit["verdict"])
        counts["auto_resolved"] += 1
    return counts
