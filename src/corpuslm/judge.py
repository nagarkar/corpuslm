"""Judge: typed questions to Jev (TypeSafe's System One model), cached and billed like the rest.

The arbiter (corpuslm.arbiter) asks Jev about the ambiguities of the AI cleanup through a
transport its tool injects. corpuslm's own verbs have questions of their own: the form of a
conversation (corpuslm.form) and whether a monologue's imperative is an instruction the owner
wants acted on (corpuslm.directives). This module is their one road to Jev: a `Judge` that sends
one System One request (a `choice` or a `noul` question over a state), caches every reply by
payload in the model client's cache, counts the spend against the client's caps through its
price table, and writes the ledger line, so a rebuild never re-bills. The HTTP transport lives
here too (`HttpTransport`, standard library only), chosen when the suite set no scripted
directory.

Never imports: db, store, cli, clikit, any vendor SDK.

Decisions:
- One key for every Jev question in corpuslm: `TYPESAFE_API_KEY` in the environment (or the
  caller's `.env`, read as the other pointers are). Without it the judge is off, the caller's
  own rule stands and its board says so; nothing fails. Enforced by:
  tests/test_judge.py::test_without_the_key_the_judge_is_off_and_says_why.
- Replies are cached by payload and never re-billed; a reply that fails its check is never
  cached. Enforced by: tests/test_judge.py::test_a_reply_is_cached_by_payload_and_a_bad_one_is_not.
- Only a 429 or 529 is retried, bounded; any other failure fails the question. Enforced by:
  tests/test_judge.py::test_the_http_transport_retries_only_rate_limits.

Terms:
- **judge**: Jev, asked a typed question whose answer code consumes: a `choice` among named
  options with a probability each, or a `noul`, the probability that a yes/no question is yes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from llmlm import llm

from . import clock, files

STAGE = "jev"
KEY_ENV = "TYPESAFE_API_KEY"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


class JudgeError(RuntimeError):
    """The judge's reply was unusable: the question is asked again next run, never cached."""


@dataclass(frozen=True, slots=True)
class Settings:
    model: str


def settings(config: dict) -> Settings:
    """`[jev]` of the settings file: the model (default `jev-latest`). The section is optional."""
    section = config.get("jev") or {}
    return Settings(model=str(section.get("model") or DEFAULT_MODEL))


# ---- transports -------------------------------------------------------------


class Transport(Protocol):
    name: str

    def send(self, body: dict, *, tag: str) -> dict: ...


class ScriptedTransport:
    """docs/design/testing.md: `<dir>/jev/<tag>.json` holds a TypeSafe response body."""

    name = "scripted"

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def send(self, body: dict, *, tag: str) -> dict:
        path = self.directory / STAGE / f"{tag}.json"
        if not path.exists():
            raise llm.NotConfigured(f"scripted transport: no reply at {path}")
        return json.loads(path.read_text(encoding="utf-8"))


class HttpTransport:
    """One POST per question to TypeSafe; only a 429 or 529 is retried (at most
    `llm.MAX_ATTEMPTS` attempts, honouring `retry-after`), every retry said on stderr, any other
    failure fails the question. `post` and `sleep` are injectable so the rule is tested offline;
    `post(url, body, headers, timeout)` returns `(status, headers, text)`."""

    name = "typesafe"

    def __init__(
        self, *, post: Callable | None = None, sleep: Callable[[float], None] = time.sleep
    ):
        self._post = post or _urllib_post
        self._sleep = sleep

    def send(self, body: dict, *, tag: str) -> dict:
        headers = {
            "Authorization": f"Bearer {os.environ.get(KEY_ENV, '')}",
            "Content-Type": "application/json",
        }
        for attempt in range(1, llm.MAX_ATTEMPTS + 1):
            try:
                status, reply_headers, text = self._post(
                    ENDPOINT, body, headers, llm.REQUEST_TIMEOUT_S
                )
            except OSError as error:
                raise llm.VendorError(f"typesafe: {type(error).__name__} for {tag}") from error
            if status in llm.RETRY_STATUSES:
                if attempt == llm.MAX_ATTEMPTS:
                    raise llm.VendorError(f"typesafe: HTTP {status} for {tag}")
                wait_header = str(reply_headers.get("retry-after", ""))
                wait = (
                    float(wait_header)
                    if wait_header.replace(".", "", 1).isdigit()
                    else llm.RETRY_SLEEP_S * attempt
                )
                llm.say_retry("typesafe", status, tag, attempt, wait)
                self._sleep(wait)
                continue
            if status >= 500:
                raise llm.VendorError(f"typesafe: HTTP {status} for {tag}")
            if status != 200:
                # the body is not echoed: it may carry the request
                raise JudgeError(f"typesafe: HTTP {status} for {tag}")
            try:
                return json.loads(text)
            except ValueError as error:
                raise JudgeError(f"typesafe: reply for {tag} is not JSON") from error
        raise llm.VendorError(f"typesafe: no reply for {tag}")


def _urllib_post(url: str, body: dict, headers: dict, timeout: float) -> tuple[int, dict, str]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read().decode("utf-8", "replace")


def default_transport() -> Transport:
    """Scripted when the suite set `llmlm.llm.SCRIPTS`, else TypeSafe over HTTP."""
    scripts = llm.SCRIPTS
    return ScriptedTransport(scripts) if scripts is not None else HttpTransport()


