"""Cleanup: the deterministic transcript cleanup steps, protected vocabulary and spell fix.

Both steps edit a transcript document (corpuslm.transcript) in place, text inside a segment,
never the segment set, and call no model and no outside service.

Protected vocabulary (`protect_vocabulary`): names and terms are restored to their protected
spelling wherever the text carries them case-insensitively or split across tokens, known
mis-hearings are substituted, and every protected span is marked so no later step touches it.
`vocabulary_from_texts` builds the vocabulary from a source's own words (title, summary
headings, a channel name, tags). `apply_correction_memory` substitutes the owner's term
verdicts after any model step, deterministically.

Spell fix (`spell_fix`): SymSpell over the remaining tokens, English only; a token is
corrected only when it is not a dictionary word and exactly one dictionary word sits at edit
distance 1 (ambiguous typos wait for a model step that has the sentence); capitalised
tokens, numbers, protected spans and short tokens are left alone. It needs the `clean` extra
(symspellpy).

The stored provenance keys of a document keep their legacy names, `passes.pass1`,
`passes.pass2` and `passes.memory`: renaming them would change stored bytes and force a model
rebuild.

Never imports: db, store, clikit, any model transport.

Decisions:
- Protected vocabulary runs first, then spell fix, then any model step: spell fix must not
  touch protected names, and a model gets the vocabulary as law.
- No later step alters a protected term. Enforced by:
  tests/test_basic_cleanup.py::test_spell_fix_fixes_lowercase_typos_and_leaves_protected_and_names_alone.
- Edits are made per segment, never over flattened text, so timestamps survive. Enforced by:
  tests/test_basic_cleanup.py::test_protect_vocabulary_restores_protected_spellings_and_marks_them.
- Spell fix runs only on English and is otherwise recorded as skipped. Enforced by:
  tests/test_basic_cleanup.py::test_spell_fix_is_skipped_for_a_non_english_source_with_provenance.
- Rerunning a step on the document it produced changes nothing. Enforced by:
  tests/test_basic_cleanup.py::test_a_rerun_changes_nothing.

Terms:
- **basic cleanup**: the cleanup that uses no model: protected vocabulary, then spell fix (the
  step with a model is the AI cleanup, `corpuslm.ai_cleanup`).
- **protected vocabulary**: names and terms no later step may alter; built from a source's own
  words and from correction memory term verdicts.
- **spell fix**: deterministic dictionary correction (SymSpell) over non-protected tokens;
  English only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache

_CAPITALISED_RUN = re.compile(
    r"\b(?:[A-Z][\w'’\-]+)(?:\s+(?:[A-Z][\w'’\-]+|of|the|de|von|van|and)){0,4}\b"
)
_STOP = {
    "The",
    "A",
    "An",
    "This",
    "That",
    "How",
    "Why",
    "What",
    "When",
    "Where",
    "Who",
    "Is",
    "Are",
    "In",
    "On",
    "At",
    "For",
    "With",
    "And",
    "But",
    "Or",
    "Part",
    "Episode",
    "Ep",
    "Vs",
    "Live",
    "New",
    "Full",
    "Official",
    "Video",
    "Interview",
    "Talk",
    "Lecture",
    "Podcast",
    "Clip",
    "Clips",
    "I",
    "We",
    "You",
    "It",
    "My",
    "Your",
}
_MIN_TERM = 4


@dataclass(frozen=True, slots=True)
class Vocabulary:
    terms: list[str]  # protected forms, longest first at use
    substitutions: dict[str, str]  # normalized mis-hearing → protected form
    sources: dict[str, str] = field(default_factory=dict)

    def merged(self, other: Vocabulary) -> Vocabulary:
        terms = list(dict.fromkeys(self.terms + other.terms))
        return Vocabulary(
            terms=terms,
            substitutions=self.substitutions | other.substitutions,
            sources=self.sources | other.sources,
        )

    def fingerprint(self) -> dict:
        return {
            "terms": sorted(self.terms),
            "substitutions": dict(sorted(self.substitutions.items())),
        }


def _candidates(text: str) -> list[str]:
    out = []
    for m in _CAPITALISED_RUN.finditer(text or ""):
        run = m.group(0).strip()
        words = run.split()
        while words and words[0] in _STOP:
            words = words[1:]
        while words and (words[-1] in _STOP or words[-1].lower() in ("of", "the", "and")):
            words = words[:-1]
        if not words:
            continue
        term = " ".join(words)
        if len(term) >= _MIN_TERM and any(len(w) >= _MIN_TERM for w in words):
            out.append(term)
    return out


def vocabulary_from_texts(
    *,
    names: Iterable[tuple[str, str | None]] = (),
    texts: Iterable[tuple[str, str | None]] = (),
    tags: Iterable[tuple[str, str | None]] = (),
) -> Vocabulary:
    """Protected vocabulary from a source's own words. Each argument holds (source label,
    text) pairs, in the order terms are taken: `names` are kept whole (a channel or person
    name of at least four characters), `texts` give their capitalised runs (a title, a
    description, summary headings), `tags` are kept whole when capitalised. The first source
    label to name a term stays in `sources`."""
    terms: dict[str, str] = {}
    for source, name in names:
        name = (name or "").strip()
        if len(name) >= _MIN_TERM:
            terms[name] = source
    for source, text in texts:
        for term in _candidates(text or ""):
            terms.setdefault(term, source)
    for source, tag in tags:
        tag = str(tag or "").strip()
        if tag[:1].isupper() and len(tag) >= _MIN_TERM and tag not in _STOP:
            terms.setdefault(tag, source)
    return Vocabulary(terms=list(terms), substitutions={}, sources=terms)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _apply_term(text: str, term: str) -> tuple[str, bool]:
    """Restore `term`'s spelling where the text carries it case-insensitively,
    or split across whitespace/hyphens (\"sarva priyananda\")."""
    changed = False
    # exact (case-insensitive) whole-word match → protected casing
    pattern = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)
    new = pattern.sub(term, text)
    if new != text:
        changed = True
        text = new
    if term in text:
        return text, changed
    # split-token match: the term's letters as one word, possibly broken by spaces/hyphens
    letters = re.sub(r"[^a-z0-9]", "", term.lower())
    if len(letters) < 6 or " " in term:
        return text, changed
    split = re.compile(
        r"(?<!\w)" + r"[\s\-]*".join(map(re.escape, letters)) + r"(?!\w)", re.IGNORECASE
    )
    new = split.sub(term, text)
    if new != text:
        changed = True
        text = new
    return text, changed


