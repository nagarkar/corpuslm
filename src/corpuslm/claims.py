"""Claims: what a source claims, one short sentence at a time, tied to its segments.

A claim is a restatement in a model's words, 25 words or fewer, of one thing a source asserts,
defines, illustrates, tells, asks, predicts or disputes, with the segment positions that support
it. It is not a quotation and not bound to sentence boundaries. Claims are rows of `claims`, one
list per source, rebuilt whole by the claim pass; they are the building block of concepts,
moments and monologues (nagarkar/corpuslm#45, milestone "Artifacts: monologues").

The claim pass (`build`) reads a source's best segments (`Store.best_segments`: the cleaned ones
when the source has them, else the raw ones) in sections of about `[claims].section_chars`
characters, one cheap model call each, as ytlm's video digest did. A claim that cites no line
of its own section is left out and counted, never repaired. The pass records an `artifacts` row
(node `claims`) whose inputs are the hashes of the text, the prompt and the model name plus the
section size, so `status` can say what changed. `estimate` counts what a run would send from the
reply cache alone and calls no model.

`import_digests` turns ytlm's `videos/<id>/digest.json` files into claims with no model call,
keeping each digest's own provenance as the artifact's inputs under `imported_from`; such a source
reads `imported` in `status` and is never rebuilt until the owner asks.

The prompt is chosen by the source kind as the cleanup prompt is (`speaker_map.claims_prompt_id`):
ytlm's `claims` for YouTube, Vimeo, file and text sources, untouched; `claims_bee` for Bee
sources, which also extracts directives by the conversation's form (corpuslm.form) and receives
the form and the owner's first name (`[claims].owner_first_name`). The form is a build input of
a Bee source, so a reclassified source reads stale.

Never imports: cli, clikit. Reads the database only through `Store`.

Decisions:
- A claims prompt variant per source kind, chosen by the kind as the cleanup prompt is; ytlm's
  prompt is untouched. (owner, 2026-10-21) Enforced by:
  tests/test_directives.py::test_a_bee_source_uses_the_bee_prompt_and_a_video_the_plain_one.
- Links between claims are out of bounds: a wrong stored link poisons every later pass, and
  verifying them is combinatorial. Reasoning lives in the perspective sheet and in each recipe
  run's output. (owner)
- A claim is a restatement, never a quotation, and not bound to sentence boundaries: a 26-word
  sentence yields a shorter claim, and one sentence may yield two. (owner)
- Nothing regenerates when an output for the same inputs exists: the pass serves cached replies
  and `status` names what changed before a rebuild. (owner)
- Imported digests are never rebuilt by the pass on their own: they stay `imported` until the
  owner runs `build` for that source. Why: a rebuild is a paid run. Enforced by:
  tests/test_claims.py::test_an_imported_source_stays_imported_and_is_not_billed.
- A reply the model gets wrong for one source (a reply that will not parse twice, a vendor
  error, a truncated reply) records that source's artifact `failed` with the reason and the run
  goes on to the next source; only a spend cap stops a run. Seen 2026-10-13: one video's reply
  listed seven segments for a claim and the whole YouTube run stopped at it. Enforced by:
  tests/test_claims.py::test_a_bad_reply_fails_one_source_and_the_run_goes_on.

Terms:
- **claim**: one short sentence (25 words or fewer) restating one thing a source says, with its
  kind and the segment positions that support it; a row of `claims`.
- **claim kind**: assertion, definition, example, story, question, prediction, disagreement,
  directive (corpuslm.directives; the Bee prompt only).
- **claim pass**: the model calls per source that list its claims, one per section.
- **section**: the span of a source's segments one claim-pass call reads, about
  `section_chars` characters; a text source's sections follow its headings
  (corpuslm.text_source).
- **build key**: the hash of a claim pass's inputs (text, prompt, model, section size); every
  claim of the pass carries it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from llmlm import llm

from . import directives as directives_module
from . import events as events_module
from . import form as form_module
from . import judge as judge_module
from . import prompts, speaker_map, text_source
from .identity import canonical_sha256
from .store import Store

STAGE = "claims"
NODE = "claims"
MAX_SEGMENTS = 6  # per claim; the prompt asks for the clearest few
SECTION_CHARS = 6000  # default section size, about seven minutes of talk
ASSUMED_OUTPUT_PER_INPUT = 0.35  # what `estimate` assumes a reply costs per token sent
SOURCE_LABELS = {
    "youtube": "a YouTube video",
    "bee": "a recorded conversation",
    "text": "a text",
}
IMPORTED_FROM = "ytlm.digest"
UNKNOWN_FORM = "unknown"  # a Bee source not yet classified: the prompt reads it as company


def prompt(kind: str = "youtube") -> prompts.Prompt:
    """The claims prompt of a source kind: the Bee variant (`claims_bee`, which also extracts
    directives) for `bee`, ytlm's `claims` for every other kind."""
    return prompts.load(speaker_map.claims_prompt_id(kind))


