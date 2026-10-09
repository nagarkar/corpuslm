"""The arbiter (`src/corpuslm/arbiter.py`): a judge's second opinion on pending ambiguities,
asked through an injected transport, cached by payload, applied only above the threshold.
Sanitized synthetic text only; no judge is ever called."""

import json
from pathlib import Path

import pytest

from corpuslm import arbiter, llm, review
from corpuslm.prompts import Prompt

JUDGE_MODEL = "jev-latest"


class RecordingTransport:
    name = "recording"

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.bodies = reply, error, []

    def send(self, body, *, tag):
        self.bodies.append((tag, body))
        if self.error:
            raise self.error
        return self.reply


def prompt(tmp_path):
    path = tmp_path / "arbiter_v1.md"
    path.write_text(
        '---\nid = "arbiter"\nversion = 1\nstage = "arbiter"\noutput_schema = "none"\n'
        'config_section = "arbiter"\n---\nPick the reading.\n'
    )
    return Prompt("arbiter", 1, "arbiter", "none", "arbiter", "Pick the reading.\n", path, "f" * 64)


def reply(choice, p, other):
    return {
        "model": "jev-1",
        "answers": {
            "reading": {
                "type": "choice",
                "choice": choice,
                "probabilities": {choice: p, other: round(1 - p, 6)},
            }
        },
        "usage": {"input_tokens": 400, "output_tokens": 20},
    }


def document():
    return {
        "segments": [{"idx": 0, "t0": 0, "t1": 1, "text": "he said these guys"}],
        "ambiguities": [
            {
                "key": "amb-0001",
                "idx": 0,
                "span": "these guys",
                "candidates": ["these guys", "the skys"],
                "pick": "these guys",
                "probabilities": [0.6, 0.4],
                "status": "pending",
            }
        ],
    }


def validate(reply_body):
    if "answers" not in reply_body:
        raise ValueError("no answers")


def make(tmp_path, transport, *, min_probability=0.8, config=None):
    client = llm.Client(tmp_path, config or {"llm": {}, "logs": {}})
    return arbiter.Arbiter(
        client,
        arbiter.Settings(True, JUDGE_MODEL, min_probability),
        transport,
        prompt(tmp_path),
        validate,
    )


def test_the_arbiter_asks_through_the_transport_it_is_given(tmp_path):
    transport = RecordingTransport(reply("the skys", 0.97, "these guys"))
    stats = make(tmp_path, transport).run(document(), source="s1", protected=[], memory={})
    assert [tag for tag, _ in transport.bodies] == ["s1-amb-0001"]
    assert stats["asked"] == 1 and stats["error"] == ""
    body = transport.bodies[0][1]
    assert body["model"] == JUDGE_MODEL
    assert list(body["questions"]["reading"]["criteria"]) == [
        "these guys",
        "the skys",
    ]


def test_an_older_item_with_one_confidence_spreads_the_rest_evenly():
    a = {"span": "heard it", "candidates": ["A", "B"], "pick": "A", "confidence": 0.7}
    opts = review.reading_options(a)
    assert opts == ["A", "B", "heard it"]
    assert arbiter.clean_up_probabilities(a, opts) == pytest.approx(
        {"A": 0.7, "B": 0.15, "heard it": 0.15}
    )


def test_a_pick_that_clears_the_threshold_is_applied_with_an_arbiter_origin(tmp_path):
    t = document()
    transport = RecordingTransport(reply("the skys", 0.97, "these guys"))
    stats = make(tmp_path, transport).run(t, source="s1", protected=[], memory={})
    a = t["ambiguities"][0]
    assert a["status"] == "auto_resolved" and a["resolution"] == "the skys"
    assert a["origin"].startswith("arbiter jev-1 ")
    assert t["segments"][0]["text"] == "he said the skys"
    assert stats["applied"] == 1 and stats["overrode"] == 1


