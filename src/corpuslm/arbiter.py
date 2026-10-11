"""Arbiter: a second opinion on the ambiguities the AI cleanup left pending.

The arbiter (corpuslm.arbiter): a second opinion from an outside judge (ytlm asks Jev, TypeSafe's
System One model) on review items the AI cleanup left pending, for every source kind.

Each pending ambiguity is one Choice question whose candidates are exactly the card's: the
words as heard plus the candidates. The judge's probability for each candidate is multiplied by the
AI cleanup's and rescaled (Bayes' rule, independent sources, no prior preference between
candidates). When the judge's candidate reaches `Settings.min_probability` it is applied as
`auto_resolved` with an `arbiter` origin; otherwise the item stays pending for the owner. An
arbiter pick never enters the correction memory: only the owner's word is precedent.

Every request is cached by payload in the client's cache, so a rebuild never re-bills; spend
counts toward the client's caps through its guard and price table.

What a tool injects: the transport that reaches the judge (a `Transport`; ytlm's wraps its
TypeSafe adapter and keeps the key), the prompt, and the validator of the judge's reply. The
card's candidates, the span key and the writing of a resolution are `corpuslm.review`'s, the same
ones the review page uses.

Never imports: db, store, cli, clikit, any outside service.

Decisions:
- The adapter that reaches the judge is the tool's, injected as a `Transport`; this module holds
  no vendor name, key or URL. Enforced by:
  tests/test_arbiter.py::test_the_arbiter_asks_through_the_transport_it_is_given.
- Per candidate, the AI cleanup's probability is multiplied by the judge's and rescaled to sum
  to 1; an older item's single confidence goes to its pick and the rest is spread evenly.
  Enforced by:
  tests/test_arbiter.py::test_an_older_item_with_one_confidence_spreads_the_rest_evenly.
- When the judge's candidate reaches the threshold it is applied as `auto_resolved` with an arbiter
  origin; otherwise the item stays pending. One threshold serves agreement and disagreement
  alike. Enforced by:
  tests/test_arbiter.py::test_a_pick_that_clears_the_threshold_is_applied_with_an_arbiter_origin,
  tests/test_arbiter.py::test_a_pick_below_the_threshold_stays_pending.
- Replies are cached by payload; a reply that fails its check is never cached and skips only its
  item. Enforced by:
  tests/test_arbiter.py::test_a_bad_reply_is_never_cached_and_skips_only_its_item.
- A failed or spend-capped arbiter leaves its items pending and says why in its counts, so the
  next run asks again. Enforced by:
  tests/test_arbiter.py::test_the_spend_cap_stops_the_arbiter_and_leaves_the_rest_pending.

Terms:
- **judge**: the outside model the arbiter asks (ytlm: Jev, TypeSafe's System One model).
- **arbiter**: the step inside the AI cleanup that asks the judge about each still-pending
  ambiguity and applies a candidate whose combined probability reaches the threshold.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import clock, files, llm, review
from .identity import normalize_span
from .prompts import Prompt

STAGE = "arbiter"
QUESTION = "reading"
MAX_PRECEDENT = 10  # a bounded state; rules sorted by span


class ArbiterError(RuntimeError):
    """The judge's reply was unusable, or the service failed: the items stay pending and the
    next run asks again."""


@dataclass(frozen=True, slots=True)
class Settings:
    enabled: bool
    model: str
    min_probability: float


def settings(config: dict) -> Settings | None:
    """None when the `[arbiter]` section is absent (absent means off)."""
    section = config.get("arbiter")
    if section is None:
        return None
    return Settings(
        enabled=bool(section.get("enabled", False)),
        model=str(section.get("model", "")),  # config.toml only; no model in code
        min_probability=float(section.get("min_probability", 0.8)),
    )


# ---- the rule ---------------------------------------------------------------


def _key(text: str) -> str:
    return normalize_span(text) or text


def clean_up_probabilities(a: dict, offered_candidates: list[str]) -> dict[str, float]:
    """The AI cleanup's probability per candidate. Prompt v2 gives one per
    candidate; an older item's single confidence goes to its pick and the
    rest is spread evenly over the other candidates."""
    by_key = {_key(o): o for o in offered_candidates}
    if a.get("probabilities") is not None:
        out = dict.fromkeys(offered_candidates, 0.0)
        for c, p in zip(a["candidates"], a["probabilities"], strict=True):
            out[by_key[_key(c)]] += float(p)
        return out
    pick = by_key[_key(a["pick"])]
    others = [o for o in offered_candidates if o != pick]
    conf = float(a["confidence"])
    out = {o: (1 - conf) / len(others) for o in others} if others else {}
    out[pick] = conf if others else 1.0
    return out


def combine(p_clean_up: dict[str, float], p_judge: dict[str, float]) -> dict[str, float]:
    """Multiply per candidate and rescale to sum to 1; all zeros → all zeros."""
    raw = {o: p_clean_up.get(o, 0.0) * p_judge.get(o, 0.0) for o in p_clean_up}
    total = sum(raw.values())
    return {o: (v / total if total else 0.0) for o, v in raw.items()}


def precedent(a: dict, offered_candidates: list[str], memory: dict[str, dict]) -> list[dict]:
    """Correction-memory rules whose span or verdict shares a word (three
    letters or more) with the item's span or candidates."""
    words = {w for text in (a["span"], *offered_candidates) for w in _words(text)}
    hits = []
    for span_norm, rule in sorted(memory.items()):
        if words & (_words(span_norm) | _words(rule["verdict"])):
            hits.append({"heard": rule.get("span") or span_norm, "author_chose": rule["verdict"]})
    return hits[:MAX_PRECEDENT]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3}