def off_reason(found: Settings, transport: Transport) -> str | None:
    """Why the judge is off, or None."""
    if llm.price_usd(found.model, 0, 0) is None:
        return f"{found.model} has no price row, so the spend cap cannot be enforced"
    if transport.name == "typesafe" and not os.environ.get(KEY_ENV):
        return f"{KEY_ENV} is not set in .env"
    return None


# ---- the judge --------------------------------------------------------------


class Judge:
    def __init__(self, client: llm.Client, found: Settings, transport: Transport):
        self.client = client
        self.settings = found
        self.transport = transport

    def fingerprint(self) -> dict:
        return {"model": self.settings.model}

    def choose(
        self,
        state: Any,
        question: str,
        instructions: Any,
        criteria: dict[str, Any],
        *,
        tag: str,
    ) -> tuple[str, dict[str, float], dict]:
        """One `choice` question: (the pick, probability per option, the call record)."""
        body = {
            "model": self.settings.model,
            "state": state,
            "questions": {
                question: {"type": "choice", "instructions": instructions, "criteria": criteria}
            },
        }

        def check(reply: dict) -> None:
            answer = _answer(reply, question, tag)
            if answer.get("choice") not in criteria or not isinstance(
                answer.get("probabilities"), dict
            ):
                raise JudgeError(f"{tag}: the judge answered outside the options")

        reply, call = self.ask(body, tag=tag, check=check)
        answer = reply["answers"][question]
        probabilities = {
            option: float(answer["probabilities"].get(option, 0.0)) for option in criteria
        }
        return str(answer["choice"]), probabilities, call

    def yes(
        self,
        state: Any,
        question: str,
        instructions: Any,
        criteria: dict[str, str] | None = None,
        *,
        tag: str,
    ) -> tuple[float, dict]:
        """One `noul` question: (the probability that the answer is yes, the call record)."""
        asked: dict = {"type": "noul", "instructions": instructions}
        if criteria:
            asked["criteria"] = criteria
        body = {"model": self.settings.model, "state": state, "questions": {question: asked}}

        def check(reply: dict) -> None:
            answer = _answer(reply, question, tag)
            value = answer.get("noul")
            if not isinstance(value, int | float) or not 0 <= float(value) <= 1:
                raise JudgeError(f"{tag}: the judge gave no probability")

        reply, call = self.ask(body, tag=tag, check=check)
        return float(reply["answers"][question]["noul"]), call

    def ask(self, body: dict, *, tag: str, check: Callable[[dict], None]) -> tuple[dict, dict]:
        """One request: from the cache, else from the judge. `check` runs before the reply is
        cached, so a bad reply is never served again."""
        key = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        sha256 = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self.client.cache_dir / f"{sha256}.json"
        requested_at = clock.iso(self.client.clock())
        if path.exists():
            reply = json.loads(path.read_text(encoding="utf-8"))["response"]
            check(reply)
            self._ledger(reply, tag, cost=0.0, cached=True)
            return reply, _call(tag, sha256, requested_at, cached=True, cost=0.0)
        client = self.client
        client.check_spend_cap(STAGE, tag)
        reply = self.transport.send(body, tag=tag)
        client.log_block(
            STAGE,
            f"{tag} payload={sha256[:12]}",
            json.dumps(body, ensure_ascii=False),
            json.dumps(reply, ensure_ascii=False),
        )
        client.calls += 1
        tokens_in = int((reply.get("usage") or {}).get("input_tokens", 0) or 0)
        cost = llm.price_usd(self.settings.model, tokens_in, 0)  # null when unknown
        client.spent_usd += cost or 0.0
        try:
            check(reply)
        except JudgeError:
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
        return reply, _call(tag, sha256, requested_at, cached=False, cost=cost)

    def _ledger(
        self, reply: dict, tag: str, *, cost: float | None, cached: bool, outcome: str = "ok"
    ) -> None:
        usage = reply.get("usage") or {}
        self.client.ledger.usage(
            {
                "stage": STAGE,
                "prompt_id": STAGE,
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


def _call(tag: str, sha256: str, requested_at: str, *, cached: bool, cost: float | None) -> dict:
    return {
        "tag": tag,
        "payload_sha256": sha256,
        "requested_at": requested_at,
        "cached": cached,
        "cost_usd": cost,
    }


def _answer(reply: dict, question: str, tag: str) -> dict:
    answers = reply.get("answers") if isinstance(reply, dict) else None
    answer = answers.get(question) if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        raise JudgeError(f"{tag}: the judge's reply has no answer to {question!r}")
    return answer


def make(
    config: dict, client: llm.Client, transport: Transport | None = None
) -> tuple[Judge | None, str | None]:
    """The judge for this configuration, or None and why it is off."""
    found = settings(config)
    chosen = transport or default_transport()
    reason = off_reason(found, chosen)
    if reason:
        return None, reason
    return Judge(client, found, chosen), None


__all__ = [
    "DEFAULT_MODEL",
    "ENDPOINT",
    "KEY_ENV",
    "STAGE",
    "HttpTransport",
    "Judge",
    "JudgeError",
    "ScriptedTransport",
    "Settings",
    "Transport",
    "default_transport",
    "make",
    "off_reason",
    "settings",
]