def settings(config: dict) -> dict:
    """`[claims]` of the settings: the model, the section size, the owner's first name (for the
    Bee prompt) and the directive gate's threshold."""
    section = config.get("claims") or {}
    if not section.get("model"):
        raise llm.NotConfigured("[claims].model is missing from the settings file")
    return {
        "model": str(section["model"]),
        "section_chars": int(section.get("section_chars", SECTION_CHARS)),
        "owner_first_name": str(section.get("owner_first_name") or ""),
        "directive_threshold": float(
            section.get("directive_threshold", directives_module.DEFAULT_THRESHOLD)
        ),
    }


def sections(segments: list[dict], size: int) -> list[list[dict]]:
    """Whole segments, in order, cut once a section holds `size` characters."""
    out: list[list[dict]] = []
    current: list[dict] = []
    character_count = 0
    for segment in segments:
        current.append(segment)
        character_count += len(segment["text"])
        if character_count >= size:
            out.append(current)
            current, character_count = [], 0
    if current:
        out.append(current)
    return out


def sections_of(source_row: dict, segments: list[dict], size: int) -> list[list[dict]]:
    """A text source's sections follow its headings (corpuslm.text_source); any other source is
    cut every `size` characters."""
    recorded = (
        text_source.sections_of(source_row) if source_row.get("kind") == text_source.KIND else []
    )
    if not recorded:
        return sections(segments, size)
    by_index = {segment["idx"]: segment for segment in segments}
    out = []
    for section in recorded:
        part = [
            by_index[index] for index in range(section.first, section.last + 1) if index in by_index
        ]
        if part:
            out.append(part)
    return out


def text_sha256(segments: list[dict]) -> str:
    """The hash of what the pass reads: positions, speakers and text."""
    return canonical_sha256(
        [[segment["idx"], segment.get("speaker", ""), segment["text"]] for segment in segments]
    )


def form_of(store: Store, source_row: dict) -> str:
    """The form the Bee prompt reads (corpuslm.form), `unknown` until classified; `''` for every
    other kind, whose prompt has no form."""
    if source_row.get("kind") != "bee":
        return ""
    return form_module.label_of(store, source_row["id"]) or UNKNOWN_FORM


def inputs(store: Store, source_id: str, config: dict) -> dict:
    found = settings(config)
    row = store.source(source_id) or {"id": source_id, "kind": "youtube"}
    record = {
        "text": text_sha256(store.best_segments(source_id)),
        "prompt": prompt(row["kind"]).sha256,
        "model": found["model"],
        "section_chars": found["section_chars"],
    }
    if row["kind"] == "bee":
        # the form decides what the Bee prompt extracts, so a change rebuilds
        record["form"] = form_of(store, row)
    return record


def build_key(inputs_record: dict) -> str:
    return canonical_sha256(inputs_record)


def _lines(segments: list[dict]) -> str:
    return "\n".join(
        f"[{segment['idx']}] {segment.get('speaker', '')}: {segment['text']}"
        for segment in segments
    )


def _variables(
    source_row: dict,
    section_index: int,
    total: int,
    part: list[dict],
    *,
    form: str = "",
    owner: str = "",
) -> dict:
    return {
        "source_label": SOURCE_LABELS.get(source_row["kind"], "a source"),
        "title": source_row.get("title") or "(untitled)",
        "date": (source_row.get("published_at") or "")[:10] or "(unknown)",
        "section": f"{section_index + 1} of {total}",
        "transcript": _lines(part),
        "form": form or UNKNOWN_FORM,
        "owner": owner or "(the owner)",
    }


def _tag(source_id: str, section_index: int) -> str:
    return f"{source_id}-s{section_index}"


# ---- what stands ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceStatus:
    source_id: str
    kind: str
    claims: int
    state: str  # current | stale | imported | none
    reason: str  # for stale: what changed


