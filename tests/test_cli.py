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
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {db.USER_VERSION + 1}")
    conn.close()
    assert cli.main(["doctor", str(path), "--json"]) == 1
    assert "newer than this corpuslm" in _board(capsys)["problems"][0]


def test_an_unknown_verb_exits_one_and_points_at_help(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["frobnicate"])
    assert stopped.value.code == 1
    assert "corpuslm help" in capsys.readouterr().err