def request(
    a: dict,
    sentence: str,
    *,
    model: str,
    protected: list[str],
    memory: dict[str, dict],
    instructions: str,
) -> dict:
    offered_candidates = review.candidates_offered(a)
    # The sentence as the recognizer produced it: the cleanup's pick is put
    # back to the words heard, bracketed. No separate "heard" field: given a
    # blank and the heard words, Jev copied the mis-hearing (2026-09-29).
    marked = sentence
    for needle in (a["pick"], a["span"]):
        if needle and needle in sentence:
            marked = sentence.replace(needle, f"[{a['span']}]", 1)
            break
    return {
        "model": model,
        "state": {
            "sentence": marked,
            "protected_names": sorted(protected),
            "author_precedent": precedent(a, offered_candidates, memory),
        },
        "questions": {
            QUESTION: {
                "type": "choice",
                "instructions": instructions,
                "criteria": dict.fromkeys(offered_candidates),
            }
        },
    }


# ---- transports -------------------------------------------------------------


class Transport(Protocol):
    name: str

    def send(self, body: dict, *, tag: str) -> dict: ...


class ScriptedTransport:
    """docs/design/testing.md: `<dir>/arbiter/<tag>.json` holds a TypeSafe response body."""

    name = "scripted"

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def send(self, body: dict, *, tag: str) -> dict:
        path = self.directory / STAGE / f"{tag}.json"
        if not path.exists():
            raise llm.NotConfigured(f"scripted transport: no reply at {path}")
        return json.loads(path.read_text(encoding="utf-8"))


def off_reason(s: Settings | None, transport: Transport, *, key_env: str) -> str | None:
    """Why the arbiter is off, or None. `key_env` names the environment variable a transport
    named "typesafe" needs."""
    if s is None:
        return "[arbiter] is absent from config.toml"
    if not s.enabled:
        return "[arbiter].enabled is false in config.toml"
    if not s.model:
        return "[arbiter].model is not set in config.toml"
    if llm.price_usd(s.model, 0, 0) is None:
        return f"{s.model} has no price row, so the spend cap cannot be enforced"
    if transport.name == "typesafe" and not os.environ.get(key_env):
        return f"{key_env} is not set in .env"
    return None


# ---- the pass ---------------------------------------------------------------