def status(store: Store, config: dict | None, source_ids: list[str]) -> list[SourceStatus]:
    """What stands for each source: `current` (built from the inputs in force), `stale` (an
    input changed; the reason names which), `imported` (from a ytlm digest; kept until the
    owner rebuilds), or `none`. With no `[claims]` settings every built source reads `stale`
    with the reason `no [claims] settings`."""
    counts = store.claim_counts_by_source()
    out = []
    for source_id in source_ids:
        row = store.source(source_id)
        kind = row["kind"] if row else ""
        artifact = store.artifact(source_id, NODE)
        if artifact is None:
            out.append(SourceStatus(source_id, kind, counts.get(source_id, 0), "none", ""))
            continue
        recorded = json.loads(artifact["inputs"])
        provenance = json.loads(artifact["provenance"])
        if provenance.get("imported_from"):
            out.append(SourceStatus(source_id, kind, counts.get(source_id, 0), "imported", ""))
            continue
        if config is None:
            out.append(
                SourceStatus(
                    source_id, kind, counts.get(source_id, 0), "stale", "no [claims] settings"
                )
            )
            continue
        current = inputs(store, source_id, config)
        changed = sorted(key for key in current if recorded.get(key) != current[key])
        if artifact["state"] == "built" and not changed:
            out.append(SourceStatus(source_id, kind, counts.get(source_id, 0), "current", ""))
        else:
            reason = ", ".join(f"{key} changed" for key in changed) or f"state {artifact['state']}"
            out.append(SourceStatus(source_id, kind, counts.get(source_id, 0), "stale", reason))
    return out


# ---- what a run would send ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Estimate:
    """What the pass would do for a set of sources, from the cache alone."""

    sources: int  # sources that would be built (not current, not imported)
    from_cache: int  # of those, the ones every section of which the cache holds
    calls: int  # the model calls that would be billed
    tokens_in: int
    tokens_out: int
    usd: float | None  # None when the model has no price row
    model: str


def estimate(store: Store, client: llm.Client, source_ids: list[str]) -> Estimate:
    """Calls no model, prints no text, writes nothing."""
    found = settings(client.config)
    standing = {entry.source_id: entry for entry in status(store, client.config, source_ids)}
    sources = from_cache = calls = tokens_in = 0
    for source_id in source_ids:
        if standing[source_id].state in ("current", "imported"):
            continue
        row = store.source(source_id)
        if row is None:
            continue
        parts = sections_of(row, store.best_segments(source_id), found["section_chars"])
        if not parts:
            continue
        sources += 1
        chosen = prompt(row["kind"])
        extra = {"form": form_of(store, row), "owner": found["owner_first_name"]}
        missed = [
            part
            for section_index, part in enumerate(parts)
            if client.lookup(
                STAGE, chosen, _variables(row, section_index, len(parts), part, **extra)
            )
            is None
        ]
        if not missed:
            from_cache += 1
            continue
        calls += len(missed)
        tokens_in += sum(_estimate_tokens(chosen.body + _lines(part)) for part in missed)
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


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4 + 1)


# ---- the pass ---------------------------------------------------------------------------


def record_failed(store: Store, source_id: str, reason: str, *, created_by: str) -> None:
    """The source's `claims` artifact as failed, with the reason; its claims, if any, stay."""
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


