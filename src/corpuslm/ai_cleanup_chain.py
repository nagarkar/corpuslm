"""AI cleanup chain: the builder of the AI cleanup step, the same for every source kind.

The AI cleanup (`build_ai_cleanup`) is the step after spell fix that asks a model, batch by
batch, for the edits, label-to-name bindings and ambiguities of one source
(corpuslm.ai_cleanup holds the batching and the bounds; this module holds the bookkeeping
around the calls). It reads the spell-fix document, calls a model through `corpuslm.llm.Client`
(or serves the recorded replies from the cache), applies the correction memory and the owner's
verdicts, optionally asks the arbiter about what is still pending, and records the artifact with
the hashes of its inputs. A tool describes its source with a `SourceKind` (corpuslm.chain) and a
`SpeakerScheme` (corpuslm.speaker_map) and hands the builder an `AiCleanupSetup`: the collaborators
the step needs and nothing else.

What a tool keeps: its review layer (`ReviewHooks`, the owner's verdicts and the correction
memory), its arbiter (corpuslm.arbiter, built with the tool's transport), the word that turns
the step off for a source (`declined`), the prompt directories, and the report it keeps.

`estimate` counts what a run would send, from the documents and the reply cache: it sends nothing
to a vendor and calls no model.

Never imports: db, cli, clikit.

Decisions:
- A PATCH input (the owner's verdicts, the correction memory, the arbiter's settings) is
  applied after the model's replies and never asks the model again; a held build is replayed
  from the cache. A CONFIG input (prompt, model, batch settings) applies forward only: a built
  transcript keeps the prompt and model it was built with until `rebuild_stale` is said.
  Enforced by:
  tests/test_ai_cleanup_chain.py::test_a_changed_prompt_applies_forward_only_and_a_patch_input_replays_the_cache.
- At a spend cap the step starts no new call and records the source failed (transient,
  budget); the next run continues. Enforced by:
  tests/test_ai_cleanup_chain.py::test_the_run_cap_stops_the_step_and_marks_the_rest_failed.
- A reply that is wrong for its batch is never cached and fails the source transiently.
  Enforced by:
  tests/test_ai_cleanup_chain.py::test_a_reply_outside_its_batch_fails_the_source_transiently.
- The stored names (the node, the inputs keys, the provenance keys) are the tool's and the
  first tool's; the builder never renames them. Enforced by:
  tests/test_ai_cleanup_chain.py::test_the_shape_names_the_node_the_stage_and_the_inputs.
- The owner's verdicts and the correction memory are read from the shared database
  (`DatabaseReview`, corpuslm.review) for every source kind. Enforced by:
  tests/test_ai_cleanup_chain.py::test_a_recorded_verdict_changes_the_inputs_and_a_term_rule_is_applied.
- With fixed labels the transcript's speaker source stays what the source said (a diarization
  is not "inferred"). Enforced by:
  tests/test_ai_cleanup_chain.py::test_fixed_labels_keep_the_sources_speaker_source.
- A build or rebuild records the model's suggestions for every ambiguity
  (corpuslm.model_suggestions) with the artifact, in one transaction. Enforced by:
  tests/test_ai_cleanup_chain.py::test_building_the_ai_cleanup_records_its_suggestions.
- `estimate` calls no model and bills nothing. Enforced by:
  tests/test_ai_cleanup_chain.py::test_the_estimate_calls_no_model_and_counts_what_would_be_billed.
- A landed verdict or memory rule changes a source's inputs; its batches are served from the reply
  cache, so the rebuild makes no model call and costs nothing, and `estimate` counts that source
  under `from_cache` with no billed batch. A batch the cache lacks is billed with every batch after
  it. Enforced by:
  tests/test_ai_cleanup_chain.py::test_a_landed_verdict_is_rebuilt_from_the_cache_and_the_estimate_says_so,
  tests/test_ai_cleanup_chain.py::test_a_batch_the_cache_lacks_is_billed_with_every_batch_after_it.

Terms:
- **CONFIG input**: prompt, model, batch tokens, overlap; a change applies forward only.
- **PATCH input**: verdicts, memory, arbiter settings; a change replays the cached calls.
- **held build**: a built transcript kept although a CONFIG input changed.
- **from cache**: a source whose step is not current but whose every batch the reply cache holds.
- **spend cap stop**: the run's note of the spend cap that stopped it (`SpendCapStop`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from . import ai_cleanup, basic_cleanup, llm, model_suggestions, review
from . import clock as clock_module
from . import prompts as prompts_module
from .chain import Chain, Report, canonical_sha256, is_current, record, write_if_changed
from .db import loads
from .speaker_map import SpeakerScheme
from .store import Store

__all__ = [
    "ArbiterLike",
    "DatabaseReview",
    "Estimate",
    "AiCleanupSetup",
    "NoReview",
    "ReviewHooks",
    "SpendCapStop",
    "build_ai_cleanup",
    "estimate",
]

# Inputs by kind: a change in a CONFIG input applies forward only; a change in a PATCH input is
# applied after the model's replies and never needs the model again; anything else (the spell-fix
# document itself) is a real rebuild.
CONFIG_INPUTS = frozenset({"prompt", "model", "batch_tokens", "overlap_segments"})
PATCH_INPUTS = frozenset({"verdicts", "memory", "arbiter"})

# What `estimate` assumes about the reply: tokens out per token in. A batch reply is edits only.
ASSUMED_OUTPUT_PER_INPUT = 0.25


class ReviewHooks(Protocol):
    """A tool's review layer: what the owner already said, applied to a fresh build."""

    def verdicts(self, source_id: str) -> dict[str, dict]:
        """The owner's current verdicts on the source, by review key."""
        ...

    def corrections(self) -> dict[str, dict]:
        """The correction memory: ordinary-correction rules by normalized span."""
        ...

    def memory_vocabulary(self) -> basic_cleanup.Vocabulary:
        """The memory's term verdicts as a vocabulary for `cleanup.apply_correction_memory`."""
        ...

    def reconcile(
        self, document: dict, verdicts: dict[str, dict], memory: dict[str, dict], *, now: datetime
    ) -> dict:
        """Apply verdicts and rules to the document; the counts: carried, unplaced,
        auto_resolved, already_applied and unplaced_items."""
        ...


