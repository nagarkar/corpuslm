"""Tests for the ledgers (module corpuslm.ledger); moved from ytlm.

Written from the requirement and the public interface only (metalm
testing.md, "Anti-gaming" (c)); no implementation file was read.

Sources:
- ytlm's design rules, paragraph "Three ledgers, one architecture":
  JSONL under logs/, one line per event, rotated at 5 MB with two
  generations kept, written best-effort so a ledger failure never fails a
  run, each with an off-switch under [logs]; db-perf off by default.
- src/corpuslm/schemas/usage_line.json and trace_line.json (the line schemas).
- metalm coding.md, "Failure" (fail loudly on our own bad data) and
  "Idempotence and writes" (validate every artifact at write and at read).
- metalm operations.md, "Scheduled and unattended work".

Expected values are derived by hand from those documents.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from corpuslm import ledger, schemas


def usage_event(**over):
    e = {
        "stage": "cleanup",
        "prompt_id": "cleanup",
        "model": "gemini/gemini-2.5-flash",
        "tokens_in": 1200,
        "tokens_out": 300,
        "cost_usd": 0.0012,
        "kind": "api",
        "cached": False,
        "tag": "vidAAAAAAA1 batch 1",
        "outcome": "ok",
    }
    e.update(over)
    return e


def trace_event(**over):
    e = {
        "argv": ["run", "--now"],
        "client": "cli",
        "workspace": "/Volumes/X6/ytlm",
        "seconds": 1.5,
        "exit_code": 0,
    }
    e.update(over)
    return e


def lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# 1. One JSON line per event, with `at`; off-switches --------------------------


def test_the_rotation_size_generations_and_defaults_are_the_documented_ones():
    # "rotated at 5 MB with two generations kept"; db-perf "off by default"
    assert ledger.ROTATE_BYTES == 5 * 1024 * 1024
    assert ledger.GENERATIONS == 2
    assert ledger.DEFAULTS == {"usage": True, "trace": True, "db_perf": False}


def test_usage_and_trace_write_one_line_each_with_at(tmp_path):
    # ytlm's design rules: "JSONL files under logs/ ... one line per event";
    # schemas require `at` (ISO, UTC)
    d = tmp_path / "logs"
    led = ledger.Ledger(d)
    led.usage(usage_event())
    led.usage(usage_event(kind="chat", cost_usd=None, cached=True))
    led.trace(trace_event())
    u = lines(d / "usage.jsonl")
    t = lines(d / "trace.jsonl")
    assert len(u) == 2 and len(t) == 1
    for row in u + t:
        assert isinstance(row["at"], str)
        datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
    assert {k: v for k, v in u[0].items() if k != "at"} == usage_event()
    assert u[1]["cost_usd"] is None and u[1]["kind"] == "chat"
    assert {k: v for k, v in t[0].items() if k != "at"} == trace_event()
    # each line is a standalone JSON document
    raw = (d / "usage.jsonl").read_text(encoding="utf-8")
    assert raw.count("\n") == 2


def test_db_perf_off_by_default_writes_nothing(tmp_path):
    # "db-perf.jsonl ... off by default ([logs].db_perf, currently false)"
    d = tmp_path / "logs"
    led = ledger.Ledger(d)
    led.usage(usage_event())
    led.trace(trace_event())
    assert not (d / "db-perf.jsonl").exists()


@pytest.mark.parametrize("off", ["usage", "trace"])
def test_section_switched_off_writes_nothing(tmp_path, off):
    # "each with an off-switch under a [logs] section in config.toml"
    d = tmp_path / "logs"
    led = ledger.Ledger(d, {off: False})
    led.usage(usage_event())
    led.trace(trace_event())
    on = "trace" if off == "usage" else "usage"
    assert not (d / f"{off}.jsonl").exists()
    assert len(lines(d / f"{on}.jsonl")) == 1


# 2. Rotation -------------------------------------------------------------------


def test_rotation_keeps_two_generations_and_never_more(tmp_path):
    # "rotated at 5 MB with two generations kept"
    d = tmp_path / "logs"
    d.mkdir()
    led = ledger.Ledger(d)
    live = d / "usage.jsonl"
    filler = "x" * ledger.ROTATE_BYTES
    for i in range(4):
        # a full live file, marked with its round number on line one
        live.write_text(json.dumps({"marker": i}) + "\n" + filler + "\n", encoding="utf-8")
        led.usage(usage_event(tag=f"round {i}"))
    rotated = sorted(p for p in d.iterdir() if p.name.startswith("usage") and p != live)
    assert len(rotated) == ledger.GENERATIONS  # .1 and .2, never a third
    markers = set()
    for p in rotated:
        first = p.read_text(encoding="utf-8").splitlines()[0]
        markers.add(json.loads(first)["marker"])
    assert markers == {2, 3}  # the two newest generations kept
    names = {p.name for p in rotated}
    assert names == {"usage.jsonl.1", "usage.jsonl.2"}
    newest = json.loads((d / "usage.jsonl.1").read_text(encoding="utf-8").splitlines()[0])
    assert newest["marker"] == 3
    # the live file stays under the limit after rotation
    assert (not live.exists()) or live.stat().st_size < ledger.ROTATE_BYTES


def test_no_rotation_below_limit(tmp_path):
    d = tmp_path / "logs"
    led = ledger.Ledger(d)
    for _ in range(5):
        led.usage(usage_event())
    assert sorted(p.name for p in d.iterdir()) == ["usage.jsonl"]


# 3. Best effort --------------------------------------------------------------


def test_unwritable_directory_does_not_raise(tmp_path):
    # "written best-effort so a ledger failure never fails a run"
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a regular file", encoding="utf-8")
    led = ledger.Ledger(blocker / "logs")
    led.usage(usage_event())
    led.trace(trace_event())
    assert blocker.read_text(encoding="utf-8") == "a regular file"


# 4. A malformed event is our bug, not best effort ------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        usage_event(kind="bogus"),  # enum api | chat
        usage_event(tokens_in=-1),  # minimum 0
        {k: v for k, v in usage_event().items() if k != "model"},  # required
        usage_event(extra_field=1),  # additionalProperties false
    ],
)
def test_malformed_usage_event_raises(tmp_path, bad):
    # coding.md "Failure"/"validate every artifact at write"; the line schema rejects it
    d = tmp_path / "logs"
    led = ledger.Ledger(d)
    with pytest.raises(schemas.SchemaError):
        led.usage(bad)
    assert not (d / "usage.jsonl").exists() or lines(d / "usage.jsonl") == []


def test_malformed_trace_event_raises(tmp_path):
    d = tmp_path / "logs"
    led = ledger.Ledger(d)
    with pytest.raises(schemas.SchemaError):
        led.trace(trace_event(argv="run --now"))  # argv must be an array
    assert not (d / "trace.jsonl").exists() or lines(d / "trace.jsonl") == []


# 5. read() -------------------------------------------------------------------


def test_read_missing_file_is_empty(tmp_path):
    assert ledger.read(tmp_path / "nope.jsonl") == []


def test_read_skips_and_counts_malformed_lines(tmp_path, capsys):
    # read(): "a malformed line is skipped, never raised, so a damaged ledger
    # still reads, and the number skipped is printed as one note on stderr"
    d = tmp_path / "logs"
    led = ledger.Ledger(d)
    led.usage(usage_event(tag="first"))
    led.usage(usage_event(tag="second"))
    p = d / "usage.jsonl"
    good = p.read_text(encoding="utf-8").splitlines()
    damaged = [
        good[0],
        "{this is not json",
        json.dumps({"at": "2026-10-03T00:00:00Z", "stage": "x"}),  # fails the line schema
        good[1],
    ]
    p.write_text("\n".join(damaged) + "\n", encoding="utf-8")
    out = ledger.read(p)
    assert [r["tag"] for r in out] == ["first", "second"]
    err = capsys.readouterr().err
    assert "2 malformed" in err and "usage.jsonl" in err


def test_read_returns_what_was_written(tmp_path, capsys):
    d = tmp_path / "logs"
    led = ledger.Ledger(d)
    led.trace(trace_event(exit_code=1))
    out = ledger.read(d / "trace.jsonl")
    assert len(out) == 1
    assert out[0]["exit_code"] == 1 and out[0]["argv"] == ["run", "--now"]
    assert "malformed" not in capsys.readouterr().err
