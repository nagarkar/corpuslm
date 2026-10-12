"""`corpuslm help` and `corpuslm doctor` (docs/design/surfaces.md)."""

import json
import sqlite3

import pytest

from corpuslm import cli, db


def _board(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_help_lists_every_verb_with_an_example(capsys):
    assert cli.main(["help", "--json"]) == 0
    verbs = _board(capsys)["verbs"]
    assert set(verbs) == set(cli.VERBS)
    assert all(
        "Example: " in text and "corpuslm" in text.split("Example: ")[1] for text in verbs.values()
    )


def test_doctor_reports_a_current_database_and_changes_nothing(tmp_path, capsys):
    path = tmp_path / "transcript.sqlite"
    db.Database(path).close()
    before = path.read_bytes()
    assert cli.main(["--json", "doctor", str(path)]) == 0
    board = _board(capsys)
    assert board["ok"] and board["problems"] == []
    assert board["user_version"] == db.USER_VERSION
    assert board["integrity_check"] == "ok"
    assert board["tables"]["sources"] == 0
    assert path.read_bytes() == before


def test_doctor_on_a_missing_file_fails_loudly_and_creates_nothing(tmp_path, capsys):
    path = tmp_path / "nope.sqlite"
    assert cli.main(["doctor", str(path), "--json"]) == 1
    assert "does not exist" in _board(capsys)["error"]
    assert not path.exists()


def test_doctor_names_a_newer_schema(tmp_path, capsys):
    path = tmp_path / "transcript.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {db.USER_VERSION + 1}")
    connection.close()
    assert cli.main(["doctor", str(path), "--json"]) == 1
    assert "newer than this corpuslm" in _board(capsys)["problems"][0]


def test_an_unknown_verb_exits_one_and_points_at_help(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["frobnicate"])
    assert stopped.value.code == 1
    assert "corpuslm help" in capsys.readouterr().err


def test_usage_show_and_serve_dry_run_read_the_settings_file(tmp_path, capsys):
    from datetime import UTC, datetime

    from corpuslm import ledger

    ledger.Ledger(tmp_path / "logs", clock=lambda: datetime(2026, 10, 5, tzinfo=UTC)).usage(
        {
            "stage": "cleanup", "prompt_id": "cleanup", "model": "m", "tokens_in": 1,
            "tokens_out": 2, "cost_usd": 0.5, "kind": "api", "cached": False, "tag": "t",
            "outcome": "ok",
        }
    )  # fmt: skip
    db.Database(tmp_path / "transcript.sqlite").close()
    path = tmp_path / "review.toml"
    path.write_text(
        f'database = "{tmp_path / "transcript.sqlite"}"\n'
        f'[kinds.youtube]\ndirectory = "{tmp_path}"\n'
        f'[usage]\nport = 8790\n[usage.ledgers]\nytlm = "{tmp_path / "logs"}"\n'
    )
    assert cli.main(["usage", "show", "--config", str(path), "--month", "2026-10", "--json"]) == 0
    board = _board(capsys)
    assert board["model_spend"]["tools"]["ytlm"]["total"]["cost_usd"] == 0.5
    assert cli.main(["usage", "show", "--config", str(path), "--month", "2026-10"]) == 0
    assert "ytlm: $0.50, 1 calls" in capsys.readouterr().out
    assert cli.main(["usage", "show", "--config", str(path), "--month", "October", "--json"]) == 1
    assert "--month must look like" in _board(capsys)["error"]
    assert cli.main(["usage", "serve", "--config", str(path), "--dry-run", "--json"]) == 0
    assert _board(capsys)["url"] == "http://127.0.0.1:8790/"


def test_usage_verbs_without_a_usage_table_say_so(tmp_path, capsys):
    path = tmp_path / "review.toml"
    path.write_text(
        f'database = "{tmp_path}/t.sqlite"\n[kinds.youtube]\ndirectory = "{tmp_path}"\n'
    )
    assert cli.main(["usage", "serve", "--config", str(path), "--dry-run", "--json"]) == 1
    assert "no [usage] table" in _board(capsys)["error"]