class NoReview:
    """A tool with no review layer yet: no verdicts, no memory, nothing carried."""

    def verdicts(self, source_id: str) -> dict[str, dict]:
        return {}

    def corrections(self) -> dict[str, dict]:
        return {}

    def memory_vocabulary(self) -> basic_cleanup.Vocabulary:
        return basic_cleanup.Vocabulary(terms=[], substitutions={}, sources={})

    def reconcile(
        self, document: dict, verdicts: dict[str, dict], memory: dict[str, dict], *, now: datetime
    ) -> dict:
        return {
            "carried": 0,
            "unplaced": 0,
            "auto_resolved": 0,
            "unplaced_items": [],
            "already_applied": 0,
        }


class DatabaseReview:
    """The review layer over the shared database (corpuslm.review): the owner's verdicts and
    the correction memory as they stand, applied to a fresh build. ytlm and beelm both use it."""

    def __init__(self, store: Store):
        self.store = store

    def verdicts(self, source_id: str) -> dict[str, dict]:
        return review.verdicts_for(self.store.db, source_id)

    def corrections(self) -> dict[str, dict]:
        return review.corrections(self.store.db)

    def memory_vocabulary(self) -> basic_cleanup.Vocabulary:
        terms = review.term_vocabulary(self.store.db)
        return basic_cleanup.Vocabulary(
            terms=list(dict.fromkeys(terms.values())),
            substitutions=dict(terms),
            sources={v: "correction memory" for v in terms.values()},
        )

    def reconcile(
        self, document: dict, verdicts: dict[str, dict], memory: dict[str, dict], *, now: datetime
    ) -> dict:
        return review.reconcile(document, verdicts, memory, now=now)


class ArbiterLike(Protocol):
    """What the step needs of an arbiter (corpuslm.arbiter.Arbiter)."""

    def fingerprint(self) -> dict: ...

    def run(
        self, document: dict, *, source_id: str, protected: list[str], memory: dict
    ) -> dict: ...