class Arbiter:
    def __init__(
        self,
        client: llm.Client,
        settings: Settings,
        transport: Transport,
        prompt: Prompt,
        validate_reply: Callable[[dict], None],
        judge: str = "the judge",
    ):
        self.client = client
        self.settings = settings
        self.transport = transport
        self.prompt = prompt
        self.validate_reply = validate_reply
        self.judge = judge
        self.instructions = prompt.render({}).strip()

    def fingerprint(self) -> dict:
        """The arbiter's settings that, when changed, decide again from the cache without a call
        (they are a PATCH input of the AI cleanup)."""
        return {
            "prompt": self.prompt.sha256,
            "model": self.settings.model,
            "min_probability": self.settings.min_probability,
        }

    def run(
        self, document: dict, *, source: str, protected: list[str], memory: dict[str, dict]
    ) -> dict:
        """Ask about every pending ambiguity in `t` and apply the picks that
        clear the threshold, in place. Returns counts; a failure stops the
        pass and is returned as `error` (with `spend_cap_reached` when the spend cap
        stopped it). Items already decided stay decided; the rest stay
        pending, and the next run asks again."""
        stats: dict = {
            "asked": 0,
            "applied": 0,
            "overrode": 0,
            "deferred": 0,
            "bad": 0,
            "cost_usd": 0.0,
            "cached": 0,
            "error": "",
            "spend_cap_reached": False,
            "calls": [],
        }
        for a in document.get("ambiguities") or []:
            if a["status"] != "pending":
                continue
            try:
                self._decide(
                    document, a, source=source, protected=protected, memory=memory, stats=stats
                )
            except llm.SpendCapReached as e:
                stats["error"], stats["spend_cap_reached"] = f"budget: {e}", True
                stats["cap"] = e.cap
                break
            except ArbiterError as e:
                # this item only: never cached, asked again on the next run
                stats["error"] = str(e)
                stats["bad"] += 1
            except (llm.VendorError, llm.NotConfigured) as e:
                stats["error"] = str(e)
                break
        return stats

    def _decide(
        self,
        document: dict,
        a: dict,
        *,
        source: str,
        protected: list[str],
        memory: dict[str, dict],
        stats: dict,
    ) -> None:
        offered_candidates = review.candidates_offered(a)
        body = request(
            a,
            document["segments"][a["idx"]]["text"],
            model=self.settings.model,
            protected=protected,
            memory=memory,
            instructions=self.instructions,
        )
        tag = f"{source}-{a['key']}"
        reply, call = self._ask(
            body,
            tag=tag,
            check=lambda r: _check(r, offered_candidates, tag, self.validate_reply, self.judge),
        )
        stats["asked"] += 1
        stats["cached"] += call["cached"]
        stats["cost_usd"] = round(stats["cost_usd"] + (call["cost_usd"] or 0.0), 6)
        stats["calls"].append(call)
        answer = reply["answers"][QUESTION]
        choice, p_judge = answer["choice"], answer["probabilities"]
        p = combine(clean_up_probabilities(a, offered_candidates), p_judge)[choice]
        if p < self.settings.min_probability:
            stats["deferred"] += 1
            return
        model = str(reply.get("model") or self.settings.model)
        a["status"] = "auto_resolved"
        a["resolution"] = choice
        a["origin"] = f"arbiter {model} {p:.2f}"
        a["resolved_at"] = self._now()
        a["arbiter"] = {
            "model": model,
            "probabilities": p_judge,
            "p_combined": round(p, 3),
            "overrode": choice != a["pick"],
        }
        review.apply_resolution(document, a, choice)
        stats["applied"] += 1
        stats["overrode"] += choice != a["pick"]

    def _ask(self, body: dict, *, tag: str, check) -> tuple[dict, dict]:
        """One question: from the cache, else from the judge. `check` runs before
        the reply is cached (ytlm.llm), so a bad reply is never served again."""
        key = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        sha256 = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self.client.cache_dir / f"{sha256}.json"
        requested_at = self._now()
        if path.exists():
            reply = json.loads(path.read_text(encoding="utf-8"))["response"]
            check(reply)
            self._ledger(reply, tag, cost=0.0, cached=True)
            return reply, {
                "tag": tag,
                "payload_sha256": sha256,
                "requested_at": requested_at,
                "cached": True,
                "cost_usd": 0.0,
            }
        c = self.client
        c.check_spend_cap(STAGE, tag)
        reply = self.transport.send(body, tag=tag)
        self._log(tag, sha256, body, reply)
        c.calls += 1
        tokens_in = int((reply.get("usage") or {}).get("input_tokens", 0) or 0)
        cost = llm.price_usd(
            self.settings.model, tokens_in, 0
        )  # null when unknown, never an estimate
        c.spent_usd += cost or 0.0
        try:
            check(reply)
        except ArbiterError:
            self._ledger(reply, tag, cost=cost, cached=False, outcome="bad_reply")
            raise
        files.write_atomic(
            path,
            json.dumps(
                {
                    "request": body,
                    "response": reply,
                    "cost_usd": cost,
                    "requested_at": requested_at,
                },
                ensure_ascii=False,
                indent=1,
            ),
        )
        self._ledger(reply, tag, cost=cost, cached=False)
        return reply, {
            "tag": tag,
            "payload_sha256": sha256,
            "requested_at": requested_at,
            "cached": False,
            "cost_usd": cost,
        }

    def _now(self) -> str:
        return clock.iso(self.client.clock())

    def _ledger(
        self, reply: dict, tag: str, *, cost: float | None, cached: bool, outcome: str = "ok"
    ) -> None:
        usage = reply.get("usage") or {}
        self.client.ledger.usage(
            {
                "stage": STAGE,
                "prompt_id": self.prompt.id,
                "model": str(reply.get("model") or self.settings.model),
                "tokens_in": int(usage.get("input_tokens", 0) or 0),
                "tokens_out": int(usage.get("output_tokens", 0) or 0),
                "cost_usd": cost,
                "kind": "api",
                "cached": cached,
                "tag": tag,
                "outcome": outcome,
            }
        )

    def _log(self, tag: str, sha256: str, body: dict, reply: dict) -> None:
        """Into the invocation's log, tagged with the stage (ytlm.llm)."""
        self.client.log_block(
            STAGE,
            f"{tag} payload={sha256[:12]}",
            json.dumps(body, ensure_ascii=False),
            json.dumps(reply, ensure_ascii=False),
        )


def _check(
    reply: dict,
    offered_candidates: list[str],
    tag: str,
    validate: Callable[[dict], None],
    judge: str,
) -> None:
    """The reply matches its schema and answers inside the card's candidates."""
    try:
        validate(reply)
    except ValueError as e:
        raise ArbiterError(f"{tag}: {e}") from e
    answer = reply["answers"][QUESTION]
    if answer["choice"] not in offered_candidates or not set(answer["probabilities"]) <= set(
        offered_candidates
    ):
        raise ArbiterError(f"{tag}: {judge} answered outside the card's candidates")
