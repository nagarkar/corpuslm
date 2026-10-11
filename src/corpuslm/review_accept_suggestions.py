"""Accept suggestions: answer open review cards with the cleanup model's recorded suggestion.

For the older recordings of one source kind, the owner may be content to take the model's own top
suggestion (recorded in the database by corpuslm.model_suggestions) for every ambiguity card still
open, and change the few they disagree with. `accept` saves each such answer exactly as the review
page would, through `Store.save_review_answer`, so the owner sees it on the page, filled in, and can
change it. It lands nothing: `review apply` is still the owner's step. It calls no model.

A card is answered only when every source behind it is of the chosen kind and recorded before the
cutoff date, every one of its places has a recorded suggestion for the card's words, and those
suggestions are the same candidate. A card that already holds an answer (even one the owner undid)
is never touched, and a speaker card is never touched.

Never imports: cli, clikit, any model transport.

Decisions:
- An existing answer is never overwritten, an undone one included: the owner's tap, or the
  owner's undoing of one, is theirs. Enforced by:
  tests/test_review_accept_suggestions.py::test_an_existing_answer_is_never_overwritten.
- Only open ambiguity cards are answered; a speaker card, a resolved card and a card the arbiter
  picked for are left alone. Enforced by:
  tests/test_review_accept_suggestions.py::test_speaker_cards_and_resolved_cards_are_never_touched.
- Every source of a card must be of the chosen kind and dated before the cutoff; a source with
  no recording date is not before it. Enforced by:
  tests/test_review_accept_suggestions.py::test_a_card_with_a_source_on_or_after_the_cutoff_is_left_open.
- A card is answered only when each of its places has a recorded suggestion for the card's
  words and they agree; a missing or different one leaves it open, counted. Enforced by:
  tests/test_review_accept_suggestions.py::test_a_card_without_a_recorded_suggestion_or_with_differing_ones_is_left_open.
- The answer is saved through the Store method the review server uses, with the suggestion's own
  spelling. Enforced by:
  tests/test_review_accept_suggestions.py::test_the_answer_equals_the_suggestion_and_shows_on_the_page.
- `--dry-run` counts and writes nothing. Enforced by:
  tests/test_review_accept_suggestions.py::test_a_dry_run_counts_and_writes_nothing.

Terms:
- **open card**: an ambiguity card that is not resolved and that the arbiter has not picked for.
- **cutoff**: the date a source's recording date (`published_at`) must be before.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from .db import now_iso
from .review_page import KindSetup, page_data_by_kind
from .store import Store

CREATED_BY = "corpuslm"


@dataclass(frozen=True, slots=True)
class AcceptReport:
    cards_considered: int = 0
    cards_to_answer: int = 0
    cards_answered: int = 0
    cards_skipped_already_answered: int = 0
    cards_skipped_a_source_not_before_the_cutoff: int = 0
    cards_skipped_no_recorded_suggestion: int = 0
    cards_skipped_suggestions_differ: int = 0
    cards_left_alone_with_a_source_of_another_kind: int = 0


def parse_cutoff(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"--before must be a date like 2026-07-01, not {text!r}") from error


def recorded_before(source: dict, cutoff: date) -> bool:
    """Whether the source's recording date is before `cutoff`; a source with no usable date is
    not."""
    try:
        return date.fromisoformat(str(source.get("published_at") or "")[:10]) < cutoff
    except ValueError:
        return False


def accept(
    db,
    kinds: Mapping[str, KindSetup],
    *,
    kind: str,
    before: date,
    dry_run: bool = False,
) -> AcceptReport:
    """Answer the open ambiguity cards of sources of `kind` recorded before `before` with the
    model's recorded suggestion. Returns counts only."""
    store = Store(db)
    sources: dict[str, dict] = {}
    suggestions: dict[str, dict[str, dict]] = {}
    counts = dict.fromkeys(AcceptReport.__slots__, 0)  # type: ignore[attr-defined]
    stamp = now_iso()
    for corpus_id in store.corpus_ids(kind):
        corpus = store.corpus(corpus_id)
        if corpus is None:
            continue
        data = page_data_by_kind(db, kinds, corpus["name"])
        for card in data["cards"]:
            if card["kind"] != "ambiguity" or card["resolved"] or card.get("jev"):
                continue
            places = [item["video_id"] for item in card["items"]]
            for source_id in places:
                if source_id not in sources:
                    sources[source_id] = store.source(source_id) or {}
            if any(sources[source_id].get("kind") != kind for source_id in places):
                counts["cards_left_alone_with_a_source_of_another_kind"] += 1
                continue
            counts["cards_considered"] += 1
            if store.review_answer(corpus_id, card["id"]) is not None:
                counts["cards_skipped_already_answered"] += 1
                continue
            if not all(recorded_before(sources[source_id], before) for source_id in places):
                counts["cards_skipped_a_source_not_before_the_cutoff"] += 1
                continue
            picked_candidates: set[str] = set()
            spelling = ""
            missing = False
            for item in card["items"]:
                held = suggestions.get(item["video_id"])
                if held is None:
                    held = suggestions[item["video_id"]] = store.model_suggestions(item["video_id"])
                found = held.get(item["key"])
                if found is None or found["span_norm"] != card["span_norm"]:
                    missing = True
                    break
                picked_candidates.add(found["suggestion"])
                spelling = spelling or found["suggestion"]
            if missing:
                counts["cards_skipped_no_recorded_suggestion"] += 1
                continue
            if len(picked_candidates) > 1:
                counts["cards_skipped_suggestions_differ"] += 1
                continue
            counts["cards_to_answer"] += 1
            if dry_run:
                continue
            store.save_review_answer(corpus_id, card["id"], spelling, stamp, created_by=CREATED_BY)
            counts["cards_answered"] += 1
    return AcceptReport(**counts)


__all__ = ["AcceptReport", "accept", "parse_cutoff", "recorded_before"]
