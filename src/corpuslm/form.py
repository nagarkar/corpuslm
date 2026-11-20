"""Form: what kind of conversation a source is, so the claim pass knows how to read it.

A source's form is one of `monologue` (the owner alone), `dialogue` (the owner and one other),
`group` (three or more) or `media` (many voices, speeches, a gathering, or a device playing). The
speaker count proposes: the named speakers on the source's best segments, leaving out the labels
that name nobody (`Unknown`, `Media`, `Noise`, `Someone else`, `unclear`, `noise`, `media`,
`other`): one is a monologue, two a dialogue, three or more a group. Jev decides (corpuslm.judge)
from the count and a sample of turns, the one judge that can tell a gathering or a playing
device from a conversation. Both are recorded on the source row, `metadata.form =
{"count", "proposed", "jev", "label", "owner"?}`; `label` is what stands: the owner's ruling when
there is one, else Jev's, else the count's. A disagreement between the count and Jev is a review
card of kind `form` (corpuslm.directives_page) until the owner rules.

One case is settled without Jev's say: a recording whose only speaker labels are the owner's name
and `Unknown` (or none) is the owner alone, because the recorder splits one voice between the
two. It is a monologue; Jev is still asked and recorded, but only its `media` overrules (a
podcast heard beside the owner is not the owner speaking), and its `dialogue` or `group` is set
aside with no card. The record then carries `owner_alone: true`. `outdated` says when a record no
longer fits the lines it was made from (the count or this case changed), so a caller classifies
again.

Never imports: cli, clikit. Reads the database only through `Store`.

Decisions:
- The count proposes, Jev decides, both are recorded, and a disagreement is a review card for
  the owner (owner, 2026-10-21). Enforced by:
  tests/test_form.py::test_the_count_proposes_jev_decides_and_a_disagreement_is_a_card.
- A recording whose only labels are the owner's name and `Unknown` is a monologue unless Jev
  says `media`; Jev's `dialogue` or `group` is recorded and set aside, with no review card. Why:
  owner, 2026-11-20 (nagarkar/beelm#43): Bee split the owner's speech between the two labels and
  Jev called two recordings of the owner alone a dialogue. (owner) Enforced by:
  tests/test_form.py::test_the_owners_name_and_unknown_alone_is_a_monologue_unless_jev_says_media.
- A form record is classified again when the lines it was made from changed their count or
  stopped (or started) being the owner alone; the owner's ruling is never outdated. Enforced by:
  tests/test_form.py::test_a_record_made_before_the_lines_changed_is_outdated.
- Without `TYPESAFE_API_KEY` the count's label stands and the board says Jev was off; nothing
  fails. Enforced by: tests/test_form.py::test_without_jev_the_counts_label_stands.
- The owner's ruling wins and is never overwritten by a reclassification. Enforced by:
  tests/test_form.py::test_the_owners_ruling_stands_through_a_reclassification.

Terms:
- **form**: monologue, dialogue, group or media; the shape of a recorded conversation.
- **form record**: `metadata.form` on the source row: the count, the count's label, Jev's
  label, the owner's, whether the lines are the owner alone, and the label in force.
- **owner alone**: every speaker label is the owner's name, `Unknown` or empty, and the owner's
  name is on at least one line.
"""

from __future__ import annotations

from . import judge as judge_module
from .store import Store

STAGE = "form"
QUESTION = "form"
LABELS = ("monologue", "dialogue", "group", "media")
# Speaker labels that name nobody, compared in lower case.
UNNAMED = frozenset(
    {"unknown", "media", "noise", "someone else", "unclear", "other", "", "speaker"}
)
SAMPLE_TURNS = 40  # turns shown to Jev, spread over the source
TURN_CHARS = 200  # each turn cut here: the form shows in who speaks, not in what is said

CRITERIA = {
    "monologue": "one person speaking alone: thinking aloud, dictating notes, talking to the "
    "recorder or to Computer; nobody answers",
    "dialogue": "two people talking with each other, taking turns",
    "group": "three or more people in one conversation, taking turns",
    "media": "many voices or speeches, a gathering, a class, a ceremony, a meeting with an "
    "audience, or a device playing (a podcast, a video, music, the radio)",
}
INSTRUCTIONS = (
    "What is the form of this recorded conversation? `speaker_count` is how many named speakers "
    "the recorder's diarization heard, and `proposed` is the label that count suggests; the "
    "recorder often mislabels, so judge by the sampled turns: who speaks, whether anyone answers, "
    "whether the voices are a conversation or something playing or performed."
)


def named_speakers(segments: list[dict]) -> list[str]:
    """The distinct speaker labels that name somebody, in order of first appearance."""
    seen: dict[str, None] = {}
    for segment in segments:
        label = str(segment.get("speaker") or "").strip()
        if label.lower() in UNNAMED or label.lower().startswith("speaker "):
            continue
        seen.setdefault(label)
    return list(seen)


# What the recorder calls a voice it did not recognise; with the owner's name, the owner's own.
UNRECOGNISED = frozenset({"unknown", ""})


