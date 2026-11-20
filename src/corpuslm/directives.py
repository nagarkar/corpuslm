"""Directives: what the owner asked for, as claims of kind `directive`, gated and ruled on.

A directive is an instruction, a piece of guidance or a commitment the owner spoke: a claim of
kind `directive` in the shared `claims` table (nagarkar/corpuslm#86), produced by the Bee variant
of the claims prompt (`claims_bee`, corpuslm.claims) in the same model read as the other claims,
with two columns of its own: `addressee` (`computer` for what Computer is to act on, `self` for a
note to the owner) and `heard` (the words as heard, verbatim: the lines the model cites). The
model says on what basis a line is a candidate (`basis`: `wake_word`, `guidance`,
`instruction`, `commitment`); the gate here, never the model, decides from the basis and the
conversation's form (corpuslm.form) whether it is a directive, for whom, and whether Jev checks it ("is this something the owner
wants acted on", corpuslm.judge, at `[claims].directive_threshold`):

| basis | monologue | dialogue, group | media, unclassified |
|---|---|---|---|
| `wake_word`: addressed to "Computer" | `computer`, Jev | `computer` | `computer` |
| `guidance`: "we should", "the next step is" | `computer`, Jev | `computer`, Jev | a claim |
| `instruction`: "write that down" | `computer`, Jev | a claim | a claim |
| `commitment`: "I will call her" | `self`, Jev | a claim | a claim |

A candidate the gate leaves out stays a claim of its ordinary kind (the reply's `else_kind`). A
reply without a basis (the first version of the prompt) is read by its addressee: `computer` as
the wake word, anything else as a commitment.

The owner rules on each directive from the review surface (corpuslm.directives_page): confirm or
decline, an append-only row of `directive_verdicts` keyed by the directive's subject (the source,
the words as heard and the addressee), so a rebuild that extracts the same directive keeps the
ruling and never overwrites it. Moments, concepts and the monologue recipes never read
directives (`Store.claims` leaves them out by default).

Never imports: cli, clikit. Reads the database only through `Store`.

Decisions:
- Guidance the owner speaks in general terms is for Computer: any line of the owner's that says
  work ought to be done ("we should", "we must", "we need", "the next step is", "there should
  be"), read by meaning, never from a phrase list, and checked by Jev; alone or in a dialogue or
  group, never in a media recording. Its addressee is `computer` outright, as a wake-word
  utterance's is. Why: owner, 2026-11-20, "There may be guidance in general like "we should do
  this" or "the next step is to" and those are guidances for computer" (nagarkar/beelm#43).
  (owner) Enforced by:
  tests/test_directives.py::test_in_a_monologue_guidance_and_plain_instructions_are_for_computer,
  tests/test_directives.py::test_in_company_guidance_is_for_computer_and_a_plain_instruction_is_a_claim,
  tests/test_directives.py::test_in_a_media_recording_only_the_wake_word_is_a_directive.
- A plain instruction without the wake word ("write that down") is for Computer when the owner
  is alone and an ordinary claim in company, where it is said to the person in the room; a
  first-person promise is a note to the owner (`self`), listed and never acted on, and only when
  alone. An utterance addressed to "Computer" is for Computer in every form, whatever it asks.
  Household members are not addressees. (owner) Enforced by:
  tests/test_directives.py::test_in_a_monologue_guidance_and_plain_instructions_are_for_computer,
  tests/test_directives.py::test_in_company_only_an_addressed_utterance_is_a_directive.
- The gate decides the addressee from the basis and the form; the model's own `addressee` is
  read only when a reply has no basis. Enforced by:
  tests/test_directives.py::test_the_gate_not_the_model_decides_the_addressee.
- A candidate below the threshold keeps its ordinary kind. (owner) Enforced by:
  tests/test_directives.py::test_a_monologue_candidate_below_the_threshold_keeps_its_ordinary_kind.
- Without Jev the candidates Jev would check stand ungated and the artifact says so. Enforced by:
  tests/test_directives.py::test_without_jev_monologue_candidates_stand_and_the_pass_says_so.
- A rebuild never overwrites the owner's verdict: the verdict is keyed by subject, not by claim
  id. (owner, 2026-10-21) Enforced by:
  tests/test_directives.py::test_a_rebuild_keeps_the_verdict_on_the_same_directive.
- Moments, concept tagging and the corpus document skip kind `directive`. (owner, 2026-10-21)
  Enforced by: tests/test_directives.py::test_the_other_passes_never_see_a_directive.

Terms:
- **directive**: an instruction, guidance or commitment the owner spoke; a claim of kind
  `directive`.
- **basis**: why a line is a candidate: `wake_word` (addressed to "Computer"), `guidance` (says
  work ought to be done), `instruction` (a plain imperative) or `commitment` (a first-person
  promise).
- **addressee**: `computer` (Computer is to act on it) or `self` (a note to the owner).
- **heard**: a directive's words as heard, verbatim.
- **directive gate**: the form rule and Jev's yes/no on a candidate, applied at the threshold.
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
BASES = ("wake_word", "guidance", "instruction", "commitment")
COMPUTER, SELF = "computer", "self"
INSTRUCTIONS = (
    "`utterance` is something the owner of a wearable recorder said, as the recorder heard it, "
    "and `restated` is a model's one-sentence reading of it. `form` says whether the owner was "
    "alone (monologue) or in company, and `basis` why it was picked: `wake_word` (addressed to "
    'the owner\'s assistant, "Computer"), `instruction` (a plain imperative), `guidance` (the '
    'owner saying that work ought to be done: "we should explore this and write it down", '
    '"we must put that into the sermons", "we need an argument for it", "the next step is '
    'to", "there should be a chapter on that"; the owner\'s "we" is the owner and the '
    "assistant) or `commitment` (a first-person promise). Is it something the owner wants acted "
    "on later (work to do, write, explore, explain, add, remember, buy, send, book, look up, or "
    "a promise to someone), rather than a thought spoken aloud, a quotation, a hypothetical, a "
    "remark about the past, or a general opinion about how people or the world should be?"
)
CRITERIA = {
    "true": "the owner wants this done or remembered: an actionable instruction, guidance about "
    "work that ought to be done on the owner's own projects, or a commitment",
    "false": "thinking aloud, narration, a quotation, a hypothetical, advice to nobody, a "
    "general opinion about how people or the world should be, or a remark that merely mentions "
    "doing something",
}
# basis -> form -> (addressee, whether Jev checks it); a form not listed leaves the candidate out
_ALONE = ("monologue",)
_TOGETHER = ("dialogue", "group")
RULES: dict[str, dict[str, tuple[str, bool]]] = {
    "guidance": dict.fromkeys(_ALONE + _TOGETHER, (COMPUTER, True)),
    "instruction": dict.fromkeys(_ALONE, (COMPUTER, True)),
    "commitment": dict.fromkeys(_ALONE, (SELF, True)),
}


def basis_of(claim: dict) -> str:
    """Why a candidate is one: the reply's `basis`, else (a reply of the first prompt version)
    its addressee read as the wake word or a commitment."""
    basis = claim.get("basis")
    if basis in BASES:
        return str(basis)
    return "wake_word" if claim.get("addressee") == COMPUTER else "commitment"


def ruling(basis: str, form: str) -> tuple[str, bool] | None:
    """(addressee, whether Jev checks it) for a candidate of `basis` in a conversation of
    `form`; None when it is no directive there."""
    if basis == "wake_word":
        return COMPUTER, form in _ALONE
    return RULES.get(basis, {}).get(form)


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
    """Apply the form rule and Jev's gate to the candidates in `claims` (dicts as
    `Store.replace_claims` takes them, plus `basis` and `else_kind`). Returns the claims to store
    and counts: `candidates`, `asked`, `directives`, `demoted`, `cost_usd`, `gate` (`jev` when
    Jev was asked, `off` when it should have been and there is none, `wake word` when the form
    rule alone decided, `none` with no candidate)."""
    stats = {
        "candidates": 0,
        "asked": 0,
        "directives": 0,
        "demoted": 0,
        "cost_usd": 0.0,
        "gate": "none",
    }
    ungated = False
    out = []
    for position, claim in enumerate(claims):
        if claim.get("kind") != KIND:
            out.append(_without_directive_fields(claim))
            continue
        stats["candidates"] += 1
        basis = basis_of(claim)
        ruled = ruling(basis, form)
        if ruled is None:
            stats["demoted"] += 1
            out.append(_demoted(claim))
            continue
        addressee, checked = ruled
        claim = dict(claim) | {"addressee": addressee}
        keep = True
        if checked and judge is None:
            ungated = True
        elif checked and judge is not None:
            probability, call = judge.yes(
                {
                    "utterance": claim.get("heard") or "",
                    "restated": claim["text"],
                    "basis": basis,
                    "form": form,
                },
                QUESTION,
                INSTRUCTIONS,
                CRITERIA,
                tag=f"{tag_prefix}-d{position}",
            )
            stats["asked"] += 1
            stats["cost_usd"] = round(stats["cost_usd"] + (call["cost_usd"] or 0.0), 6)
            claim = claim | {"gate_probability": round(probability, 3)}
            keep = probability >= threshold
        if keep:
            stats["directives"] += 1
            out.append(_directive(claim))
        else:
            stats["demoted"] += 1
            out.append(_demoted(claim))
    if stats["asked"]:
        stats["gate"] = "jev"
    elif ungated:
        stats["gate"] = "off"
    elif stats["candidates"]:
        stats["gate"] = "wake word"
    return out, stats


def _without_directive_fields(claim: dict) -> dict:
    return {
        key: value
        for key, value in claim.items()
        if key not in ("addressee", "heard", "else_kind", "basis", "gate_probability")
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
    "BASES",
    "CRITERIA",
    "DEFAULT_THRESHOLD",
    "INSTRUCTIONS",
    "KIND",
    "QUESTION",
    "RULES",
    "VERDICTS",
    "basis_of",
    "gate",
    "listing",
    "ruling",
    "state_of",
    "subject_sha256",
    "summary",
    "verdict",
]
