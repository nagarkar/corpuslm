"""Vendor transports (`corpuslm.llm`; metalm
coding.md, "LLM calls"): retry only 429/529, bounded, each retry said on
stderr; any other failure fails the step at once; requests are shaped from
MODEL_PROFILES before sending, never changed after a vendor rejects them."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from corpuslm import llm


class VendorFailure(Exception):
    def __init__(self, text, status_code=None):
        super().__init__(text)
        self.status_code = status_code


def _ok():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1),
    )


def _transport(failures, sleeps):
    calls = []

    def completion(**kw):
        calls.append(kw)
        if failures:
            raise failures.pop(0)
        return _ok()

    t = llm.LiteLLMTransport(completion=completion, sleep=sleeps.append, cost=lambda r: 0.0)
    return t, calls


@pytest.mark.parametrize(
    "failure",
    [
        VendorFailure("RateLimitError: 429 RESOURCE_EXHAUSTED", 429),
        VendorFailure("OverloadedError: overloaded", 529),
    ],
)
def test_a_429_or_529_is_retried_and_says_so_on_stderr(failure, capsys):
    sleeps = []
    t, calls = _transport([failure], sleeps)
    raw = t.send("gemini/gemini-3.1-flash-lite", [], {}, tag="v-b0")
    assert raw.text == "{}" and len(calls) == 2 and len(sleeps) == 1
    err = capsys.readouterr().err
    assert "retry" in err and "v-b0" in err and str(failure.status_code) in err


@pytest.mark.parametrize(
    "failure",
    [
        VendorFailure("InternalServerError: 500", 500),
        VendorFailure("ServiceUnavailableError: 503", 503),
        VendorFailure("Timeout: timed out"),
        VendorFailure("APIConnectionError: connection reset"),
    ],
)
def test_any_other_failure_fails_the_step_without_a_retry(failure):
    sleeps = []
    t, calls = _transport([failure], sleeps)
    with pytest.raises(llm.VendorError):
        t.send("gemini/gemini-3.1-flash-lite", [], {}, tag="v-b0")
    assert len(calls) == 1 and sleeps == []


def test_retries_are_bounded(capsys):
    sleeps = []
    t, calls = _transport([VendorFailure("429", 429) for _ in range(9)], sleeps)
    with pytest.raises(llm.VendorError, match="429"):
        t.send("gemini/gemini-3.1-flash-lite", [], {}, tag="v-b0")
    assert len(calls) == llm.MAX_ATTEMPTS and len(sleeps) == llm.MAX_ATTEMPTS - 1
    assert capsys.readouterr().err.count("retry") == llm.MAX_ATTEMPTS - 1


@pytest.mark.parametrize(
    "params, rejected, row",
    [
        (
            {"temperature": 0.2, "response_format": {"type": "json_object"}},
            "BadRequestError: temperature is not supported",
            'ModelProfile("gemini-9", VENDOR_DEFAULT_ONLY, True)',
        ),
        (
            {"temperature": 0.2, "response_format": {"type": "json_object"}},
            "BadRequestError: response_format json_object is not supported",
            'ModelProfile("gemini-9", ANY, False)',
        ),
    ],
)
def test_a_rejected_parameter_fails_the_call_naming_the_profile_row(params, rejected, row):
    """ytlm's design rules (the owner: "if temperature is not accepted
    by some models, it should not be sent"): the request is never re-shaped
    after the vendor answers. The call fails once, and the error names the
    exact MODEL_PROFILES row that would have shaped it right."""
    sleeps = []
    t, calls = _transport([VendorFailure(rejected, 400)], sleeps)
    with pytest.raises(llm.VendorError) as e:
        t.send("gemini/gemini-9", [], params, tag="v-b0")
    assert len(calls) == 1 and sleeps == []
    assert "MODEL_PROFILES" in str(e.value)
    assert f'("gemini/gemini-9", {row})' in str(e.value)


def test_the_transport_never_resends_without_a_parameter():
    source = Path(llm.__file__).read_text()
    assert "asking once without it" not in source and "once, without the parameter" not in source


def test_an_unknown_model_gets_the_plain_request(tmp_path):
    seen = []

    class Recorder:
        name = "recorder"

        def send(self, model, messages, params, *, tag):
            seen.append(params)
            return llm.RawReply('{"greeting": "hi"}', 1, 1, 0.0)

    from corpuslm import prompts

    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "greeting_v1.md").write_text(
        '---\nid = "greeting"\nversion = 1\nstage = "greet"\n'
        'output_schema = "greeting_reply"\nconfig_section = "greeting"\n---\nSay {word}.\n'
    )
    client = llm.Client(
        tmp_path,
        {"greeting": {"model": "vendor/unknown-model", "temperature": 0.7}, "logs": {}},
        transport=Recorder(),
        validator=lambda schema, obj: None,
    )
    client.complete(
        "greet", prompts.load("greeting", directories=[directory]), {"word": "x"}, tag="t"
    )
    assert seen == [{}]  # no temperature, no response_format: shaped before sending


def test_a_known_model_is_shaped_from_its_row_before_sending(tmp_path):
    t, calls = _transport([], [])
    t.send("gemini/gemini-2.5-flash", [], {"temperature": 0.2}, tag="t")
    assert calls[0]["temperature"] == 0.2 and calls[0]["timeout"] == llm.REQUEST_TIMEOUT_S


def test_a_reply_cut_at_the_token_limit_is_a_failure():
    def completion(**kw):
        reply = _ok()
        reply.choices[0].finish_reason = "length"
        return reply

    t = llm.LiteLLMTransport(completion=completion, cost=lambda r: 0.0)
    with pytest.raises(llm.TruncatedReply):
        t.send("gemini/gemini-3.1-flash-lite", [], {}, tag="t")


def test_an_unpriceable_reply_costs_null():
    def broken_price(response):
        raise KeyError("no price")

    t = llm.LiteLLMTransport(completion=lambda **kw: _ok(), cost=broken_price)
    raw = t.send("gemini/gemini-3.1-flash-lite", [], {}, tag="t")
    assert raw.cost_usd is None and raw.tokens_in == 3 and raw.tokens_out == 1
