"""Line speakers: who spoke each line a recorder left unidentified, one line at a time.

Bee recognizes only the owner's voice; every other line is labeled `Unknown`, and Unknown is a
mix of the owner himself (his voice not matched), the people around him, media and noise. The
speaker map names a label once per source, so naming `Unknown` credited every such line, the
owner's own included, to one person (nagarkar/corpuslm#66). This pass asks a model about each
unidentified line instead, reading the source's summary (the recorder's own, kept as the
source's `description`), the household (`[line_speakers].household`: names, roles, the
misspellings the recognizer uses) and the lines around it, in windows of `window_lines` lines
with `context_lines` lines before each window shown as context only.

A pick is a household name, or `media`, `other`, `noise` or `unclear`, with a confidence. The
picks of a source are rows of `line_speakers`, rebuilt whole by the pass; the owner's picks are
rows of `line_speaker_verdicts`, append-only, and always win.
`speaker_map.apply_line_speakers` puts them on a transcript document after the speaker map: the
owner's pick, else a model pick at or above `speaker_map.MIN_LINE_CONFIDENCE`, else the line
keeps its recorder label, so an unsure line is never named and the owner's per-label answer no
longer reaches a line the pass has seen.

The pass records an `artifacts` row (node `line_speakers`) whose inputs are the hashes of the
lines, the summary, the household, the prompt and the model plus the window sizes, so `status`
says what changed and nothing is asked twice; replies are cached by payload. `estimate` reads
only the reply cache.

Never imports: cli, clikit. Reads the database only through `Store`.

Decisions:
- A model may name a line only with a household name or `media`, `other`, `noise`; any other
  name it returns is read as `unclear`. Enforced by:
  tests/test_line_speakers.py::test_a_name_outside_the_household_is_read_as_unclear.
- The owner's pick for a line always wins over the model's, and a model rebuild never touches
  it. Enforced by:
  tests/test_line_speakers.py::test_the_owner_pick_wins_and_survives_a_rebuild.
- A pick below `speaker_map.MIN_LINE_CONFIDENCE` names nothing: the line keeps its recorder
  label and goes to the owner. Why: on the owner's marked lines (2026-10-14) picks at 0.6 or
  above were right about nine times in ten and those below it about seven in ten. Enforced by:
  tests/test_line_speakers.py::test_an_unsure_pick_keeps_the_recorder_label.
- The model is Claude Opus (owner, 2026-10-14): on 60 lines the owner marked it was right on
  52, Sonnet 50, the best Gemini 31, Jev 26; a cheap-first cascade cost more than Opus alone,
  since an escalated window must be read again whole. (owner)

Terms:
- **line speaker**: who spoke one line, as the model picked it (a row of `line_speakers`) or
  the owner did (a row of `line_speaker_verdicts`).
- **household**: the people who usually speak in a kind's sources, from the settings: name,
  role, the names the recognizer writes for them, a note.
- **unidentified line**: a line whose recorder label is `[line_speakers].unidentified_label`
  (Bee: `Unknown`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import llm, prompts
from .identity import canonical_sha256
from .speaker_map import NON_PERSONS, UNCLEAR, confident_line_pick
from .store import Store

STAGE = "line_speakers"
NODE = "line_speakers"
WINDOW_LINES = 150
CONTEXT_LINES = 20
OUTPUT_TOKENS_PER_LINE = 60  # what `estimate` assumes one pick costs (Opus measured about 46)
SUMMARY_SECTIONS = ("Summary", "Atmosphere")


def prompt() -> prompts.Prompt:
    return prompts.load("line_speakers")


def settings(config: dict) -> dict:
    """`[line_speakers]` of the settings: the model, the owner's name, the label meaning
    unidentified, the household and the window sizes."""
    section = config.get("line_speakers") or {}
    if not section.get("model"):
        raise llm.NotConfigured("[line_speakers].model is missing from the settings file")
    if not section.get("owner"):
        raise llm.NotConfigured("[line_speakers].owner is missing from the settings file")
    household = [
        {
            "name": str(person["name"]),
            "role": str(person.get("role", "")),
            "aliases": [str(alias) for alias in person.get("aliases", [])],
            "note": str(person.get("note", "")),
        }
        for person in section.get("household", [])
    ]
    return {
        "model": str(section["model"]),
        "owner": str(section["owner"]),
        "unidentified_label": str(section.get("unidentified_label", "Unknown")),
        "household": household,
        "window_lines": int(section.get("window_lines", WINDOW_LINES)),
        "context_lines": int(section.get("context_lines", CONTEXT_LINES)),
    }


def household_text(found: dict) -> str:
    """One line per person, the owner first."""
    out = []
    for person in found["household"]:
        parts = [person["name"]]
        if person["role"]:
            parts.append(person["role"])
        if person["aliases"]:
            parts.append("may appear as " + " or ".join(person["aliases"]))
        if person["note"]:
            parts.append(person["note"])
        out.append("- " + "; ".join(parts) + ".")
    return "\n".join(out)


def allowed_names(found: dict) -> set[str]:
    return {found["owner"], *(person["name"] for person in found["household"])}


def summary_of(source_row: dict) -> str:
    """The recorder's summary sections that say who was present and what played (Summary,
    Atmosphere); the whole description when it has neither."""
    description = source_row.get("description") or ""
    parts = re.split(r"^#+ ", description, flags=re.M)
    kept = [
        "## " + part.strip() for part in parts if part.split("\n", 1)[0].strip() in SUMMARY_SECTIONS
    ]
    return "\n\n".join(kept) or description.strip() or "(none)"


def lines_of(store: Store, source_id: str) -> list[dict]:
    """Each line with its recorder label (the raw segment's speaker) and its best text."""
    best = {segment["idx"]: segment["text"] for segment in store.best_segments(source_id)}
    return [
        {
            "idx": segment["idx"],
            "t0": segment["t0"],
            "t1": segment["t1"],
            "label": segment["speaker"],
            "text": best.get(segment["idx"], segment["text"]),
        }
        for segment in store.segments(source_id)
    ]


def windows(lines: list[dict], size: int, context: int) -> list[tuple[list[dict], list[dict]]]:
    """(context, window) pairs: every line once in a window, the `context` lines before it
    shown for reading only."""
    return [
        (lines[max(0, start - context) : start], lines[start : start + size])
        for start in range(0, len(lines), size)
    ]


def _line(line: dict) -> str:
    return (
        f"[{line['idx']}] {int(line['t0'] or 0)}-{int(line['t1'] or 0)} | "
        f"{line['label']}: {line['text']}"
    )


def _variables(found: dict, summary: str, context: list[dict], window: list[dict]) -> dict:
    return {
        "owner": found["owner"],
        "household": household_text(found),
        "summary": summary,
        "transcript": "\n".join(_line(line) for line in [*context, *window]),
        "first": str(window[0]["idx"]),
        "last": str(window[-1]["idx"]),
    }


def inputs(store: Store, source_id: str, config: dict) -> dict:
    found = settings(config)
    row = store.source(source_id) or {}
    return {
        "lines": canonical_sha256(
            [[line["idx"], line["label"], line["text"]] for line in lines_of(store, source_id)]
        ),
        "summary": canonical_sha256(summary_of(row)),
        "household": canonical_sha256([found["owner"], found["household"]]),
        "prompt": prompt().sha256,
        "model": found["model"],
        "window_lines": found["window_lines"],
        "context_lines": found["context_lines"],
    }


def _unidentified(lines: list[dict], label: str) -> list[dict]:
    return [line for line in lines if line["label"] == label]


# ---- what stands ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceStatus:
    source_id: str
    unidentified: int  # lines with the unidentified label
    named: int  # of those, named by the owner or a confident model pick
    unsure: int  # unsure model picks (below the threshold, or unclear) the owner has not picked
    owner: int  # the owner's picks in force
    state: str  # current | stale | none
    reason: str


def status(store: Store, config: dict, source_ids: list[str]) -> list[SourceStatus]:
    found = settings(config)
    out = []
    for source_id in source_ids:
        lines = lines_of(store, source_id)
        unidentified = {line["idx"] for line in _unidentified(lines, found["unidentified_label"])}
        picks = store.line_speakers(source_id)
        verdicts = store.current_line_speaker_verdicts(source_id)
        named = sum(
            1
            for index in unidentified
            if index in verdicts or confident_line_pick(picks.get(index))
        )
        unsure = sum(
            1
            for index in unidentified
            if index not in verdicts and index in picks and not confident_line_pick(picks[index])
        )
        artifact = store.artifact(source_id, NODE)
        if artifact is None:
            state, reason = "none", ""
        else:
            recorded = json.loads(artifact["inputs"])
            current = inputs(store, source_id, config)
            changed = sorted(key for key in current if recorded.get(key) != current[key])
            if artifact["state"] == "built" and not changed:
                state, reason = "current", ""
            else:
                state = "stale"
                reason = ", ".join(f"{key} changed" for key in changed) or (
                    f"state {artifact['state']}"
                )
        out.append(
            SourceStatus(source_id, len(unidentified), named, unsure, len(verdicts), state, reason)
        )
    return out


# ---- what a run would send ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Estimate:
    sources: int  # sources that would be asked (not current, with unidentified lines)
    from_cache: int  # of those, the ones every window of which the cache holds
    calls: int
    lines: int  # unidentified lines in the windows that would be billed
    tokens_in: int
    tokens_out: int
    usd: float | None
    model: str


def estimate(store: Store, client: llm.Client, source_ids: list[str]) -> Estimate:
    """Calls no model, prints no text, writes nothing."""
    found = settings(client.config)
    standing = {entry.source_id: entry for entry in status(store, client.config, source_ids)}
    sources = from_cache = calls = lines_billed = tokens_in = 0
    for source_id in source_ids:
        entry = standing[source_id]
        if entry.state == "current" or not entry.unidentified:
            continue
        row = store.source(source_id) or {}
        summary = summary_of(row)
        sources += 1
        missed = []
        for context, window in windows(
            lines_of(store, source_id), found["window_lines"], found["context_lines"]
        ):
            if not _unidentified(window, found["unidentified_label"]):
                continue
            variables = _variables(found, summary, context, window)
            if client.lookup(STAGE, prompt(), variables) is None:
                missed.append((variables, window))
        if not missed:
            from_cache += 1
            continue
        calls += len(missed)
        for variables, window in missed:
            tokens_in += len(prompt().render(variables)) // 4 + 1
            lines_billed += len(_unidentified(window, found["unidentified_label"]))
    tokens_out = lines_billed * OUTPUT_TOKENS_PER_LINE
    return Estimate(
        sources,
        from_cache,
        calls,
        lines_billed,
        tokens_in,
        tokens_out,
        llm.price_usd(found["model"], tokens_in, tokens_out),
        found["model"],
    )


# ---- the pass ---------------------------------------------------------------------------


def _pick(entry: dict, allowed: set[str]) -> dict:
    who = str(entry["who"]).strip()
    lowered = who.lower()
    if lowered in NON_PERSONS or lowered == UNCLEAR:
        name = lowered
    elif who in allowed:
        name = who
    else:
        name = UNCLEAR
    confidence = 0.0 if name == UNCLEAR else float(entry["confidence"])
    return {"position": int(entry["index"]), "name": name, "confidence": confidence}


def build(
    store: Store, client: llm.Client, source_id: str, *, created_by: str, run_id: str = ""
) -> dict:
    """Ask the model window by window, keep one pick per unidentified line, store the picks and
    the artifact. Returns counts only."""
    row = store.source(source_id)
    if row is None:
        raise KeyError(f"unknown source {source_id!r}")
    found = settings(client.config)
    allowed = allowed_names(found)
    summary = summary_of(row)
    label = found["unidentified_label"]
    picks: dict[int, dict] = {}
    replies = []
    for context, window in windows(
        lines_of(store, source_id), found["window_lines"], found["context_lines"]
    ):
        wanted = {line["idx"] for line in _unidentified(window, label)}
        if not wanted:
            continue

        def check(parsed: dict, wanted: set[int] = wanted) -> None:
            if not any(int(entry["index"]) in wanted for entry in parsed["lines"]):
                raise ValueError("the reply names none of the window's unidentified lines")

        reply = client.complete(
            STAGE,
            prompt(),
            _variables(found, summary, context, window),
            tag=f"{source_id}-w{window[0]['idx']}",
            check=check,
        )
        replies.append(reply)
        for entry in reply.parsed["lines"]:
            pick = _pick(entry, allowed)
            if pick["position"] in wanted:
                picks[pick["position"]] = pick
    inputs_record = inputs(store, source_id, client.config)
    key = canonical_sha256(inputs_record)
    cost = sum(reply.cost_usd or 0.0 for reply in replies)
    with store.transaction():
        written = store.replace_line_speakers(
            source_id, picks.values(), build_key=key, created_by=created_by
        )
        store.record_artifact(
            source_id,
            NODE,
            path="",
            sha256=key,
            state="built",
            inputs=inputs_record,
            provenance={
                "stage": STAGE,
                "prompt_id": prompt().id,
                "prompt_version": prompt().version,
                "model": found["model"],
                "windows": len(replies),
                "run_id": run_id,
                "tokens_in": sum(reply.tokens_in for reply in replies),
                "tokens_out": sum(reply.tokens_out for reply in replies),
                "cost_usd": round(cost, 6),
                "cached": all(reply.cached for reply in replies),
            },
            created_by=created_by,
        )
    confident = sum(1 for pick in picks.values() if confident_line_pick(pick))
    return {
        "source": source_id,
        "windows": len(replies),
        "picks": written,
        "confident": confident,
        "unsure": written - confident,
        "cached": all(reply.cached for reply in replies) if replies else True,
        "cost_usd": round(cost, 6),
    }


def record_failed(store: Store, source_id: str, reason: str, *, created_by: str) -> None:
    """The source's artifact as failed, with the reason; its picks, if any, stay."""
    store.record_artifact(
        source_id,
        NODE,
        path="",
        sha256="",
        state="failed",
        inputs={},
        provenance={"stage": STAGE, "failed": reason[:300]},
        created_by=created_by,
    )


__all__ = [
    "Estimate",
    "SourceStatus",
    "build",
    "estimate",
    "inputs",
    "settings",
    "status",
    "summary_of",
    "windows",
]
