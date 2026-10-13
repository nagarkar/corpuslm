"""Concepts: the lexicon derived from claims by recurrence, and the tags that tie claims to it.

A concept is a recurring idea or phrase a body of work keeps returning to ("beat", "choice"). It
is not declared in advance and not approved one by one: the concept pass (`build`) reads a
source's claims in batches, one cheap model call each, and names the concepts each claim uses,
reusing the labels already in use so that labels converge. The tags are rows of
`claim_concepts`; the concepts are rows of `concepts` with their counts of sections, claims and
sources (nagarkar/corpuslm#47, milestone "Artifacts: monologues").

Whether a concept is IN is derived, never stored: the owner's verdict in force wins; else it is
in when it recurs in at least `[concepts].min_sections` sections and `[concepts].min_claims`
claims (document frequency, the text-mining standard); the boundary band, from `min_sections` up
to `min_sections + 1`, is shown to the owner for a verdict; everything else is out. Counts move as
claims arrive, so a concept crosses the gate on its own.

Connections between concepts are co-occurrence counts over claims (`Store.concept_edges`),
computed when asked, with the claims as evidence. No relation is ever inferred or stored.

Freshness per source goes through the `artifacts` row for node `concept_tags`, whose inputs are
the source's claims build key, the prompt hash and the model, so `status` names what changed.
`estimate` reads only the reply cache and calls no model.

Never imports: cli, clikit. Reads the database only through `Store`.

Decisions:
- Concepts are derived from claims by recurrence and need no approval above the gate: if the
  owner says something again and again, that is already a concept. Only the boundary band is
  put to the owner. (owner)
- Inferred relations between concepts are out of bounds; an edge is the count of claims two
  concepts share, recomputed from the tags, with those claims as its evidence. (owner)
- The gate is document frequency by section (at least `min_sections` sections and `min_claims`
  claims), not a standard-deviation rule: mention counts are heavy-tailed. (owner)
- Whether a concept is in is derived at read time from counts, gate and verdict; nothing stores
  it, so a gate change or a new claim never leaves a stale state behind. Enforced by:
  tests/test_concepts.py::test_in_out_and_boundary_follow_the_gate_and_a_verdict_wins.

Terms:
- **concept**: a recurring idea or phrase named by the concept pass, a row of `concepts`, with
  its label, alternates and counts.
- **concept tag**: a row of `claim_concepts`: one claim uses one concept.
- **gate**: the recurrence a concept needs to be in: `min_sections` sections and `min_claims`
  claims, in the settings file.
- **boundary**: a concept whose section count is `min_sections` or `min_sections + 1`, put to
  the owner for a verdict; the verdict in force wins over the gate.
- **concept edge**: two concepts and the count of claims they share, computed on demand.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import claims as claims_module
from . import llm, prompts
from .identity import canonical_sha256
from .store import Store

STAGE = "concepts"
NODE = "concept_tags"
DEFAULTS = {"min_sections": 3, "min_claims": 5, "batch_claims": 120}
VOCABULARY_LIMIT = 400  # labels shown to the model per call, most used first
ASSUMED_OUTPUT_PER_INPUT = 0.3
_PUNCTUATION = re.compile(r"[^\w\s'-]+")


def prompt() -> prompts.Prompt:
    return prompts.load("concepts")


def settings(config: dict) -> dict:
    section = config.get("concepts") or {}
    if not section.get("model"):
        raise llm.NotConfigured("[concepts].model is missing from the settings file")
    return {"model": str(section["model"])} | {
        name: int(section.get(name, default)) for name, default in DEFAULTS.items()
    }


def normalize(label: str) -> str:
    """The key two spellings of one label share: lowercase, no punctuation, single spaces."""
    return " ".join(_PUNCTUATION.sub(" ", label.lower()).split())


@dataclass(frozen=True, slots=True)
class Gate:
    min_sections: int
    min_claims: int

    def state_of(self, concept: dict, verdict: str | None) -> str:
        """`in`, `out` or `boundary`."""
        if verdict in ("in", "out"):
            return verdict
        sections, claims = concept["section_count"], concept["claim_count"]
        if sections >= self.min_sections and claims >= self.min_claims:
            return "boundary" if sections <= self.min_sections + 1 else "in"
        return "out"


def gate(config: dict) -> Gate:
    found = settings(config)
    return Gate(found["min_sections"], found["min_claims"])


def states(store: Store, config: dict) -> dict[str, str]:
    """Concept key -> state, for every concept."""
    this_gate = gate(config)
    verdicts = store.current_concept_verdicts()
    return {
        concept["key"]: this_gate.state_of(concept, verdicts.get(concept["id"]))
        for concept in store.concepts()
    }


def vocabulary(store: Store, config: dict) -> list[str]:
    """The labels the model is told to reuse: every concept in or boundary, most used first,
    at most VOCABULARY_LIMIT."""
    known = states(store, config)
    return [
        concept["label"]
        for concept in store.concepts()
        if known[concept["key"]] in ("in", "boundary")
    ][:VOCABULARY_LIMIT]


# ---- what stands ------------------------------------------------------------------------


def inputs(store: Store, source_id: str, config: dict) -> dict:
    artifact = store.artifact(source_id, claims_module.NODE)
    found = settings(config)
    return {
        "claims": artifact["sha256"] if artifact else "",
        "prompt": prompt().sha256,
        "model": found["model"],
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


# ---- the pass ---------------------------------------------------------------------------


def _batches(rows: list[dict], size: int) -> list[list[dict]]:
    return [rows[start : start + size] for start in range(0, len(rows), size)]


def _variables(source_row: dict, batch: list[dict], labels: list[str]) -> dict:
    return {
        "source_label": claims_module.SOURCE_LABELS.get(source_row["kind"], "a source"),
        "vocabulary": "\n".join(f"- {label}" for label in labels) or "(none yet)",
        "claims": "\n".join(f"[{index}] {row['text']}" for index, row in enumerate(batch)),
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
    """Calls no model, writes nothing. The vocabulary shown to each call changes as tags are
    written, so a cache hit is counted only for the first call of a source made against the
    vocabulary as it stands now."""
    found = settings(client.config)
    labels = vocabulary(store, client.config)
    standing = {entry.source_id: entry for entry in status(store, client.config, source_ids)}
    sources = from_cache = calls = tokens_in = 0
    for source_id in source_ids:
        if standing[source_id].state in ("current", "no_claims"):
            continue
        row = store.source(source_id)
        if row is None:
            continue
        batches = _batches(store.claims(source_id), found["batch_claims"])
        sources += 1
        missed = []
        for number, batch in enumerate(batches):
            variables = _variables(row, batch, labels)
            if number == 0 and client.lookup(STAGE, prompt(), variables) is not None:
                continue
            missed.append(variables)
        if not missed:
            from_cache += 1
            continue
        calls += len(missed)
        tokens_in += sum(
            _estimate_tokens(prompt().body + v["vocabulary"] + v["claims"]) for v in missed
        )
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
    """Tag one source's claims, batch by batch, each batch shown the vocabulary as it stands
    after the batches before it. Returns counts only."""
    row = store.source(source_id)
    if row is None:
        raise KeyError(f"unknown source {source_id!r}")
    found = settings(client.config)
    rows = store.claims(source_id)
    if not rows:
        raise ValueError(f"{source_id} has no claims; run `claims build` first")
    inputs_record = inputs(store, source_id, client.config)
    key = canonical_sha256(inputs_record)
    tagged = new_labels = 0
    replies = []
    known = {concept["key"] for concept in store.concepts()}  # every concept, gated or not
    for batch in _batches(rows, found["batch_claims"]):
        labels = vocabulary(store, client.config)
        reply = client.complete(
            STAGE,
            prompt(),
            _variables(row, batch, labels),
            tag=f"{source_id}-c{batch[0]['position']}",
        )
        replies.append(reply)
        tags: dict[str, list[tuple[str, str]]] = {claim["id"]: [] for claim in batch}
        for entry in reply.parsed["claims"]:
            index = int(entry["index"])
            if not 0 <= index < len(batch):
                continue
            for label in entry["concepts"]:
                label = " ".join(label.split())
                normalized = normalize(label)
                if not normalized:
                    continue
                if normalized not in known:
                    known.add(normalized)
                    new_labels += 1
                tags[batch[index]["id"]].append((normalized, label))
        tagged += store.tag_claims(tags, build_key=key, created_by=created_by)
        store.refresh_concept_counts()
    cost = sum(reply.cost_usd or 0.0 for reply in replies)
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
            "batches": len(replies),
            "tokens_in": sum(reply.tokens_in for reply in replies),
            "tokens_out": sum(reply.tokens_out for reply in replies),
            "cost_usd": round(cost, 6),
            "cached": all(reply.cached for reply in replies),
        },
        created_by=created_by,
    )
    return {
        "source": source_id,
        "batches": len(replies),
        "claims": len(rows),
        "tags": tagged,
        "new_labels": new_labels,
        "cached": all(reply.cached for reply in replies),
        "cost_usd": round(cost, 6),
    }


# ---- what the owner sees ----------------------------------------------------------------


def summary(store: Store, config: dict) -> dict:
    """Counts by state, the boundary list and the section-count distribution."""
    this_gate = gate(config)
    verdicts = store.current_concept_verdicts()
    counts = {"in": 0, "out": 0, "boundary": 0}
    boundary = []
    for concept in store.concepts():
        state = this_gate.state_of(concept, verdicts.get(concept["id"]))
        counts[state] += 1
        if state == "boundary":
            boundary.append(
                {
                    "key": concept["key"],
                    "label": concept["label"],
                    "sections": concept["section_count"],
                    "claims": concept["claim_count"],
                    "sources": concept["source_count"],
                }
            )
    return {
        "concepts": store.concept_count(),
        "states": counts,
        "verdicts": len(verdicts),
        "gate": {"min_sections": this_gate.min_sections, "min_claims": this_gate.min_claims},
        "boundary": boundary,
        "sections_histogram": store.concept_section_histogram(),
    }


def listing(store: Store, config: dict, state: str | None = None) -> list[dict]:
    """Every concept with its state and counts, most used first; `state` narrows it."""
    this_gate = gate(config)
    verdicts = store.current_concept_verdicts()
    out = []
    for concept in store.concepts():
        found = this_gate.state_of(concept, verdicts.get(concept["id"]))
        if state and found != state:
            continue
        out.append(
            {
                "key": concept["key"],
                "label": concept["label"],
                "state": found,
                "sections": concept["section_count"],
                "claims": concept["claim_count"],
                "sources": concept["source_count"],
                "verdict": verdicts.get(concept["id"], ""),
            }
        )
    return out


def edges(store: Store, key: str) -> list[dict]:
    concept = store.concept(normalize(key))
    if concept is None:
        raise KeyError(f"no concept {key!r}")
    return store.concept_edges(concept["id"])


def verdict(store: Store, key: str, ruling: str, *, answered_through: str, created_by: str) -> dict:
    if ruling not in ("in", "out"):
        raise ValueError("a verdict is in or out")
    concept = store.concept(normalize(key))
    if concept is None:
        raise KeyError(f"no concept {key!r}")
    store.append_concept_verdict(
        concept["id"], ruling, answered_through=answered_through, created_by=created_by
    )
    return {"key": concept["key"], "verdict": ruling}


__all__ = [
    "Estimate",
    "Gate",
    "SourceStatus",
    "build",
    "edges",
    "estimate",
    "gate",
    "listing",
    "normalize",
    "settings",
    "states",
    "status",
    "summary",
    "verdict",
    "vocabulary",
]
