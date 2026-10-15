"""Moments: the building block of monologues, one span of one source with a one-line insight.

A moment is a stretch of a source where something worth retelling happens: a span of segments
and one sentence saying what it shows. Monologues are made from sets of moments
(nagarkar/corpuslm#48, milestone "Artifacts: monologues"). Moments come from two places:

- `import_notes`: the owner's insight-moment notes (markdown files, one heading per
  conversation, one `### Moment N: <insight>` per moment whose first bullet names a seconds
  range). Each becomes a moment of origin `import`, approved on import, the span mapped from
  seconds to segment positions. A moment already present is left alone, so the import is safe
  to run again. The notes' prose stays in the files.
- `build`: the proposal pass, one cheap model call per source over its claims, proposing at
  most `[moments].max_moments` moments of origin `model`, each with the claims it draws on; the
  span runs from the first to the last segment those claims cite. The model's moments of a
  source are rebuilt whole; the imported ones stay.

Approved, declined or proposed is derived from the verdict in force for the moment's subject
(source and span), never stored, so a rebuild that proposes the same span keeps its ruling. Only
approved moments feed a recipe. Freshness per source goes through the `artifacts` row for node
`moments` (inputs: the source's claims build key, the prompt hash, the model). `estimate` reads
only the reply cache.

Never imports: cli, clikit. Reads the database only through `Store`.

Decisions:
- Moments are model-proposed and owner-approved on the review page; only approved moments feed
  recipes. The owner's own notes import as approved. (owner)
- A moment retells generically: no family names; the insight names "my son" or "a friend", never
  a person. (owner)
- A moment's identity across rebuilds is its source and span, so a ruling survives a rebuild
  that proposes the same span. Enforced by:
  tests/test_moments.py::test_a_rebuild_keeps_the_ruling_on_the_same_span.
- A rebuild never deletes a moment the owner has ruled on, approved or declined, even when the
  new claims no longer propose its span. Why: 2026-10-14 the Bee claims were rebuilt from
  corrected speakers and 44 approved moments would have gone. Enforced by:
  tests/test_moments.py::test_a_rebuild_keeps_every_ruled_moment.

Terms:
- **moment**: one span of one source with a one-line insight, a row of `moments`.
- **moment verdict**: the owner's ruling, approved or declined, a row of `moment_verdicts`; the
  newest per (source, span) is in force.
- **insight**: the moment's one line: what it shows, 25 words or fewer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import claims as claims_module
from . import llm, prompts
from .identity import canonical_sha256
from .store import Store

STAGE = "moments"
NODE = "moments"
DEFAULT_MAX_MOMENTS = 8
ASSUMED_OUTPUT_PER_INPUT = 0.2
_CONVERSATION_HEADING = re.compile(r"^## .*?(\d{6,10})")
_MOMENT_HEADING = re.compile(r"^### Moment \d+\s*:\s*(.+?)\s*$")
_RANGE = re.compile(r"(\d{1,6})\s*(?:s|sec|seconds)?\s+to\s+(\d{1,6})\s*(?:s\b|sec|seconds)")


def prompt() -> prompts.Prompt:
    return prompts.load("moments")


def settings(config: dict) -> dict:
    section = config.get("moments") or {}
    if not section.get("model"):
        raise llm.NotConfigured("[moments].model is missing from the settings file")
    return {
        "model": str(section["model"]),
        "max_moments": int(section.get("max_moments", DEFAULT_MAX_MOMENTS)),
    }


def state_of(verdict: str | None) -> str:
    return verdict if verdict in ("approved", "declined") else "proposed"


# ---- the owner's notes ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Note:
    conversation: str  # the Bee conversation id
    insight: str
    first_second: int
    last_second: int


def parse_notes(text: str, *, fallback_conversation: str = "") -> tuple[list[Note], int]:
    """The moments one note file names, and how many headings had no usable range."""
    conversation = fallback_conversation
    notes: list[Note] = []
    unranged = 0
    lines = text.splitlines()
    for index, line in enumerate(lines):
        heading = _CONVERSATION_HEADING.match(line)
        if heading:
            conversation = heading.group(1)
            continue
        if not conversation and line.startswith("# "):
            found = re.search(r"(\d{6,10})", line)
            if found:
                conversation = found.group(1)
        moment = _MOMENT_HEADING.match(line)
        if not moment:
            continue
        span = None
        for later in lines[index + 1 : index + 6]:
            found = _RANGE.search(later)
            if found and int(found.group(1)) < int(found.group(2)):
                span = (int(found.group(1)), int(found.group(2)))
                break
        if span is None or not conversation:
            unranged += 1
            continue
        notes.append(Note(conversation, " ".join(moment.group(1).split()), *span))
    return notes, unranged


def _positions(segments: list[dict], first_second: int, last_second: int) -> tuple[int, int]:
    """The segment positions a seconds range covers: the first segment ending after the start,
    the last one starting before the end."""
    inside = [s["idx"] for s in segments if s["t1"] > first_second and s["t0"] <= last_second]
    if not inside:
        nearest = min(segments, key=lambda s: abs(s["t0"] - first_second))
        return nearest["idx"], nearest["idx"]
    return min(inside), max(inside)


def import_notes(store: Store, directory: Path, *, created_by: str, dry_run: bool) -> dict:
    """Every `*.md` under `directory` (not recursive). Returns counts only."""
    directory = Path(directory).expanduser()
    files = sorted(directory.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"{directory} has no .md files")
    imported = skipped = unknown = unranged = 0
    sources: set[str] = set()
    for path in files:
        fallback = next(iter(re.findall(r"\d{6,10}", path.name)), "")
        notes, missing = parse_notes(
            path.read_text(encoding="utf-8"), fallback_conversation=fallback
        )
        unranged += missing
        for note in notes:
            source_id = f"bee:{note.conversation}"
            if not store.has_source(source_id):
                unknown += 1
                continue
            segments = store.best_segments(source_id)
            if not segments:
                unknown += 1
                continue
            first, last = _positions(segments, note.first_second, note.last_second)
            subject = store.moment_subject(source_id, first, last)
            if any(m["subject_sha256"] == subject for m in store.moments(source_id)):
                skipped += 1
                continue
            imported += 1
            sources.add(source_id)
            if dry_run:
                continue
            with store.transaction():
                store.add_moment(
                    source_id, first, last, note.insight, origin="import", created_by=created_by
                )
                store.append_moment_verdict(
                    source_id, subject, "approved", answered_through="review file",
                    created_by=created_by,
                )  # fmt: skip
    return {
        "files": len(files),
        "imported": imported,
        "sources": len(sources),
        "skipped": skipped,
        "unknown_source": unknown,
        "unranged": unranged,
    }


# ---- what stands ------------------------------------------------------------------------


def inputs(store: Store, source_id: str, config: dict) -> dict:
    artifact = store.artifact(source_id, claims_module.NODE)
    found = settings(config)
    return {
        "claims": artifact["sha256"] if artifact else "",
        "prompt": prompt().sha256,
        "model": found["model"],
        "max_moments": found["max_moments"],
    }


@dataclass(frozen=True, slots=True)
class SourceStatus:
    source_id: str
    state: str  # current | stale | none | no_claims
    reason: str


def status(store: Store, config: dict, source_ids: list[str]) -> list[SourceStatus]:
    out = []
    for source_id in source_ids:
        if store.claim_count(source_id) == 0:
            out.append(SourceStatus(source_id, "no_claims", ""))
            continue
        artifact = store.artifact(source_id, NODE)
        if artifact is None:
            out.append(SourceStatus(source_id, "none", ""))
            continue
        recorded = json.loads(artifact["inputs"])
        current = inputs(store, source_id, config)
        changed = sorted(key for key in current if recorded.get(key) != current[key])
        if artifact["state"] == "built" and not changed:
            out.append(SourceStatus(source_id, "current", ""))
        else:
            reason = ", ".join(f"{key} changed" for key in changed) or f"state {artifact['state']}"
            out.append(SourceStatus(source_id, "stale", reason))
    return out


# ---- the proposal pass ------------------------------------------------------------------


def _variables(source_row: dict, rows: list[dict], max_moments: int) -> dict:
    return {
        "source_label": claims_module.SOURCE_LABELS.get(source_row["kind"], "a source"),
        "title": source_row.get("title") or "(untitled)",
        "date": (source_row.get("published_at") or "")[:10] or "(unknown)",
        "max_moments": str(max_moments),
        "claims": "\n".join(
            f"[{index}] (segments {min(row['segments'])}-{max(row['segments'])}) {row['text']}"
            for index, row in enumerate(rows)
        ),
    }


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4 + 1)


@dataclass(frozen=True, slots=True)
class Estimate:
    sources: int
    from_cache: int
    calls: int
    tokens_in: int
    tokens_out: int
    usd: float | None
    model: str


def estimate(store: Store, client: llm.Client, source_ids: list[str]) -> Estimate:
    found = settings(client.config)
    standing = {entry.source_id: entry for entry in status(store, client.config, source_ids)}
    sources = from_cache = calls = tokens_in = 0
    for source_id in source_ids:
        if standing[source_id].state in ("current", "no_claims"):
            continue
        row = store.source(source_id)
        if row is None:
            continue
        sources += 1
        variables = _variables(row, store.claims(source_id), found["max_moments"])
        if client.lookup(STAGE, prompt(), variables) is not None:
            from_cache += 1
            continue
        calls += 1
        tokens_in += _estimate_tokens(prompt().body + variables["claims"])
    tokens_out = int(tokens_in * ASSUMED_OUTPUT_PER_INPUT)
    return Estimate(
        sources,
        from_cache,
        calls,
        tokens_in,
        tokens_out,
        llm.price_usd(found["model"], tokens_in, tokens_out),
        found["model"],
    )


def build(store: Store, client: llm.Client, source_id: str, *, created_by: str) -> dict:
    """Propose one source's moments from its claims. Returns counts only."""
    row = store.source(source_id)
    if row is None:
        raise KeyError(f"unknown source {source_id!r}")
    found = settings(client.config)
    rows = store.claims(source_id)
    if not rows:
        raise ValueError(f"{source_id} has no claims; run `claims build` first")
    reply = client.complete(
        STAGE, prompt(), _variables(row, rows, found["max_moments"]), tag=source_id
    )
    proposed: list[dict] = []
    dropped = 0
    for entry in reply.parsed["moments"][: found["max_moments"]]:
        cited = [rows[index] for index in entry["claims"] if 0 <= index < len(rows)]
        positions = [position for claim in cited for position in claim["segments"]]
        if not positions:
            dropped += 1
            continue
        proposed.append(
            {
                "first": min(positions),
                "last": max(positions),
                "insight": " ".join(entry["insight"].split()),
            }
        )
    inputs_record = inputs(store, source_id, client.config)
    key = canonical_sha256(inputs_record)
    with store.transaction():
        written = store.replace_model_moments(
            source_id, proposed, build_key=key, created_by=created_by
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
                "tokens_in": reply.tokens_in,
                "tokens_out": reply.tokens_out,
                "cost_usd": round(reply.cost_usd or 0.0, 6),
                "cached": reply.cached,
            },
            created_by=created_by,
        )
    return {
        "source": source_id,
        "proposed": written,
        "dropped": dropped,
        "cached": reply.cached,
        "cost_usd": round(reply.cost_usd or 0.0, 6),
    }


