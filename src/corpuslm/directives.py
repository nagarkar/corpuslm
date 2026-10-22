"""Directives: what the owner asked for, as claims of kind `directive`, gated and ruled on.

A directive is an instruction or commitment the owner spoke: a claim of kind `directive` in the
shared `claims` table (nagarkar/corpuslm#86), produced by the Bee variant of the claims prompt
(`claims_bee`, corpuslm.claims) in the same model read as the other claims, with two columns of
its own: `addressee` (`computer` when the utterance addresses the wake word "Computer", `self`
for any other imperative) and `heard` (the words as heard, verbatim). Which lines are candidates
depends on the conversation's form (corpuslm.form): in a monologue every imperative or
first-person commitment the owner speaks is one, and Jev gates each ("is this an instruction the
owner wants acted on", corpuslm.judge) at `[claims].directive_threshold`; in company only an
utterance that addresses "Computer" is one, and no gate is needed. A candidate the gate leaves
out stays a claim of its ordinary kind (the reply's `else_kind`).

The owner rules on each directive from the review surface (corpuslm.directives_page): confirm or
decline, an append-only row of `directive_verdicts` keyed by the directive's subject (the source,
the words as heard and the addressee), so a rebuild that extracts the same directive keeps the
ruling and never overwrites it. Moments, concepts and the monologue recipes never read
directives (`Store.claims` leaves them out by default).

Never imports: cli, clikit. Reads the database only through `Store`.

Decisions:
- Addressee is decided by the wake word alone: an utterance addressed to "Computer" is for
  computerlm, whatever it asks; any other imperative is a note to the owner (`self`), listed and
  never acted on. Household members are not addressees. (owner, 2026-10-21)
- Extraction is form-dependent: in a monologue every imperative is a candidate, gated by Jev; in
  company only an addressed utterance is one. A candidate below the threshold keeps its ordinary
  kind. (owner, 2026-10-21) Enforced by:
  tests/test_directives.py::test_a_monologue_candidate_below_the_threshold_keeps_its_ordinary_kind,
  tests/test_directives.py::test_in_company_only_an_addressed_utterance_is_a_directive.
- Without Jev a monologue's candidates stand ungated and the artifact says so. Enforced by:
  tests/test_directives.py::test_without_jev_monologue_candidates_stand_and_the_pass_says_so.
- A rebuild never overwrites the owner's verdict: the verdict is keyed by subject, not by claim
  id. (owner, 2026-10-21) Enforced by:
  tests/test_directives.py::test_a_rebuild_keeps_the_verdict_on_the_same_directive.
- Moments, concept tagging and the corpus document skip kind `directive`. (owner, 2026-10-21)
  Enforced by: tests/test_directives.py::test_the_other_passes_never_see_a_directive.

Terms:
- **directive**: an instruction or commitment the owner spoke; a claim of kind `directive`.
- **addressee**: `computer` (the wake word was spoken) or `self` (a note to the owner).
- **heard**: a directive's words as heard, verbatim.
- **directive gate**: Jev's yes/no on a monologue candidate, applied at the threshold.
- **directive verdict**: the owner's confirm or decline, a row of `directive_verdicts`; the
  newest per (source, subject) is in force.
"""

from __future__ import annotations

from . import judge as judge_module
from .identity import canonical_sha256, normalize_span
from .store import Store

KIND = "directive"
QUESTION = "acted_on"
DEFAULT_THRESHOLD = 0.6
VERDICTS = ("confirm", "decline")
INSTRUCTIONS = (
    "`utterance` is something the owner said while alone, as the recorder heard it, and "
    "`restated` is a model's one-sentence reading of it. Is it an instruction, request or "
    "commitment the owner wants acted on later (something to do, remember, buy, send, book, "
    "look up, or a promise to someone), rather than a thought spoken aloud, a quotation, a "
    "hypothetical, or a remark about the past?"
)
CRITERIA = {
    "true": "the owner wants this done or remembered: an actionable instruction or a commitment",
    "false": "thinking aloud, narration, a quotation, a hypothetical, advice to nobody, or a "
    "remark that merely mentions doing something",
}


def subject_sha256(source_id: str, heard: str, addressee: str | None) -> str:
    """A directive's identity across rebuilds: the source, the words as heard (normalized) and
    the addressee."""
    return canonical_sha256(
        {
            "kind": KIND,
            "source_id": source_id,
            "heard": normalize_span(heard or "") or (heard or ""),
            "addressee": addressee or "",
        }
    )


