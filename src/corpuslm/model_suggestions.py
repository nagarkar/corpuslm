"""Model suggestions: the AI cleanup's top suggestion for every ambiguity, kept in the database.

The AI cleanup (corpuslm.ai_cleanup_chain) returns, per ambiguity, the words as heard, its
candidates, the pick it made and a probability per candidate. Those lived only inside each
source's AI cleanup artifact file. This module copies them into the `model_suggestions` table
(through the Store, one row per source and review key, its candidates beside it), so a later step
can use the model's suggestion without rerunning the model: faster and free.

Two ways in, both with no model call. `record` is the builder's: whenever the AI cleanup step
builds or rebuilds a source's artifact, the rows of that source are replaced by the new
document's. `backfill` is the verb's (`corpuslm suggestions record`): it reads every source's
existing AI cleanup artifact file and writes the same rows.

Never imports: cli, clikit, db, any model transport.

Decisions:
- The suggestion is the model's own pick, never the owner's or the arbiter's resolution: the
  document keeps `pick` untouched when a verdict lands. Enforced by:
  tests/test_model_suggestions.py::test_the_suggestion_is_the_models_pick_not_the_resolution.
- A suggestion's probability is the model's probability for its pick; a reply from the first
  prompt gave one confidence and no probabilities, so its probability is left empty rather than
  guessed. Enforced by:
  tests/test_model_suggestions.py::test_a_reply_without_probabilities_leaves_them_empty.
- A rebuilt artifact replaces the rows of the one before it, and rows that already come from the
  artifact are not written again (a second backfill writes nothing). Enforced by:
  tests/test_model_suggestions.py::test_a_rebuilt_artifact_replaces_its_rows_and_a_second_backfill_writes_nothing.
- The backfill reads a file only when its bytes are the artifact the database recorded; a file
  changed since is counted and left alone. Enforced by:
  tests/test_model_suggestions.py::test_a_file_changed_since_it_was_built_is_counted_and_not_read.
- A dry run reads and counts, and writes nothing. Enforced by:
  tests/test_model_suggestions.py::test_a_dry_run_counts_and_writes_nothing.
- The builder records the suggestions with the artifact in one transaction, so the rows and the
  artifact never disagree. Enforced by:
  tests/test_ai_cleanup_chain.py::test_building_the_ai_cleanup_records_its_suggestions.

Terms:
- **suggestion**: the pick the AI cleanup made for an ambiguity, recorded with its candidates
  (the artifact calls the same choice `pick`).
- **review key**: `amb-NNNN`, the ambiguity's name inside one source.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import transcript
from .identity import normalize_span
from .store import Store

if TYPE_CHECKING:
    from .review_page import KindSetup


def suggestions_of(ambiguities: list[dict]) -> list[dict]:
    """The Store's rows (`Store.replace_model_suggestions`) for a document's ambiguities, in the
    document's order. A probability is kept only when the model gave one per candidate."""
    out = []
    for item in ambiguities:
        candidates = [str(c) for c in item["candidates"]]
        given = item.get("probabilities")
        probabilities: list[float | None]
        if given is not None and len(given) == len(candidates):
            probabilities = [float(p) for p in given]
        else:
            probabilities = [None] * len(candidates)
        pick = item["pick"]
        probability = probabilities[candidates.index(pick)] if pick in candidates else None
        out.append(
            {
                "key": item["key"],
                "span_norm": normalize_span(item["span"]),
                "suggestion": pick,
                "suggestion_probability": probability,
                "candidates": candidates,
                "probabilities": probabilities,
            }
        )
    return out


def model_of(document: dict) -> str:
    """The model that cleaned the document (`passes.pass3.model`), '' when it records none."""
    return str(((document.get("passes") or {}).get("pass3") or {}).get("model") or "")