# ---- what the owner sees ----------------------------------------------------------------


def listing(store: Store, *, state: str | None = None, source_id: str | None = None) -> list[dict]:
    """Every moment with its state, the span in seconds, its concepts; `state` narrows it."""
    verdicts = store.current_moment_verdicts(source_id)
    out = []
    for moment in store.moments(source_id):
        found = state_of(verdicts.get(moment["subject_sha256"]))
        if state and found != state:
            continue
        segments = store.best_segments(moment["source_id"])
        by_index = {segment["idx"]: segment for segment in segments}
        first = by_index.get(moment["first_position"])
        last = by_index.get(moment["last_position"])
        row = store.source(moment["source_id"]) or {}
        out.append(
            {
                "id": moment["id"],
                "source_id": moment["source_id"],
                "title": row.get("title") or "",
                "date": (row.get("published_at") or "")[:10],
                "subject": moment["subject_sha256"],
                "first_position": moment["first_position"],
                "last_position": moment["last_position"],
                "start_seconds": first["t0"] if first else None,
                "end_seconds": last["t1"] if last else None,
                "insight": moment["insight"],
                "origin": moment["origin"],
                "state": found,
                "concepts": store.moment_concept_keys(moment),
            }
        )
    return out


def summary(store: Store) -> dict:
    verdicts = store.current_moment_verdicts()
    counts = {"approved": 0, "declined": 0, "proposed": 0}
    origins = {"import": 0, "model": 0}
    for moment in store.moments():
        counts[state_of(verdicts.get(moment["subject_sha256"]))] += 1
        origins[moment["origin"]] += 1
    return {"moments": store.moment_count(), "states": counts, "origins": origins}


def verdict(
    store: Store, moment_id: str, ruling: str, *, answered_through: str, created_by: str
) -> dict:
    if ruling not in ("approved", "declined"):
        raise ValueError("a verdict is approved or declined")
    moment = store.moment(moment_id)
    if moment is None:
        raise KeyError(f"no moment {moment_id!r}")
    store.append_moment_verdict(
        moment["source_id"],
        moment["subject_sha256"],
        ruling,
        answered_through=answered_through,
        created_by=created_by,
    )
    return {"id": moment_id, "verdict": ruling}


__all__ = [
    "Estimate",
    "Note",
    "SourceStatus",
    "build",
    "estimate",
    "import_notes",
    "inputs",
    "listing",
    "parse_notes",
    "settings",
    "state_of",
    "status",
    "summary",
    "verdict",
]