def protect_vocabulary(document: dict, vocab: Vocabulary) -> tuple[dict, dict]:
    """Restore protected spellings, substitute known mis-hearings and mark every protected
    span. Returns the document and a report of counts."""
    terms = sorted(dict.fromkeys(vocab.terms), key=len, reverse=True)
    subs = {k: v for k, v in vocab.substitutions.items() if k}
    changed_segments = 0
    subs_applied = 0
    for segment in document["segments"]:
        original = segment["text"]
        text = original
        for heard, protected in subs.items():
            pattern = re.compile(
                r"(?<!\w)" + r"[\s\-]*".join(map(re.escape, heard.split())) + r"(?!\w)",
                re.IGNORECASE,
            )
            new = pattern.sub(protected, text)
            if new != text:
                subs_applied += 1
                text = new
        for term in terms:
            text, _ = _apply_term(text, term)
        present = [term for term in terms if term in text]
        if text != original:
            changed_segments += 1
            segment.setdefault("raw", original)
            segment["text"] = text
        if present:
            segment["protected"] = present
        else:
            segment.pop("protected", None)
    document["state"] = "DETERMINISTIC-ONLY"
    # legacy stored key (module docstring)
    document.setdefault("passes", {})["pass1"] = {
        "status": "ran",
        "terms": len(terms),
        "substitutions": len(subs),
        "segments_changed": changed_segments,
        "substitutions_applied": subs_applied,
    }
    report = {
        "status": "ran",
        "terms": len(terms),
        "segments_changed": changed_segments,
        "substitutions_applied": subs_applied,
    }
    return document, report


@cache
def _symspell():
    from importlib.resources import files

    from symspellpy import SymSpell

    sym = SymSpell(max_dictionary_edit_distance=1, prefix_length=7)
    dictionary = files("symspellpy") / "frequency_dictionary_en_82_765.txt"
    sym.load_dictionary(str(dictionary), term_index=0, count_index=1)
    return sym