def record(
    store: Store, source_id: str, document: dict, *, artifact_sha256: str, created_by: str
) -> bool:
    """Make the source's recorded suggestions those of `document`, the AI cleanup artifact
    with `artifact_sha256`. False when the rows already come from that artifact."""
    return store.replace_model_suggestions(
        source_id,
        suggestions_of(document.get("ambiguities") or []),
        artifact_sha256=artifact_sha256,
        model=model_of(document),
        created_by=created_by,
    )


@dataclass(frozen=True, slots=True)
class BackfillReport:
    sources_checked: int = 0
    sources_without_ai_cleanup: int = 0
    sources_file_changed_since_built: int = 0
    sources_unreadable: int = 0  # not valid as a transcript, or its suggestions cannot be kept
    sources_read: int = 0
    sources_already_recorded: int = 0
    sources_to_record: int = 0
    sources_recorded: int = 0
    suggestions_to_record: int = 0
    suggestions_recorded: int = 0
    suggestions_to_record_with_probability: int = 0


def backfill(
    store: Store,
    kinds: Mapping[str, KindSetup],
    *,
    created_by: str,
    dry_run: bool = False,
) -> BackfillReport:
    """Record the suggestions of every source of the kinds in `kinds` from its existing model
    cleanup artifact (the file beside the source's other documents, named by the artifact's
    recorded path), with no model call. Counts only: no document text is returned."""
    counts = dict.fromkeys(BackfillReport.__slots__, 0)  # type: ignore[attr-defined]
    for kind_name, setup in kinds.items():
        node = setup.kind.ai_cleanup_node
        for source_id in store.source_ids(kind_name, include_ignored=False):
            counts["sources_checked"] += 1
            row = store.artifact(source_id, node)
            if row is None or row["state"] not in ("built", "stale") or not row["path"]:
                counts["sources_without_ai_cleanup"] += 1
                continue
            path = Path(setup.sources_directory) / source_id / Path(row["path"]).name
            try:
                data = path.read_bytes()
            except OSError:
                counts["sources_without_ai_cleanup"] += 1
                continue
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                counts["sources_file_changed_since_built"] += 1
                continue
            try:
                document = transcript.loads(
                    data.decode("utf-8"), refined_schema=setup.kind.refined_schema
                )
            except (ValueError, UnicodeDecodeError):
                counts["sources_unreadable"] += 1
                continue
            counts["sources_read"] += 1
            ambiguities = document.get("ambiguities") or []
            held = store.model_suggestions(source_id)
            if _already_recorded(held, ambiguities, row["sha256"]):
                counts["sources_already_recorded"] += 1
                continue
            counts["sources_to_record"] += 1
            rows = suggestions_of(ambiguities)
            counts["suggestions_to_record"] += len(rows)
            counts["suggestions_to_record_with_probability"] += sum(
                1 for r in rows if r["suggestion_probability"] is not None
            )
            if dry_run:
                continue
            try:
                record(
                    store, source_id, document, artifact_sha256=row["sha256"], created_by=created_by
                )
            except (sqlite3.IntegrityError, KeyError, ValueError, TypeError):
                # one artifact that cannot be recorded is counted; the others go on
                counts["sources_unreadable"] += 1
                counts["sources_read"] -= 1
                counts["sources_to_record"] -= 1
                counts["suggestions_to_record"] -= len(rows)
                counts["suggestions_to_record_with_probability"] -= sum(
                    1 for r in rows if r["suggestion_probability"] is not None
                )
                continue
            counts["sources_recorded"] += 1
            counts["suggestions_recorded"] += len(rows)
    return BackfillReport(**counts)


def _already_recorded(held: dict[str, dict], ambiguities: list[dict], sha256: str) -> bool:
    if not ambiguities:
        return not held
    return len(held) == len(ambiguities) and all(
        h["artifact_sha256"] == sha256 for h in held.values()
    )


__all__ = ["BackfillReport", "backfill", "model_of", "record", "suggestions_of"]