def owner_alone(segments: list[dict], owner: str) -> bool:
    """Every speaker label is the owner's name, `Unknown` or empty, and the owner's name is on
    at least one line. False without an owner's name."""
    name = owner.strip().lower()
    if not name:
        return False
    labels = {str(segment.get("speaker") or "").strip().lower() for segment in segments}
    return name in labels and labels <= UNRECOGNISED | {name}


def propose(segments: list[dict]) -> tuple[int, str]:
    """(the count, the label the count proposes)."""
    count = len(named_speakers(segments))
    if count <= 1:
        return count, "monologue"
    if count == 2:
        return count, "dialogue"
    return count, "group"


def sample(segments: list[dict], limit: int = SAMPLE_TURNS) -> list[str]:
    """Up to `limit` turns spread evenly over the source, each `speaker: text` cut short."""
    if not segments:
        return []
    step = max(1, len(segments) // limit)
    picked = segments[::step][:limit]
    return [
        f"{segment.get('speaker') or '?'}: {str(segment.get('text') or '')[:TURN_CHARS]}"
        for segment in picked
    ]


def state_of(segments: list[dict], count: int, proposed: str) -> dict:
    return {
        "speaker_count": count,
        "proposed": proposed,
        "named_speakers": named_speakers(segments),
        "turns": sample(segments),
    }


def classify(
    store: Store,
    judge: judge_module.Judge | None,
    source_id: str,
    *,
    created_by: str,
    owner: str = "",
) -> dict:
    """Propose by count, ask Jev when there is one, write the form record (keeping the owner's
    ruling when there is one) and return it with `cost_usd`, `cached` and `disagree`. With the
    owner's first name in `owner`, lines that are the owner alone are a monologue unless Jev says
    `media`."""
    segments = store.best_segments(source_id)
    count, proposed = propose(segments)
    before = store.source_form(source_id) or {}
    record: dict = {"count": count, "proposed": proposed, "jev": None, "label": proposed}
    alone = owner_alone(segments, owner)
    if alone:
        record["owner_alone"] = True
    cost, cached = 0.0, True
    if judge is not None:
        choice, probabilities, call = judge.choose(
            state_of(segments, count, proposed),
            QUESTION,
            INSTRUCTIONS,
            CRITERIA,
            tag=f"{source_id}-form",
        )
        record["jev"] = choice
        record["jev_probabilities"] = {k: round(v, 3) for k, v in probabilities.items()}
        record["label"] = proposed if alone and choice != "media" else choice
        cost, cached = call["cost_usd"] or 0.0, call["cached"]
    if before.get("owner") in LABELS:
        record["owner"] = before["owner"]
        record["label"] = before["owner"]
    store.set_source_form(source_id, record, created_by=created_by)
    return record | {
        "cost_usd": round(cost, 6),
        "cached": cached,
        "disagree": disagrees(record),
    }


def disagrees(record: dict) -> bool:
    """The count and Jev differ and the owner has not ruled: a review card. Not when the lines
    are the owner alone and Jev said anything but `media`: that answer is set aside."""
    if record.get("owner_alone") and record.get("jev") != "media":
        return False
    return (
        record.get("jev") in LABELS
        and record.get("jev") != record.get("proposed")
        and record.get("owner") not in LABELS
    )


def outdated(store: Store, source_id: str, owner: str = "") -> bool:
    """A form record that no longer fits the source's lines: the speaker count changed, or the
    lines became (or stopped being) the owner alone. False with no record (nothing to redo) and
    when the owner ruled."""
    record = store.source_form(source_id)
    if not record or record.get("owner") in LABELS:
        return False
    segments = store.best_segments(source_id)
    count, _ = propose(segments)
    return record.get("count") != count or bool(record.get("owner_alone")) != owner_alone(
        segments, owner
    )


def label_of(store: Store, source_id: str) -> str | None:
    record = store.source_form(source_id)
    label = (record or {}).get("label")
    return label if label in LABELS else None


def rule(store: Store, source_id: str, label: str, *, created_by: str) -> dict:
    """The owner's ruling: it becomes the label in force and is kept through reclassification."""
    if label not in LABELS:
        raise ValueError(f"a form is one of {', '.join(LABELS)}")
    record = store.source_form(source_id) or {"count": 0, "proposed": "", "jev": None}
    record = record | {"owner": label, "label": label}
    store.set_source_form(source_id, record, created_by=created_by)
    return record


def cards(store: Store, source_ids: list[str]) -> list[dict]:
    """The form disagreements of `source_ids`, one card each, for the review surface."""
    out = []
    for source_id in source_ids:
        record = store.source_form(source_id)
        if not record or not disagrees(record):
            continue
        row = store.source(source_id) or {}
        out.append(
            {
                "source_id": source_id,
                "title": row.get("title") or source_id,
                "date": str(row.get("published_at") or "")[:10],
                "count": record.get("count", 0),
                "proposed": record.get("proposed", ""),
                "jev": record.get("jev"),
                "label": record.get("label", ""),
            }
        )
    return out


__all__ = [
    "CRITERIA",
    "LABELS",
    "QUESTION",
    "STAGE",
    "UNNAMED",
    "cards",
    "classify",
    "disagrees",
    "label_of",
    "named_speakers",
    "outdated",
    "owner_alone",
    "propose",
    "rule",
    "sample",
    "state_of",
]
