"""Review: verdicts, the correction memory and the notes round trip, for any source kind.

A review item is one question for the author about a source: `amb-NNNN` (a word the clean-up
model was unsure of) or `<prefix>-N` (a speaker label nobody could name; the prefix is the
`SpeakerScheme`'s). The author answers in a vault note (checkbox callouts and frontmatter
speaker properties) or on the phone page (corpuslm.review_page); both land in the same
verdict rows.

What differs per source kind is a `ReviewKind`, injected, never hard-wired: `anchor_url` (a link
to the moment in the source, or None when the source cannot be linked), `source_label` (the
text a card or note shows for a source), the `SpeakerScheme`, and the page footnote about the
links. YouTube's kind lives in ytlm; Bee's has no links: a `file://` link fails on a phone, so a
time label stands alone.

Export: `render_review` writes one callout per open item, each with a checkbox list (the
options, `other: ___`, `skip`). Apply: `parse_note` reads a note back. ONE checked box is the
verdict; none is untouched; more than one is reported by key and left alone; free text after
`other:` is the author's own, verbatim. `record_verdict` lands every verdict as a row with
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
- **review item**: one question for the author, with a stable key. The open ones form the
  review queue.
- **review kind**: how one source kind links, labels and names speakers on review surfaces.
- **verdict**: the author's answer to a review item (an option, `other`, `skip`, a name).
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
        for n in re.findall(r"([^;(]+) \(", entry["anchor"]):
            add(n)
    for other in (speaker_map.get("roles") or {}).values():
        add(other.get("name"))
    for n in kind_names:
        add(n)
    for n in picked:
        add(n)
    return names


def moment_label(kind: ReviewKind, source: dict, seconds: float) -> str:
    """A time label, linked to the moment when the kind can link to it (`[00:12](url)`),
    else the label alone."""
    link = kind.anchor_url(source, seconds)
    return f"[{timestamp(seconds)}]({link})" if link else timestamp(seconds)


def render_review(
    t: dict | None, speaker_map: dict | None, *, source: dict, kind: ReviewKind
) -> list[str]:
    """Markdown lines for the note's Review section (Live Preview shape)."""
    lines: list[str] = []
    pending: list[str] = []
    resolved: list[str] = []
    segments = (t or {}).get("segments", [])
    for a in (t or {}).get("ambiguities") or []:
        seg = segments[a["idx"]] if a["idx"] < len(segments) else {"t0": 0, "text": ""}
        if a["status"] == "pending":
            context = seg["text"].replace(a["pick"], f"**{a['pick']}**", 1)
            moment = moment_label(kind, source, seg["t0"])
            pending += [
                f'> [!question] {a["key"]} · {moment} · heard "{a["span"]}"',
                f"> {context}",
            ]
            for c in a["candidates"]:
                pending.append(f"> - [ ] {c}")
            pending += [f"> - [ ] {OTHER}", f"> - [ ] {SKIP}", ""]
        elif a["status"] in ("resolved", "auto_resolved"):
            origin = (
                f" (auto-resolved from {a.get('origin', '')})"
                if a["status"] == "auto_resolved"
                else ""
            )
            resolved.append(f'> - {a["key"]} → "{a.get("resolution", a["pick"])}"{origin}')
        elif a["status"] == "capped":
            resolved.append(
                f'> - {a["key"]} → "{a["pick"]}" (capped: the model\'s pick '
                "stands; raise [cleanup].max_new_ambiguities to review it)"
            )
        elif a["status"] == "expired":
            resolved.append(f'> - {a["key"]} → "{a["pick"]}" (expired, unreviewed)')
    for role, entry in ((speaker_map or {}).get("roles") or {}).items():
        if entry.get("status") == "NEEDS_AUTHOR":
            heard = f" Heard: {entry['anchor']}" if entry.get("anchor") else ""
            pending += [f"> [!question] {entry['key']} · who is {role}?{heard}"]
            for name in speaker_names(
                entry, speaker_map or {}, kind.extra_speaker_names(source), ()
            ):
                pending.append(f"> - [ ] {name}")
            pending += [f"> - [ ] {OTHER}", f"> - [ ] {SKIP}", ""]
        elif entry.get("status") == "AUTHOR":
            resolved.append(f'> - {role} → "{entry["name"]}" (author)')
    unplaced = ((t or {}).get("passes", {}).get("pass3", {}) or {}).get("unplaced") or []
    if unplaced:
        # Author work is never dropped silently. A verdict whose span the
        # rebuilt transcript no longer carries is shown here, with what it said.
        pending += ["> [!warning] Unplaced verdicts (the transcript changed under them)"]
        pending += [
            f'> - {u["key"]} heard "{u["span"]}" → "{u["verdict"]}"; re-answer '
            "the new item if it reappears"
            for u in unplaced
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
    """Verdicts the author left in a note: `items` key → verdict (or None
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

    for i, line in enumerate(text.splitlines()):
        if i == 0 and line.strip() == "---":
            in_front = True
            continue
        if in_front:
            if line.strip() == "---":
                in_front = False
                continue
            m = front_speaker.match(line)
            if m:
                speakers[m.group("role")] = m.group("name").replace('\\"', '"')
            continue
        m = item_line.match(line)
        if m:
            close()
            key = m.group("key")
            continue
        if key is not None:
            b = _BOX.match(line)
            if b:
                seen_any = True
                if b.group("mark") in "xX":
                    checked.append(b.group("label").strip())
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
    source: str = "review file",
    idx: int | None = None,
) -> None:
    """Append the author's verdict (decisions are append-only: an answer
    given again, or changed, is a new row and the newest is in force)."""
    store = Store(db)
    subject = subject_sha256(kind=kind, source_id=source_id, span=span, role=role)
    current = store.current_review_verdicts(source_id).get(key)
    if not (
        current
        and (current["verdict"], current["subject_sha256"], current["source"])
        == (verdict, subject, source)
    ):
        # The same answer landing again (a re-run apply) adds no row.
        store.append_review_verdict(
            source_id=source_id,
            key=key,
            kind=kind,
            verdict=verdict,
            span=span,
            role=role,
            source=source,
            subject_sha256=subject,
            metadata=None if idx is None else {"idx": int(idx)},
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
    """`term` when the reading carries a name or a word the English dictionary
    does not know (a Sanskrit term, a proper noun); `reading` for ordinary
    words. Terms feed the deterministic clean-up as substitutions
    (corpuslm.cleanup); readings are applied only where the model clean-up flags the
    same span again."""
    from .cleanup import is_dictionary_word

    words = [w.strip(".,;:!?\"'()") for w in verdict.split()]
    words = [w for w in words if w]
    if not words:
        return "reading"
    if any(w[:1].isupper() for w in words):
        return "term"
    if any(not is_dictionary_word(w) for w in words if w.isalpha()):
        return "term"
    return "reading"


def rekind_corrections(db) -> int:
    """Re-classify every rule in force with the current rule; a changed kind
    is written forward as a new row. Returns the number changed."""
    store = Store(db)
    changed = 0
    for r in store.current_corrections().values():
        kind = verdict_kind(r["verdict"])
        if kind != r["kind"]:
            store.append_correction(
                span_norm=r["span_norm"],
                verdict=r["verdict"],
                kind=kind,
                origin_source=r["origin_source"],
                origin_key=r["origin_key"],
                created_by="rekind",
            )
            changed += 1
    return changed


def corrections(db) -> dict[str, dict]:
    return Store(db).current_corrections()


def verdicts_for(db, source_id: str) -> dict[str, dict]:
    return Store(db).current_review_verdicts(source_id)


def term_vocabulary(db) -> dict[str, str]:
    """Correction memory verdicts of kind `term`: mis-hearing → protected form,
    for the deterministic clean-up (corpuslm.cleanup)."""
    return {norm: r["verdict"] for norm, r in Store(db).current_corrections(kind="term").items()}


# ---- reconcile after a rebuild ----------------------------------------------------


def reading_options(ambiguity: dict) -> list[str]:
    """The card's choices: the clean-up model's pick, the candidates and the
    words as heard. Readings that differ only in case or punctuation are one
    option, spelled as the first of them (the pick's spelling when it is
    one): a pick with a comma once matched no option at all (2026-09-29)."""
    out: dict[str, str] = {}
    for option in (ambiguity["pick"], *ambiguity["candidates"], ambiguity["span"]):
        out.setdefault(normalize_span(option) or option, option)
    return list(out.values())


def apply_resolution(t: dict, a: dict, resolution: str) -> None:
    if resolution == SKIP:
        return
    seg = t["segments"][a["idx"]]
    for needle in (a["pick"], a["span"], *a["candidates"]):
        if needle and needle in seg["text"]:
            seg.setdefault("raw", seg["text"])
            seg["text"] = seg["text"].replace(needle, resolution, 1)
            break


def reconcile(
    t: dict,
    verdicts: dict[str, dict],
    memory: dict[str, dict],
    *,
    now: datetime | None = None,
) -> dict:
    """Apply the author's own verdicts (by key, span must still match) and
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
    corpus_text = "\n".join(seg["text"] for seg in t["segments"])
    by_norm: dict[str, dict] = {}
    for a in t.get("ambiguities") or []:
        by_norm.setdefault(normalize_span(a["span"]), a)
    placed: set[str] = set()
    for key, v in verdicts.items():
        if v["kind"] != "ambiguity":
            continue
        target = None
        cand = next((a for a in t.get("ambiguities") or [] if a["key"] == key), None)
        if cand and normalize_span(cand["span"]) == normalize_span(v["span"] or cand["span"]):
            target = cand
        elif v["span"] and normalize_span(v["span"]) in by_norm:
            target = by_norm[normalize_span(v["span"])]
        if target is None or target["key"] in placed:
            # The deterministic clean-up's correction memory may already have written this reading
            # into the transcript, so the model never asked again: applied,
            # not lost (author work is never dropped silently). Checked
            # near the segment the verdict was given on when that is known.
            idx = (loads(v.get("metadata"), {}) or {}).get("idx")
            if isinstance(idx, int):
                near = " ".join(seg["text"] for seg in t["segments"][max(0, idx - 2) : idx + 3])
            else:
                near = corpus_text
            if v["verdict"] != SKIP and v["verdict"] in near:
                counts["already_applied"] += 1
                continue
            counts["unplaced"] += 1
            counts["unplaced_items"].append(
                {"key": key, "span": v.get("span", ""), "verdict": v["verdict"]}
            )
            continue
        placed.add(target["key"])
        target["status"] = "resolved"
        target["resolution"] = v["verdict"]
        target["resolved_at"] = v.get("applied_at") or now_iso(now)
        apply_resolution(t, target, v["verdict"])
        counts["carried"] += 1
    today = now.astimezone(UTC).strftime("%Y-%m-%d")
    for a in t.get("ambiguities") or []:
        if a["status"] != "pending" or a["key"] in placed:
            continue
        hit = memory.get(normalize_span(a["span"]))
        if not hit:
            continue
        a["status"] = "auto_resolved"
        a["resolution"] = hit["verdict"]
        a["origin"] = f"{hit['origin_source']} {hit.get('created_at', today)[:10]}"
        a["resolved_at"] = now_iso(now)
        apply_resolution(t, a, hit["verdict"])
        counts["auto_resolved"] += 1
    return counts
