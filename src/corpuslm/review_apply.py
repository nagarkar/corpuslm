"""Review apply: lands the answers the review page saved, for every source kind, with no model.

The page keeps each tap as a `review_answers` row (corpuslm.review_server). `apply` reads those
rows through the Store, maps them to per-key verdicts by recomputing the page's cards
(`review_page.decisions_to_verdicts_by_kind`, each source read with its own kind), lands each as a
verdict row with provenance (`review_page.land_page_verdicts`) and deletes the answers it landed,
in one transaction, so the table only ever holds answers not yet landed. A tool that can rebuild
what a speaker answer changes passes `rebuilders` (`corpuslm.review_rebuilders` finds the ones
registered in this environment): a speaker answer then rebuilds that source's speaker map at once
(no model); every other answer waits for the tool's next AI cleanup run, and the report says how
many sources wait. A source kind whose answers need such a rebuilder (`ReviewKind.rebuilt_by`) and
has none here lands nothing: its answers stay where they are and the report counts them per kind,
so the verb names the tool's own verb to run.

Never imports: cli, clikit, any model transport.

Decisions:
- The answers table holds only answers not yet landed: a landed answer, an answer that was undone
  (empty) and an answer whose verdict was already in force are deleted; an answer for a card the
  page no longer shows (resolved, or gone) stays and is counted as unknown. Enforced by:
  tests/test_review_apply.py::test_landed_answers_are_deleted_and_unknown_ones_stay.
- A name the owner already picked for a speaker is never overwritten: the page shows that card
  resolved, so its answer lands nothing. Enforced by:
  tests/test_review_apply.py::test_a_name_the_owner_picked_is_never_overwritten.
- Landing and deleting happen in one transaction; a failed landing leaves every answer where it
  was. Enforced by: tests/test_review_apply.py::test_a_failed_landing_leaves_every_answer.
- `--dry-run` counts and writes nothing. Enforced by:
  tests/test_review_apply.py::test_a_dry_run_counts_and_writes_nothing.
- A kind that names a rebuilding verb (`ReviewKind.rebuilt_by`) lands its answers only when its
  rebuilder is available in this process; otherwise its answers stay, its verdicts count as left
  unlanded, and a card that spans kinds keeps its answer too. Enforced by:
  tests/test_review_apply.py::test_a_kind_without_its_rebuilder_leaves_its_answers_unlanded.
- A source kind's `stale_nodes` are marked stale for each source a verdict landed on, so the
  tool's next run rebuilds them. Enforced by:
  tests/test_review_apply.py::test_a_landed_answer_marks_the_kinds_stale_nodes.

Terms:
- **unknown card**: an answer whose card the page no longer shows.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from . import review_rebuilders
from .review_page import (
    KindSetup,
    decisions_document,
    decisions_to_verdicts_by_kind,
    land_page_verdicts,
)
from .store import Store

Rebuilder = review_rebuilders.Rebuilder


@dataclass(frozen=True, slots=True)
class ApplyReport:
    answers_saved: int = 0
    verdicts_to_land: int = 0
    verdicts_landed: int = 0
    answers_cleared: int = 0
    unknown_cards: int = 0
    speaker_maps_rebuilt: int = 0
    sources_waiting_for_ai_cleanup: int = 0
    corpora: int = 0
    landed_keys: list[str] = field(default_factory=list)
    # Verdicts a kind's answers would land that were left, by kind: the kind has no rebuilder here.
    verdicts_left_unlanded: dict[str, int] = field(default_factory=dict)


def _kind_of(store: Store, source_id: str) -> str:
    return (store.source(source_id) or {}).get("kind", "")


def _already_in_force(store: Store, verdict: dict) -> bool:
    current = store.current_review_verdicts(verdict["video_id"]).get(verdict["key"])
    return bool(current and current["verdict"] == verdict["verdict"])


def apply(
    db,
    kinds: Mapping[str, KindSetup],
    *,
    created_by: str,
    only_kinds: Collection[str] | None = None,
    dry_run: bool = False,
    rebuilders: Mapping[str, Rebuilder] | None = None,
) -> ApplyReport:
    """Land the saved answers of the corpora that have a source of one of `only_kinds` (default:
    every kind in `kinds`). Returns counts only."""
    store = Store(db)
    allowed = {name for name in kinds if only_kinds is None or name in set(only_kinds)}
    # Kinds whose answers need the tool's rebuilder, and it is not here: nothing of theirs lands.
    needing = {
        name for name in allowed if kinds[name].kind.rebuilt_by and name not in (rebuilders or {})
    }
    landing = allowed - needing
    left_unlanded: dict[str, int] = {}
    totals = {
        "answers_saved": 0,
        "verdicts_to_land": 0,
        "verdicts_landed": 0,
        "answers_cleared": 0,
        "unknown_cards": 0,
        "corpora": 0,
    }
    landed_keys: list[str] = []
    speaker_sources: dict[str, set[str]] = {}
    waiting: set[str] = set()
    touched: set[str] = set()
    for corpus_id in store.review_answer_counts():
        corpus = store.corpus(corpus_id)
        if corpus is None or not any(
            member["kind"] in allowed for member in store.member_sources(corpus_id)
        ):
            continue
        decisions = decisions_document(store, corpus_id)
        # Every kind's sources are read, so a card that spans kinds is whole; only the verdicts of
        # the allowed kinds land, and a card with a source of another kind keeps its answer for
        # that kind's own apply.
        everything, unknown = decisions_to_verdicts_by_kind(db, kinds, corpus["name"], decisions)
        verdicts = [
            verdict for verdict in everything if _kind_of(store, verdict["video_id"]) in landing
        ]
        held_back = {
            verdict["card_id"]
            for verdict in everything
            if _kind_of(store, verdict["video_id"]) not in landing
            and not _already_in_force(store, verdict)
        }
        for verdict in everything:
            kind_name = _kind_of(store, verdict["video_id"])
            if kind_name in needing and not _already_in_force(store, verdict):
                left_unlanded[kind_name] = left_unlanded.get(kind_name, 0) + 1
        fresh = [verdict for verdict in verdicts if not _already_in_force(store, verdict)]
        landed_cards = {verdict["card_id"] for verdict in verdicts} - held_back
        empty_cards = {
            card_id
            for card_id, decision in decisions["decisions"].items()
            if not decision["verdict"].strip()
        }
        totals["corpora"] += 1
        totals["answers_saved"] += sum(
            1 for decision in decisions["decisions"].values() if decision["verdict"].strip()
        )
        totals["unknown_cards"] += len(unknown)
        totals["verdicts_to_land"] += len(fresh)
        for verdict in fresh:
            kind_name = _kind_of(store, verdict["video_id"])
            touched.add(verdict["video_id"])
            if verdict["kind"] == "speaker":
                speaker_sources.setdefault(kind_name, set()).add(verdict["video_id"])
            else:
                waiting.add(verdict["video_id"])
        if dry_run:
            continue
        with store.transaction():
            landed_keys += land_page_verdicts(db, fresh, created_by=created_by)
            for card_id in sorted(landed_cards | empty_cards):
                if store.delete_review_answer(corpus_id, card_id):
                    totals["answers_cleared"] += 1
        totals["verdicts_landed"] += len(fresh)
    rebuilt = 0
    if not dry_run:
        for source_id in sorted(touched):
            setup = kinds.get(_kind_of(store, source_id))
            for node in setup.kind.stale_nodes if setup else ():
                store.mark_stale(source_id, node)
        for kind_name, sources in sorted(speaker_sources.items()):
            rebuild = (rebuilders or {}).get(kind_name)
            if rebuild:
                rebuilt += rebuild(sources)
    return ApplyReport(
        **totals,
        speaker_maps_rebuilt=rebuilt,
        sources_waiting_for_ai_cleanup=len(waiting),
        landed_keys=landed_keys,
        verdicts_left_unlanded=left_unlanded,
    )


__all__ = ["ApplyReport", "Rebuilder", "apply"]