@dataclass(slots=True)
class SpendCapStop:
    """The spend cap that stopped this run, if one did. Shared by every source of a run."""

    cap: str | None = None

    def stop(self, cap: str, report: Report) -> None:
        """A spend cap was reached: no further billed call in this run, and the report names
        the cap."""
        self.cap = cap
        report.stopped_by = cap

    def note(self) -> str:
        which = "per-day" if self.cap == llm.DAY_CAP else "per-run"
        return f"budget: the {which} spend cap ({self.cap}) was reached earlier in this run"


def _never_declined(source_id: str) -> str | None:
    return None


def _no_arbiter() -> tuple[ArbiterLike | None, str | None]:
    return None, None


@dataclass(frozen=True, slots=True)
class AiCleanupSetup:
    """The collaborators of the AI cleanup step.

    `client` is called when a model is first needed (a client opens nothing until then).
    `declined(source)` is the tool's own reason to leave a source without the step (a corpus
    that turns it off), or None. `arbiter()` is the arbiter and why it is off.
    """

    chain: Chain
    scheme: SpeakerScheme
    config: dict
    client: Callable[[], llm.Client]
    spend_cap_stop: SpendCapStop
    review: ReviewHooks = field(default_factory=NoReview)
    prompt_directories: Sequence[Path] | None = None
    declined: Callable[[str], str | None] = _never_declined
    arbiter: Callable[[], tuple[ArbiterLike | None, str | None]] = _no_arbiter
    rebuild_stale: bool = False
    clock: clock_module.Clock = clock_module.utc_now

    @property
    def node(self) -> str:
        return self.chain.kind.shape.ai_cleanup

    def prompt(self) -> prompts_module.Prompt:
        return prompts_module.load(self.scheme.prompt_id, directories=self.prompt_directories)

    def settings(self) -> dict:
        section = self.config.get("cleanup") or {}
        return {
            "model": section.get("model", ""),
            "batch_tokens": int(section.get("batch_tokens", 6000)),
            "overlap_segments": int(section.get("overlap_segments", 4)),
            "max_new_ambiguities": int(section.get("max_new_ambiguities", 40)),
        }

    def off_reason(self, source_id: str) -> str | None:
        """Why the step does not run for this source, or None. On by default."""
        reason = self.declined(source_id)
        if reason:
            return reason
        if not self.config.get("cleanup"):
            return "[cleanup] is absent from config.toml"
        if self.spend_cap_stop.cap:
            return self.spend_cap_stop.note()
        return None

    def inputs(self, source_id: str, spell_fix_sha256: str) -> dict:
        """The hashes this build depends on."""
        shape = self.chain.kind.shape
        settings = self.settings()
        verdicts = self.review.verdicts(source_id)
        memory = self.review.corrections()
        arbiter, _ = self.arbiter()
        return {
            shape.spell_fix: spell_fix_sha256,
            "prompt": self.prompt().sha256,
            "model": settings["model"],
            "batch_tokens": settings["batch_tokens"],
            "overlap_segments": settings["overlap_segments"],
            shape.step_key: shape.ai_cleanup_step,
            "verdicts": canonical_sha256({k: v["verdict"] for k, v in verdicts.items()}),
            "memory": canonical_sha256({k: v["verdict"] for k, v in memory.items()}),
            **({"arbiter": canonical_sha256(arbiter.fingerprint())} if arbiter else {}),
        }


def _run_arbiter(
    ai_cleanup_setup: AiCleanupSetup,
    source_id: str,
    document: dict,
    terms: list[str],
    memory: dict,
    report: Report,
) -> dict:
    arbiter, reason = ai_cleanup_setup.arbiter()
    if arbiter is None:
        if ai_cleanup_setup.config.get("arbiter") is not None:
            report.notes["arbiter"] = f"skipped: {reason}"
        return {}
    if ai_cleanup_setup.spend_cap_stop.cap:
        stats = {"error": ai_cleanup_setup.spend_cap_stop.note(), "spend_cap_reached": True}
        report.count("arbiter_spend_cap")
        report.errors[source_id] = f"arbiter: {stats['error']}"
        return stats
    stats = arbiter.run(document, source_id=source_id, protected=terms, memory=memory)
    for key in ("asked", "applied", "overrode", "deferred", "bad"):
        if stats[key]:
            report.count(f"arbiter_{key}", stats[key])
    if stats["spend_cap_reached"]:
        ai_cleanup_setup.spend_cap_stop.stop(stats.get("cap", llm.RUN_CAP), report)
        report.count("arbiter_spend_cap")
    if stats["error"]:
        report.errors[source_id] = f"arbiter: {stats['error']}"
    return stats


