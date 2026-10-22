"""The judge (`src/corpuslm/judge.py`): typed questions to Jev through an injected transport,
cached by payload, billed through the client, off without the key. No service is ever called."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from corpuslm import judge, llm


class RecordingTransport:
    name = "recording"

    def __init__(self, replies: list[dict] | None = None, error: Exception | None = None):
        self.replies, self.error, self.bodies = list(replies or []), error, []

    def send(self, body, *, tag):
        self.bodies.append((tag, body))
        if self.error:
            raise self.error
        return self.replies.pop(0)


def choice_reply(choice: str, probabilities: dict) -> dict:
    return {
        "model": "jev-1",
        "answers": {"form": {"type": "choice", "choice": choice, "probabilities": probabilities}},
        "usage": {"input_tokens": 1000, "output_tokens": 5},
    }


def noul_reply(value: float, question: str = "acted_on") -> dict:
    return {
        "model": "jev-1",
        "answers": {question: {"type": "noul", "noul": value}},
        "usage": {"input_tokens": 500, "output_tokens": 2},
    }


def client(tmp_path, caps: dict | None = None) -> llm.Client:
    return llm.Client(tmp_path, {"llm": caps or {}}, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))


def test_without_the_key_the_judge_is_off_and_says_why(tmp_path, monkeypatch):
    monkeypatch.delenv(judge.KEY_ENV, raising=False)
    made, reason = judge.make({}, client(tmp_path), transport=judge.HttpTransport())
    assert made is None and reason == "TYPESAFE_API_KEY is not set in .env"
    made, reason = judge.make({"jev": {"model": "nobody"}}, client(tmp_path), RecordingTransport())
    assert made is None and "no price row" in str(reason)
    made, reason = judge.make({}, client(tmp_path), RecordingTransport())
    assert made is not None and reason is None and made.settings.model == judge.DEFAULT_MODEL


def test_a_reply_is_cached_by_payload_and_a_bad_one_is_not(tmp_path):
    transport = RecordingTransport([noul_reply(0.8), {"model": "jev-1", "answers": {}}])
    made, _ = judge.make({}, client(tmp_path), transport)
    assert made is not None
    probability, call = made.yes({"utterance": "call her"}, "acted_on", "Acted on?", tag="t1")
    assert probability == 0.8 and not call["cached"] and call["cost_usd"] == 0.000021
    assert made.client.spent_usd == 0.000021 and made.client.calls == 1
    again, call = made.yes({"utterance": "call her"}, "acted_on", "Acted on?", tag="t1")
    assert again == 0.8 and call["cached"] and call["cost_usd"] == 0.0
    assert len(transport.bodies) == 1  # served from the cache
    with pytest.raises(judge.JudgeError):
        made.yes({"utterance": "other"}, "acted_on", "Acted on?", tag="t2")
    assert len(list(made.client.cache_dir.glob("*.json"))) == 1  # the bad reply was not cached
    lines = [
        json.loads(line)
        for line in (tmp_path / llm.LOGS_DIR).glob("**/*usage*")
        for line in line.read_text().splitlines()
    ]
    assert [line["outcome"] for line in lines] == ["ok", "ok", "bad_reply"]


def test_a_choice_outside_the_options_is_declined(tmp_path):
    transport = RecordingTransport([choice_reply("song", {"song": 1.0})])
    made, _ = judge.make({}, client(tmp_path), transport)
    assert made is not None
    with pytest.raises(judge.JudgeError, match="outside the options"):
        made.choose({}, "form", "Which?", {"monologue": "", "dialogue": ""}, tag="t")
    transport.replies = [choice_reply("dialogue", {"dialogue": 0.7, "monologue": 0.3})]
    choice, probabilities, _ = made.choose(
        {}, "form", "Which?", {"monologue": "", "dialogue": ""}, tag="t"
    )
    assert choice == "dialogue" and probabilities == {"monologue": 0.3, "dialogue": 0.7}
    assert transport.bodies[-1][1]["questions"]["form"]["type"] == "choice"


def test_the_spend_cap_stops_a_question_before_it_is_sent(tmp_path):
    transport = RecordingTransport([noul_reply(0.9)])
    made, _ = judge.make({}, client(tmp_path, {"max_usd_per_run": 0.0}), transport)
    assert made is not None
    with pytest.raises(llm.SpendCapReached):
        made.yes({}, "acted_on", "Acted on?", tag="t")
    assert transport.bodies == []


def test_the_http_transport_retries_only_rate_limits(monkeypatch, capsys):
    monkeypatch.setenv(judge.KEY_ENV, "k")
    statuses = [429, 200]
    seen = []

    def post(url, body, headers, timeout):
        seen.append((url, headers["Authorization"]))
        status = statuses.pop(0)
        return status, {"retry-after": "0.5"}, json.dumps(noul_reply(0.5)) if status == 200 else ""

    sleeps = []
    transport = judge.HttpTransport(post=post, sleep=sleeps.append)
    assert transport.send({"q": 1}, tag="t")["answers"]["acted_on"]["noul"] == 0.5
    assert sleeps == [0.5] and seen[0] == (judge.ENDPOINT, "Bearer k")
    assert "429" in capsys.readouterr().err
    statuses[:] = [400]
    with pytest.raises(judge.JudgeError, match="HTTP 400"):
        transport.send({"q": 1}, tag="t")
    statuses[:] = [503]
    with pytest.raises(llm.VendorError, match="HTTP 503"):
        transport.send({"q": 1}, tag="t")
    statuses[:] = [429, 429, 429]
    with pytest.raises(llm.VendorError, match="HTTP 429"):
        transport.send({"q": 1}, tag="t")


def test_the_scripted_transport_reads_the_jev_folder(tmp_path):
    folder = tmp_path / "jev"
    folder.mkdir()
    (folder / "bee:1-form.json").write_text(json.dumps(choice_reply("media", {"media": 1.0})))
    transport = judge.ScriptedTransport(tmp_path)
    assert transport.send({}, tag="bee:1-form")["answers"]["form"]["choice"] == "media"
    with pytest.raises(llm.NotConfigured):
        transport.send({}, tag="missing")