def build(
    store: Store,
    client: llm.Client,
    source_id: str,
    *,
    created_by: str,
    run_id: str = "",
    judge: judge_module.Judge | None = None,
    events: events_module.Events | None = None,
) -> dict:
    """Ask the model section by section, check every cited segment, store the claims and the
    artifact. A Bee source's directives pass the form rule and, in a monologue, Jev's gate
    (corpuslm.directives; `judge` None leaves a monologue's candidates ungated and says so).
    Once the transaction commits, `claims.built` is published to `events` (corpuslm.events).
    Returns counts only."""
    row = store.source(source_id)
    if row is None:
        raise KeyError(f"unknown source {source_id!r}")
    found = settings(client.config)
    segments = store.best_segments(source_id)
    parts = sections_of(row, segments, found["section_chars"])
    known = {segment["idx"] for segment in segments}
    claims: list[dict] = []
    dropped = 0
    replies = []
    chosen = prompt(row["kind"])
    form = form_of(store, row)
    extra = {"form": form, "owner": found["owner_first_name"]}
    for section_index, part in enumerate(parts):
        first, last = part[0]["idx"], part[-1]["idx"]
        reply = client.complete(
            STAGE,
            chosen,
            _variables(row, section_index, len(parts), part, **extra),
            tag=_tag(source_id, section_index),
        )
        replies.append(reply)
        for claim in reply.parsed["claims"]:
            cited = list(
                dict.fromkeys(
                    index
                    for index in claim["segments"]
                    if first <= index <= last and index in known
                )
            )[:MAX_SEGMENTS]
            if not cited:
                dropped += 1
                continue
            claims.append(
                {
                    "section": section_index,
                    "segments": cited,
                    "text": " ".join(claim["statement"].split()),
                    "kind": claim["kind"],
                    "speaker": claim["speaker"].strip(),
                    **{
                        key: claim[key]
                        for key in ("addressee", "heard", "else_kind")
                        if claim.get(key) is not None
                    },
                }
            )
    gate_stats: dict = {}
    if row["kind"] == "bee":
        claims, gate_stats = directives_module.gate(
            judge,
            claims,
            form=form,
            threshold=found["directive_threshold"],
            tag_prefix=source_id,
        )
    else:
        claims = [directives_module._without_directive_fields(claim) for claim in claims]
    inputs_record = inputs(store, source_id, client.config)
    key = build_key(inputs_record)
    cost = sum(reply.cost_usd or 0.0 for reply in replies) + gate_stats.get("cost_usd", 0.0)
    with store.transaction():
        written = store.replace_claims(
            source_id, claims, build_key=key, run_id=run_id, created_by=created_by
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
                "prompt_id": chosen.id,
                "prompt_version": chosen.version,
                "model": found["model"],
                "sections": len(parts),
                "tokens_in": sum(reply.tokens_in for reply in replies),
                "tokens_out": sum(reply.tokens_out for reply in replies),
                "cost_usd": round(cost, 6),
                "cached": all(reply.cached for reply in replies),
                **({"form": form, "directives": gate_stats} if gate_stats else {}),
            },
            created_by=created_by,
        )
    events_module.publish(
        events,
        events_module.CLAIMS_BUILT,
        {"source": source_id},
        {"claims": written, "directives": gate_stats.get("directives", 0)},
    )
    return {
        "source": source_id,
        "sections": len(parts),
        "claims": written,
        "directives": gate_stats.get("directives", 0),
        "directives_demoted": gate_stats.get("demoted", 0),
        "gate": gate_stats.get("gate", "none"),
        "dropped": dropped,
        "cached": all(reply.cached for reply in replies) if replies else True,
        "cost_usd": round(cost, 6),
    }


# ---- ytlm's digests ---------------------------------------------------------------------


def import_digests(
    store: Store, directory: Path, source_ids: list[str], *, created_by: str, dry_run: bool
) -> dict:
    """Each `<directory>/<source id>/digest.json` of a source that has no claims yet becomes
    that source's claims, with no model call. A source with claims already is left alone. A
    claim citing no segment the database holds is left out and counted. Returns counts."""
    imported = skipped = missing = dropped = claims_written = 0
    for source_id in source_ids:
        digest_path = Path(directory) / source_id / "digest.json"
        if not digest_path.is_file():
            missing += 1
            continue
        if store.claim_count(source_id):
            skipped += 1
            continue
        digest = json.loads(digest_path.read_text(encoding="utf-8"))
        provenance = digest.get("provenance") or {}
        size = int(provenance.get("section_chars") or SECTION_CHARS)
        segments = store.segments(source_id)
        known = {segment["idx"] for segment in segments}
        section_of = {
            segment["idx"]: section_index
            for section_index, part in enumerate(sections(segments, size))
            for segment in part
        }
        claims: list[dict] = []
        for claim in digest.get("claims") or []:
            cited = [index for index in claim["segments"] if index in known][:MAX_SEGMENTS]
            if not cited:
                dropped += 1
                continue
            claims.append(
                {
                    "section": section_of.get(min(cited), 0),
                    "segments": cited,
                    "text": " ".join(str(claim["statement"]).split()),
                    "kind": claim["kind"],
                    "speaker": str(claim.get("speaker", "")).strip(),
                }
            )
        imported += 1
        claims_written += len(claims)
        if dry_run:
            continue
        inputs_record = {
            "text": provenance.get("transcript_sha256", ""),
            "prompt": provenance.get("prompt_sha256", ""),
            "model": provenance.get("model", ""),
            "section_chars": size,
        }
        key = hashlib.sha256(digest_path.read_bytes()).hexdigest()
        with store.transaction():
            store.replace_claims(source_id, claims, build_key=key, created_by=created_by)
            store.record_artifact(
                source_id,
                NODE,
                path="",
                sha256=key,
                state="built",
                inputs=inputs_record,
                provenance={
                    "imported_from": IMPORTED_FROM,
                    "digest_schema": digest.get("schema", ""),
                    "cost_usd": 0.0,
                },
                created_by=created_by,
            )
    return {
        "imported": imported,
        "claims": claims_written,
        "dropped": dropped,
        "skipped": skipped,
        "missing": missing,
    }


__all__ = [
    "Estimate",
    "SourceStatus",
    "build",
    "build_key",
    "estimate",
    "import_digests",
    "inputs",
    "sections",
    "settings",
    "status",
]