def gate(
    judge: judge_module.Judge | None,
    claims: list[dict],
    *,
    form: str,
    threshold: float,
    tag_prefix: str,
) -> tuple[list[dict], dict]:
    """Apply the form rule and, in a monologue, Jev's gate to the candidates in `claims`
    (dicts as `Store.replace_claims` takes them, plus `else_kind`). Returns the claims to store
    and counts: `candidates`, `asked`, `directives`, `demoted`, `cost_usd`, `gate`."""
    stats = {
        "candidates": 0,
        "asked": 0,
        "directives": 0,
        "demoted": 0,
        "cost_usd": 0.0,
        "gate": "none",
    }
    out = []
    for position, claim in enumerate(claims):
        if claim.get("kind") != KIND:
            out.append(_without_directive_fields(claim))
            continue
        stats["candidates"] += 1
        keep = True
        if form == "monologue":
            if judge is None:
                stats["gate"] = "off"
            else:
                stats["gate"] = "jev"
                probability, call = judge.yes(
                    {"utterance": claim.get("heard") or "", "restated": claim["text"]},
                    QUESTION,
                    INSTRUCTIONS,
                    CRITERIA,
                    tag=f"{tag_prefix}-d{position}",
                )
                stats["asked"] += 1
                stats["cost_usd"] = round(stats["cost_usd"] + (call["cost_usd"] or 0.0), 6)
                claim = dict(claim) | {"gate_probability": round(probability, 3)}
                keep = probability >= threshold
        else:
            stats["gate"] = "wake word"
            keep = claim.get("addressee") == "computer"
        if keep:
            stats["directives"] += 1
            out.append(_directive(claim))
        else:
            stats["demoted"] += 1
            out.append(_demoted(claim))
    return out, stats


def _without_directive_fields(claim: dict) -> dict:
    return {
        key: value
        for key, value in claim.items()
        if key not in ("addressee", "heard", "else_kind", "gate_probability")
    }


def _directive(claim: dict) -> dict:
    kept = _without_directive_fields(claim)
    return kept | {
        "kind": KIND,
        "addressee": claim.get("addressee") or "self",
        "heard": claim.get("heard") or claim["text"],
    }


def _demoted(claim: dict) -> dict:
    kept = _without_directive_fields(claim)
    return kept | {"kind": claim.get("else_kind") or "assertion"}


# ---- what stands ------------------------------------------------------------------------


def state_of(verdict: str | None) -> str:
    return verdict if verdict in VERDICTS else "open"


def listing(store: Store, *, source_id: str | None = None, state: str | None = None) -> list[dict]:
    """Every directive (of one source when given), each with its state (`confirm`, `decline`,
    `open`), its time in the source, its title and date, newest source first."""
    verdicts = store.current_directive_verdicts(source_id)
    rows = store.directives(source_id)
    sources: dict[str, dict] = {}
    starts: dict[str, dict[int, float]] = {}
    out = []
    for row in rows:
        sid = row["source_id"]
        if sid not in sources:
            sources[sid] = store.source(sid) or {}
            starts[sid] = {
                segment["idx"]: float(segment.get("t0") or 0.0)
                for segment in store.best_segments(sid)
            }
        subject = subject_sha256(sid, row["heard"] or "", row["addressee"])
        found = state_of(verdicts.get(subject))
        if state is not None and found != state:
            continue
        first = min(row["segments"]) if row["segments"] else 0
        out.append(
            {
                "id": row["id"],
                "source_id": sid,
                "title": sources[sid].get("title") or sid,
                "date": str(sources[sid].get("published_at") or "")[:10],
                "subject": subject,
                "state": found,
                "addressee": row["addressee"] or "self",
                "heard": row["heard"] or "",
                "text": row["text"],
                "start_seconds": starts[sid].get(first, 0.0),
                "position": row["position"],
            }
        )
    out.sort(key=lambda item: (item["date"], item["source_id"], item["position"]), reverse=True)
    return out


def summary(store: Store) -> dict:
    counts = {"confirm": 0, "decline": 0, "open": 0}
    rows = listing(store)
    for row in rows:
        counts[row["state"]] += 1
    by_addressee = {"computer": 0, "self": 0}
    for row in rows:
        by_addressee[row["addressee"]] = by_addressee.get(row["addressee"], 0) + 1
    return {"directives": len(rows), "states": counts, "addressees": by_addressee}


def verdict(
    store: Store, claim_id: str, ruling: str, *, answered_through: str, created_by: str
) -> dict:
    """The owner's ruling on one directive, appended under its subject."""
    if ruling not in VERDICTS:
        raise ValueError("a verdict is confirm or decline")
    row = next((d for d in store.directives() if d["id"] == claim_id), None)
    if row is None:
        raise KeyError(f"no directive {claim_id!r}")
    subject = subject_sha256(row["source_id"], row["heard"] or "", row["addressee"])
    store.append_directive_verdict(
        row["source_id"], subject, ruling, answered_through=answered_through, created_by=created_by
    )
    return {"id": claim_id, "verdict": ruling}


__all__ = [
    "CRITERIA",
    "DEFAULT_THRESHOLD",
    "INSTRUCTIONS",
    "KIND",
    "QUESTION",
    "VERDICTS",
    "gate",
    "listing",
    "state_of",
    "subject_sha256",
    "summary",
    "verdict",
]