def build_ai_cleanup(
    ai_cleanup_setup: AiCleanupSetup, source_id: str, spell_fix_sha256: str, report: Report
) -> str | None:
    """The AI cleanup of one source over its spell-fix artifact. Returns the artifact's
    sha256, or None when the step did not produce one (skipped, failed, stopped by a spend cap)."""
    chain = ai_cleanup_setup.chain
    shape = chain.kind.shape
    node = shape.ai_cleanup
    prompt = ai_cleanup_setup.prompt()
    section = ai_cleanup_setup.config.get("cleanup") or {}
    settings = ai_cleanup_setup.settings()
    batch_tokens = settings["batch_tokens"]
    overlap = settings["overlap_segments"]
    max_new = settings["max_new_ambiguities"]
    verdicts = ai_cleanup_setup.review.verdicts(source_id)
    memory = ai_cleanup_setup.review.corrections()
    inputs = ai_cleanup_setup.inputs(source_id, spell_fix_sha256)
    path = chain.path(source_id, node)
    store = chain.store
    row = store.artifact(source_id, node)
    if row and row["state"] == "built" and loads(row["inputs"], {}) == inputs and path.exists():
        return row["sha256"]

    def put(
        *,
        path: Path | None,
        sha256: str,
        inputs: dict,
        provenance: dict,
        state: str = "built",
    ) -> None:
        record(
            store,
            source_id,
            node,
            root=chain.root,
            path=path,
            sha256=sha256,
            inputs=inputs,
            provenance=provenance,
            created_by=chain.created_by,
            state=state,
        )

    replay: dict[int, str] | None = None  # batch -> payload sha of the call that built it
    held: dict = {}  # CONFIG inputs kept from the held build
    old_provenance_record: dict = {}
    if row and path.exists() and row["state"] in ("built", "stale"):
        old_inputs = loads(row["inputs"], {})
        changed = {k for k, v in inputs.items() if old_inputs.get(k) != v}
        if (
            changed & CONFIG_INPUTS
            and not ai_cleanup_setup.rebuild_stale
            and not (changed - CONFIG_INPUTS - PATCH_INPUTS)
        ):
            # A config change applies forward only: the build keeps the prompt and model it was
            # made with and stays `built`, never rebuilt on its own, until `rebuild_stale` or a
            # refetch. A build an older rule marked stale for config is set back to built.
            old_provenance_record = loads(row["provenance"], {})
            if row["state"] == "stale" and str(
                old_provenance_record.get("stale_reason", "")
            ).startswith("config:"):
                old_provenance_record = {
                    k: v
                    for k, v in old_provenance_record.items()
                    if k not in ("stale_reason", "stale_at")
                }
                put(
                    path=path,
                    sha256=row["sha256"],
                    inputs=old_inputs,
                    provenance=old_provenance_record,
                )
            if not (changed & PATCH_INPUTS):
                return row["sha256"]
            # A verdict or a memory rule landed on a held build: replay the calls that built it
            # from the cache and patch again. The model is never asked with a prompt the owner
            # has not approved.
            replay = {
                int(c["batch"]): str(c.get("payload_sha256", ""))
                for c in old_provenance_record.get("calls", [])
            }
            held = {k: old_inputs.get(k) for k in CONFIG_INPUTS}
            if not replay:
                report.count(f"{node}_held")
                report.errors[source_id] = f"{node}: held build has no recorded calls to replay"
                return row["sha256"]
            batch_tokens = int(held.get("batch_tokens") or batch_tokens)
            overlap = int(held.get("overlap_segments") or overlap)
    reason = ai_cleanup_setup.off_reason(source_id)
    if reason:
        state = "failed" if reason.startswith("budget") else "skipped"
        put(
            path=None,
            sha256="",
            inputs=inputs,
            state=state,
            provenance={"reason": reason, **({"transient": True} if state == "failed" else {})},
        )
        if state == "failed":
            report.count(f"{node}_spend_cap")
            report.errors[source_id] = f"{node}: {reason}"
        return None
    document = chain.load(source_id, shape.spell_fix)
    if ai_cleanup_setup.scheme.fixed_labels:
        ai_cleanup.adopt_fixed_labels(document)
    # Source names only in the prompt: the correction memory's terms are already applied by
    # protected vocabulary and marked protected in the segments, so apply() keeps them; leaving
    # them out of the prompt keeps every batch payload stable and the cache warm (a new verdict
    # must not re-bill the corpus).
    terms = sorted(chain.kind.vocabulary(source_id).terms)
    protected = ", ".join(terms) or "(none)"
    scheme = ai_cleanup_setup.scheme
    batches_to_run = ai_cleanup.batches(
        document, batch_tokens=batch_tokens, overlap_segments=overlap
    )
    bindings: list[dict] = []
    next_key = 1
    totals = {
        "batches": len(batches_to_run),
        "edits": 0,
        "protected_violations": 0,
        "roles": 0,
        "ambiguities": 0,
        "dropped_ambiguities": 0,
        "dropped_bindings": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "cached": 0,
        "cost_unknown": 0,
    }
    first_provenance_record: dict = {}
    calls: list[dict] = []
    client = None
    for batch in batches_to_run:
        variables = {
            "source_label": shape.source_label,
            "protected": protected,
            "roles": ai_cleanup.role_summary(
                document, upto=batch.start, bindings=bindings, scheme=scheme
            ),
            "batch": ai_cleanup.render(document, batch, scheme=scheme),
        }
        tag = f"{source_id}-{batch.tag_suffix}"

        def check(obj, batch=batch, current=document):
            ai_cleanup.check_bounds(batch, obj, scheme=scheme, document=current)

        try:
            client = client or ai_cleanup_setup.client()
            if replay is not None:
                reply = client.replay(
                    node, prompt, replay.get(batch.number, ""), tag=tag, check=check
                )
            else:
                reply = client.complete(node, prompt, variables, tag=tag, check=check)
            document, stats = ai_cleanup.apply(
                document, batch, reply.parsed, next_key=next_key, scheme=scheme
            )
        except llm.CacheMiss as e:
            # the held build stays as it is; the verdict waits in the database
            report.count(f"{node}_held")
            report.errors[source_id] = f"{node}: {e}"
            return row["sha256"] if row else None
        except llm.NotConfigured as e:
            put(path=None, sha256="", inputs=inputs, state="skipped", provenance={"reason": str(e)})
            return None
        except llm.SpendCapReached as e:
            ai_cleanup_setup.spend_cap_stop.stop(e.cap, report)
            put(
                path=None,
                sha256="",
                inputs=inputs,
                state="failed",
                provenance={
                    "reason": f"budget: {e}",
                    "transient": True,
                    "batches_done": batch.number,
                },
            )
            report.count(f"{node}_spend_cap")
            report.errors[source_id] = f"{node}: budget: {e}"
            return None
        except llm.VendorError as e:
            # transient by definition: recorded, the run goes on
            put(
                path=None,
                sha256="",
                inputs=inputs,
                state="failed",
                provenance={"reason": f"vendor: {e}", "transient": True, "batch": batch.number},
            )
            report.count(f"{node}_failed")
            report.errors[source_id] = f"{node} batch {batch.number}: vendor: {e}"
            return None
        except (llm.BadReply, llm.TruncatedReply, ai_cleanup.BatchError) as e:
            put(
                path=None,
                sha256="",
                inputs=inputs,
                state="failed",
                provenance={"reason": f"bad_reply: {e}", "transient": True, "batch": batch.number},
            )
            report.count(f"{node}_failed")
            report.errors[source_id] = f"{node} batch {batch.number}: {e}"
            return None
        next_key = stats["next_key"]
        bindings += stats["bindings"]
        for key in (
            "edits",
            "protected_violations",
            "roles",
            "ambiguities",
            "dropped_ambiguities",
            "dropped_bindings",
        ):
            totals[key] += stats[key]
        provenance_record = reply.provenance()
        first_provenance_record = first_provenance_record or provenance_record
        calls.append(
            {
                "batch": batch.number,
                "payload_sha256": provenance_record["payload_sha256"],
                "requested_at": provenance_record["requested_at"],
                "cached": provenance_record["cached"],
                "tokens_in": provenance_record["tokens_in"],
                "tokens_out": provenance_record["tokens_out"],
                "cost_usd": provenance_record["cost_usd"],
                "attempts": provenance_record["attempts"],
            }
        )
        totals["tokens_in"] += provenance_record["tokens_in"]
        totals["tokens_out"] += provenance_record["tokens_out"]
        totals["cached"] += 1 if provenance_record["cached"] else 0
        if provenance_record["cost_usd"] is None and not provenance_record["cached"]:
            totals["cost_unknown"] += 1
        else:
            totals["cost_usd"] = round(
                totals["cost_usd"] + (provenance_record["cost_usd"] or 0.0), 6
            )
    # The correction memory's term verdicts, as a deterministic patch on the model's output; then
    # the owner's verdicts and the memory's ordinary-correction rules, before the spend cap and
    # before anything reaches the queue.
    document, memory_stats = basic_cleanup.apply_correction_memory(
        document, ai_cleanup_setup.review.memory_vocabulary()
    )
    carried = ai_cleanup_setup.review.reconcile(
        document, verdicts, memory, now=ai_cleanup_setup.clock()
    )
    dropped_after_memory = ai_cleanup.drop_unanchored(document)
    arbiter_stats = _run_arbiter(ai_cleanup_setup, source_id, document, terms, memory, report)
    if arbiter_stats.get("error"):
        # asked again on the next run: the recorded input no longer matches
        inputs = inputs | {"arbiter": "incomplete"}
    capped = ai_cleanup.cap_ambiguities(document, max_new=max_new)
    document["state"] = "LLM-CLEANED"
    if not scheme.fixed_labels:
        document["speaker_source"] = "inferred"
    prompt_keys = ("prompt_id", "prompt_version", "prompt_sha256", "model", "profile")
    lineage = {k: v for k, v in first_provenance_record.items() if k in prompt_keys}
    if held:
        # the replies came from the held build: its prompt and model, not today's
        lineage = {k: old_provenance_record.get(k, lineage.get(k)) for k in prompt_keys}
    document.setdefault("passes", {})["pass3"] = {
        "status": "ran",
        "batches": len(batches_to_run),
        "edits": totals["edits"],
        "ambiguities": totals["ambiguities"],
        "ambiguities_capped": capped,
        "prompt_id": lineage.get("prompt_id", prompt.id),
        "prompt_version": lineage.get("prompt_version", prompt.version),
        "model": lineage.get("model") or section["model"],
        "bindings": bindings,
        "unplaced": carried["unplaced_items"],
    }
    sha256 = write_if_changed(path, chain.dumps(document))
    # The artifact and the model's suggestions it holds are recorded together, so a rebuilt
    # artifact replaces the suggestions of the one before it.
    with store.transaction():
        put(
            path=path,
            sha256=sha256,
            inputs=inputs | held,
            provenance={
                **lineage,
                **({"replayed": True} if held else {}),
                **totals,
                "ambiguities_new": totals["ambiguities"]
                - capped
                - carried["carried"]
                - carried["auto_resolved"],
                "ambiguities_capped": capped,
                "verdicts_carried": carried["carried"],
                "verdicts_unplaced": carried["unplaced"],
                "verdicts_already_applied": carried["already_applied"],
                "memory_substitutions": memory_stats["applied"],
                "dropped_after_memory": dropped_after_memory,
                **({"arbiter": arbiter_stats} if arbiter_stats else {}),
                "unplaced": carried["unplaced_items"],
                "auto_resolved": carried["auto_resolved"],
                "bindings": bindings,
                "calls": calls,
            },
        )
        model_suggestions.record(
            store, source_id, document, artifact_sha256=sha256, created_by=chain.created_by
        )
    report.count(f"{node}_replayed" if held else node)
    return sha256