def test_a_pick_below_the_threshold_stays_pending(tmp_path):
    t = document()
    t["ambiguities"][0]["probabilities"] = [0.9, 0.1]
    transport = RecordingTransport(reply("the skys", 0.95, "these guys"))
    stats = make(tmp_path, transport).run(t, source="s1", protected=[], memory={})
    assert t["ambiguities"][0]["status"] == "pending"
    assert stats["deferred"] == 1 and stats["applied"] == 0


def test_the_same_question_is_served_from_the_cache_the_second_time(tmp_path):
    transport = RecordingTransport(reply("the skys", 0.97, "these guys"))
    make(tmp_path, transport).run(document(), source="s1", protected=[], memory={})
    stats = make(tmp_path, transport).run(document(), source="s1", protected=[], memory={})
    assert len(transport.bodies) == 1 and stats["cached"] == 1


def test_a_bad_reply_is_never_cached_and_skips_only_its_item(tmp_path):
    t = document()
    t["ambiguities"].append(dict(t["ambiguities"][0], key="amb-0002"))
    transport = RecordingTransport({"model": "jev-1", "usage": {}})
    stats = make(tmp_path, transport).run(t, source="s1", protected=[], memory={})
    assert stats["bad"] == 2 and stats["asked"] == 0
    assert not list((Path(tmp_path) / llm.CACHE_DIR).glob("*.json"))
    assert [a["status"] for a in t["ambiguities"]] == ["pending", "pending"]


def test_a_reply_outside_the_cards_options_is_a_bad_reply(tmp_path):
    transport = RecordingTransport(reply("somewhere else", 0.97, "these guys"))
    stats = make(tmp_path, transport).run(document(), source="s1", protected=[], memory={})
    assert stats["bad"] == 1 and "outside" in stats["error"]


def test_the_spend_cap_stops_the_arbiter_and_leaves_the_rest_pending(tmp_path):
    t = document()
    t["ambiguities"].append(dict(t["ambiguities"][0], key="amb-0002"))
    transport = RecordingTransport(reply("the skys", 0.97, "these guys"))
    arb = make(tmp_path, transport, config={"llm": {"max_usd_per_run": 0.0001}, "logs": {}})
    arb.client.spent_usd = 1.0
    stats = arb.run(t, source="s1", protected=[], memory={})
    assert stats["budget"] and stats["cap"] == llm.RUN_CAP
    assert transport.bodies == []
    assert [a["status"] for a in t["ambiguities"]] == ["pending", "pending"]


def test_off_reasons_name_what_to_fix():
    transport = RecordingTransport()
    transport.name = "typesafe"
    on = arbiter.Settings(True, JUDGE_MODEL, 0.8)
    assert arbiter.off_reason(None, transport, key_env="KEY").startswith("[arbiter] is absent")
    assert "enabled is false" in arbiter.off_reason(
        arbiter.Settings(False, JUDGE_MODEL, 0.8), transport, key_env="KEY"
    )
    assert "model is not set" in arbiter.off_reason(
        arbiter.Settings(True, "", 0.8), transport, key_env="KEY"
    )
    assert "no price row" in arbiter.off_reason(
        arbiter.Settings(True, "nobody-9", 0.8), transport, key_env="KEY"
    )
    assert arbiter.off_reason(on, transport, key_env="KEY_NOT_SET_ANYWHERE") == (
        "KEY_NOT_SET_ANYWHERE is not set in .env"
    )
    assert arbiter.off_reason(on, RecordingTransport(), key_env="KEY") is None


def test_the_scripted_transport_reads_one_file_per_tag(tmp_path):
    (tmp_path / arbiter.STAGE).mkdir()
    (tmp_path / arbiter.STAGE / "s1-amb-0001.json").write_text(json.dumps({"ok": True}))
    transport = arbiter.ScriptedTransport(tmp_path)
    assert transport.send({}, tag="s1-amb-0001") == {"ok": True}
    with pytest.raises(llm.NotConfigured):
        transport.send({}, tag="missing")