_TOKEN = re.compile(r"[A-Za-z][a-z]+")


def is_dictionary_word(word: str) -> bool:
    """English dictionary membership (SymSpell's frequency list)."""
    from symspellpy import Verbosity

    hits = _symspell().lookup(
        word.lower(), Verbosity.TOP, max_edit_distance=0, include_unknown=False
    )
    return bool(hits)


def spell_fix(document: dict, *, language: str) -> tuple[dict, dict]:
    if not (language or "").lower().startswith("en"):
        # legacy stored key (module docstring)
        document.setdefault("passes", {})["pass2"] = {
            "status": "skipped",
            "reason": f"language is {language or 'unknown'}, not English",
        }
        if document["state"] == "RAW":
            document["state"] = "DETERMINISTIC-ONLY"
        return document, {"status": "skipped", "reason": f"language is {language or 'unknown'}"}
    from symspellpy import Verbosity

    sym = _symspell()
    tokens_changed = 0
    segments_changed = 0
    for segment in document["segments"]:
        original = segment["text"]
        protected_spans = segment.get("protected") or []
        masks: list[tuple[int, int]] = []
        for span in protected_spans:
            for m in re.finditer(re.escape(span), original):
                masks.append((m.start(), m.end()))

        def fix(m: re.Match, masks=masks) -> str:
            nonlocal tokens_changed
            word = m.group(0)
            if any(a <= m.start() < b for a, b in masks):
                return word
            if word[0].isupper() or len(word) < 4:
                return word
            # Conservative by design: a token is corrected only when it is
            # not a dictionary word AND exactly one dictionary word sits at
            # edit distance 1. "realy" (real / really / relay) is left for
            # a model step, which has the sentence; "elephnts" is fixed here.
            suggestions = sym.lookup(
                word, Verbosity.ALL, max_edit_distance=1, include_unknown=False
            )
            if not suggestions or any(s.distance == 0 for s in suggestions):
                return word
            if len(suggestions) != 1:
                return word
            tokens_changed += 1
            return suggestions[0].term

        text = _TOKEN.sub(fix, original)
        if text != original:
            segments_changed += 1
            segment.setdefault("raw", original)
            segment["text"] = text
    document["state"] = "DETERMINISTIC-ONLY"
    # legacy stored key (module docstring)
    document.setdefault("passes", {})["pass2"] = {
        "status": "ran",
        "tokens_changed": tokens_changed,
        "segments_changed": segments_changed,
    }
    return document, {
        "status": "ran",
        "tokens_changed": tokens_changed,
        "segments_changed": segments_changed,
    }


def vocabulary_fingerprint(vocab: Vocabulary) -> str:
    return json.dumps(vocab.fingerprint(), sort_keys=True)


def apply_correction_memory(document: dict, vocab: Vocabulary) -> tuple[dict, dict]:
    """The correction memory as a patch-through: term verdicts are substituted into the
    transcript AFTER any model step, deterministically, so a new verdict never changes the
    text the model was shown and never invalidates its cache. Substitutions apply
    case-insensitively, split-token tolerant, and every substituted term is marked protected."""
    terms = sorted(dict.fromkeys(vocab.terms), key=len, reverse=True)
    applied = 0
    segments_changed = 0
    for segment in document["segments"]:
        original = segment["text"]
        text = original
        for heard, protected in vocab.substitutions.items():
            if not heard:
                continue
            pattern = re.compile(
                r"(?<!\w)" + r"[\s\-]*".join(map(re.escape, heard.split())) + r"(?!\w)",
                re.IGNORECASE,
            )
            new = pattern.sub(protected, text)
            if new != text:
                applied += 1
                text = new
        present = [term for term in terms if term in text]
        if text != original:
            segments_changed += 1
            segment.setdefault("raw", original)
            segment["text"] = text
        if present:
            segment["protected"] = list(dict.fromkeys((segment.get("protected") or []) + present))
    document.setdefault("passes", {})["memory"] = {
        "status": "ran",
        "rules": len(vocab.substitutions),
        "applied": applied,
        "segments_changed": segments_changed,
    }
    return document, {"applied": applied, "segments_changed": segments_changed}