# ---- what a run would send ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Estimate:
    """What the step would do for a set of sources, from the documents and the reply cache alone."""

    sources: int  # sources whose step is not current
    from_cache: int  # of those, the sources every batch of which the cache already holds
    batches: int  # the model calls that would be billed: batches the cache does not hold
    tokens_in: int  # of those calls
    tokens_out: int
    usd: float | None  # None when the model has no price row; 0.0 when nothing would be billed
    model: str


def _walk_cache(ai_cleanup_setup: AiCleanupSetup, source_id: str, document: dict) -> list[str]:
    """The rendered text of each batch the cache does not hold (empty: the cache holds them all).

    Walks the source's batches as `build_ai_cleanup` does, applying each cached reply so the
    next batch's payload is the one a run would send. From the first batch the cache lacks, the
    rest are counted as billed: their payloads depend on a reply nobody has seen."""
    shape = ai_cleanup_setup.chain.kind.shape
    settings = ai_cleanup_setup.settings()
    scheme = ai_cleanup_setup.scheme
    prompt = ai_cleanup_setup.prompt()
    batches_to_run = ai_cleanup.batches(
        document,
        batch_tokens=settings["batch_tokens"],
        overlap_segments=settings["overlap_segments"],
    )
    protected = (
        ", ".join(sorted(ai_cleanup_setup.chain.kind.vocabulary(source_id).terms)) or "(none)"
    )
    client = ai_cleanup_setup.client()
    bindings: list[dict] = []
    next_key = 1
    for number, batch in enumerate(batches_to_run):
        variables = {
            "source_label": shape.source_label,
            "protected": protected,
            "roles": ai_cleanup.role_summary(
                document, upto=batch.start, bindings=bindings, scheme=scheme
            ),
            "batch": ai_cleanup.render(document, batch, scheme=scheme),
        }

        def check(obj, batch=batch, current=document):
            ai_cleanup.check_bounds(batch, obj, scheme=scheme, document=current)

        reply = client.lookup(ai_cleanup_setup.node, prompt, variables, check=check)
        if reply is None:
            return [
                ai_cleanup.render(document, later, scheme=scheme)
                for later in batches_to_run[number:]
            ]
        document, stats = ai_cleanup.apply(
            document, batch, reply.parsed, next_key=next_key, scheme=scheme
        )
        next_key = stats["next_key"]
        bindings += stats["bindings"]
    return []


def estimate(ai_cleanup_setup: AiCleanupSetup, spell_fix_sha256s: dict[str, str]) -> Estimate:
    """What the step would bill for `spell_fix_shas` (source -> spell-fix sha), skipping every
    source whose step is current. A source whose batches the reply cache holds is rebuilt from the
    cache (a landed verdict or memory rule changed its inputs): it counts under `from_cache` and
    bills nothing. Calls no model, prints no text and writes nothing."""
    shape = ai_cleanup_setup.chain.kind.shape
    settings = ai_cleanup_setup.settings()
    sources = from_cache = batches = tokens_in = 0
    for source, sha256 in sorted(spell_fix_sha256s.items()):
        if is_current(
            ai_cleanup_setup.chain.store,
            source,
            shape.ai_cleanup,
            ai_cleanup_setup.inputs(source, sha256),
        ):
            continue
        if ai_cleanup_setup.off_reason(source):
            continue
        document = ai_cleanup_setup.chain.load(source, shape.spell_fix)
        if ai_cleanup_setup.scheme.fixed_labels:
            ai_cleanup.adopt_fixed_labels(document)
        missed = _walk_cache(ai_cleanup_setup, source, document)
        sources += 1
        if not missed:
            from_cache += 1
            continue
        batches += len(missed)
        terms = ", ".join(sorted(ai_cleanup_setup.chain.kind.vocabulary(source).terms))
        tokens_in += sum(
            ai_cleanup.estimate_tokens(ai_cleanup_setup.prompt().body + terms + text)
            for text in missed
        )
    tokens_out = int(tokens_in * ASSUMED_OUTPUT_PER_INPUT)
    return Estimate(
        sources,
        from_cache,
        batches,
        tokens_in,
        tokens_out,
        llm.price_usd(settings["model"], tokens_in, tokens_out),
        settings["model"],
    )
