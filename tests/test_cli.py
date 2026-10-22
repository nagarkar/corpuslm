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

    from llmlm import ledger

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


# ---- the claim verbs (corpuslm.claims) -----------------------------------------------------

SEGMENTS = [
    {"idx": index, "t0": float(index), "t1": float(index + 1), "speaker": "Quill", "text": text}
    for index, text in enumerate(["a choice is right as of now", "so you choose again"])
]


def _claims_world(tmp_path, *, claims_table: bool = True) -> str:
    from corpuslm.store import Store

    database = db.Database(tmp_path / "transcript.sqlite")
    store = Store(database)
    store.add_source("bee:1", "l", created_by="test", kind="bee")
    store.replace_segments("bee:1", SEGMENTS)
    store.add_source("Xq3vT9bLk2A", "u", created_by="test", kind="youtube")
    store.replace_segments("Xq3vT9bLk2A", SEGMENTS)
    database.close()
    (tmp_path / "videos" / "Xq3vT9bLk2A").mkdir(parents=True)
    (tmp_path / "videos" / "Xq3vT9bLk2A" / "digest.json").write_text(
        json.dumps(
            {
                "claims": [
                    {"statement": "A choice is right only now.", "speaker": "", "segments": [0],
                     "kind": "assertion"},
                ],
                "provenance": {"transcript_sha256": "t", "prompt_sha256": "p", "model": "m",
                               "section_chars": 6000},
            }
        )
    )  # fmt: skip
    path = tmp_path / "review.toml"
    path.write_text(
        f'database = "{tmp_path / "transcript.sqlite"}"\n'
        f'[kinds.youtube]\ndirectory = "{tmp_path / "videos"}"\n'
        + (
            f'[claims]\nmodel = "gemini/gemini-2.5-flash-lite"\nworkspace = "{tmp_path / "ws"}"\n'
            "[llm]\nmax_usd_per_run = 1.0\n"
            '[concepts]\nmodel = "gemini/gemini-2.5-flash-lite"\nmin_sections = 1\nmin_claims = 1\n'
            '[moments]\nmodel = "gemini/gemini-2.5-flash-lite"\n'
            '[sheet]\nmodel = "gemini/gemini-2.5-flash-lite"\n'
            '[monologue]\nmodel = "gemini/gemini-2.5-flash-lite"\nminutes = 2\n'
            if claims_table
            else ""
        )
    )
    return str(path)


def test_claims_build_dry_run_estimates_and_sends_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = _claims_world(tmp_path)
    assert cli.main(["--json", "claims", "build", "--config", config, "--dry-run"]) == 0
    board = _board(capsys)
    assert board["status"] == "dry-run" and board["sources_chosen"] == 2
    assert board["sources_to_build"] == 2 and board["calls_billed"] == 2
    assert board["estimated_usd"] > 0 and board["caps"] == {"max_usd_per_run": 1.0}
    assert not (tmp_path / "ws").exists()  # no cache, no ledger: nothing was sent
    assert cli.main(["--json", "claims", "build", "--config", config, "--source", "bee:1"]) == 1
    assert "GEMINI_API_KEY is not set" in _board(capsys)["error"]  # offline: the call is off
    assert cli.main(["--json", "claims", "build", "--config", config, "--source", "nope"]) == 1
    assert "unknown source: nope" in _board(capsys)["error"]


def test_claims_build_says_whether_jev_is_on_and_where_events_go(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    config = _claims_world(tmp_path)
    assert cli.main(["--json", "claims", "build", "--config", config, "--dry-run"]) == 0
    board = _board(capsys)
    assert board["jev"] == "off: TYPESAFE_API_KEY is not set in .env"
    assert board["forms_to_classify"] == 1  # the Bee source; the video has no form


def test_form_verbs_classify_by_count_without_jev_and_report(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    config = _claims_world(tmp_path)
    assert cli.main(["--json", "form", "status", "--config", config]) == 0
    board = _board(capsys)
    assert (board["sources"], board["unclassified"], board["disagreements"]) == (1, 1, 0)
    assert cli.main(["--json", "form", "classify", "--config", config, "--dry-run"]) == 0
    board = _board(capsys)
    assert board["status"] == "dry-run" and board["sources_to_classify"] == 1
    assert board["jev"].startswith("off:") and board["model"] == "jev-latest"
    assert cli.main(["--json", "form", "classify", "--config", config]) == 0
    board = _board(capsys)
    assert (board["classified"], board["by_label"], board["form_cards"]) == (
        1, {"monologue": 1}, 0,
    )  # fmt: skip
    assert board["cost_usd"] == 0.0
    assert cli.main(["--json", "form", "classify", "--config", config]) == 0
    assert _board(capsys)["sources_to_classify"] == 0  # classified once, left alone
    assert cli.main(["--json", "form", "classify", "--config", config, "--again"]) == 0
    assert _board(capsys)["classified"] == 1
    assert cli.main(["--json", "form", "status", "--config", config]) == 0
    board = _board(capsys)
    assert (board["by_label"], board["unclassified"], board["next"]) == (
        {"monologue": 1}, 0, "nothing to classify",
    )  # fmt: skip
    assert cli.main(["--json", "form", "classify", "--config", config, "--source", "nope"]) == 1
    assert "unknown source: nope" in _board(capsys)["error"]
    assert (
        cli.main(["--json", "form", "classify", "--config", config, "--source", "Xq3vT9bLk2A"]) == 1
    )
    assert "not a Bee source" in _board(capsys)["error"]


def test_directives_list_counts_and_names_the_page(tmp_path, capsys):
    from corpuslm.store import Store

    config = _claims_world(tmp_path)
    database = db.Database(tmp_path / "transcript.sqlite")
    Store(database).replace_claims(
        "bee:1",
        [{"section": 0, "segments": [0], "text": "Call Wren.", "kind": "directive",
          "addressee": "computer", "heard": "Computer, call Wren"},
         {"section": 0, "segments": [1], "text": "Plain.", "kind": "assertion"}],
        build_key="k",
        created_by="test",
    )  # fmt: skip
    database.close()
    assert cli.main(["--json", "directives", "list", "--config", config]) == 0
    board = _board(capsys)
    assert board["count"] == 1 and board["summary"]["states"] == {
        "confirm": 0, "decline": 0, "open": 1,
    }  # fmt: skip
    (row,) = board["directives"]
    assert (row["source_id"], row["state"], row["addressee"], row["text"]) == (
        "bee:1", "open", "computer", "Call Wren.",
    )  # fmt: skip
    assert "heard" not in row and board["next"].endswith("/directives")
    assert cli.main(["--json", "directives", "list", "--config", config, "--state", "confirm"]) == 0
    assert _board(capsys)["count"] == 0
    assert cli.main(["--json", "claims", "status", "--config", config]) == 0
    assert _board(capsys)["by_kind"] == {"assertion": 1, "directive": 1}


def test_claims_build_without_the_claims_table_says_what_to_add(tmp_path, capsys):
    config = _claims_world(tmp_path, claims_table=False)
    assert cli.main(["--json", "claims", "build", "--config", config, "--dry-run"]) == 1
    assert "[claims] is missing" in _board(capsys)["error"]


def test_claims_import_digests_and_status_count_and_show_no_text(tmp_path, capsys):
    config = _claims_world(tmp_path)
    assert cli.main(["--json", "claims", "import-digests", "--config", config, "--dry-run"]) == 0
    board = _board(capsys)
    assert (board["imported"], board["claims"], board["status"]) == (1, 1, "dry-run")
    assert cli.main(["--json", "claims", "status", "--config", config]) == 0
    assert _board(capsys)["states"] == {"current": 0, "stale": 0, "imported": 0, "none": 2}
    assert cli.main(["--json", "claims", "import-digests", "--config", config]) == 0
    assert _board(capsys)["imported"] == 1
    assert cli.main(["--json", "claims", "status", "--config", config]) == 0
    board = _board(capsys)
    assert board["states"] == {"current": 0, "stale": 0, "imported": 1, "none": 1}
    assert board["claims"] == 1 and board["by_kind"] == {"assertion": 1}
    assert "choice" not in json.dumps(board)  # counts only; no claim text on a board
    assert cli.main(["claims", "status", "--config", config, "--kind", "bee"]) == 0
    assert "none: 1" in capsys.readouterr().out


def test_text_import_counts_and_names_what_it_left_out(tmp_path, capsys):
    config = _claims_world(tmp_path)
    export = tmp_path / "Book.md"
    export.write_text(
        "# Book\n\nOne paragraph.\n\n## Perspective sheet\n\nLeft out.\n\n## Two\n\nSecond.\n"
    )
    args = ["text", "import", str(export), "--config", config, "--name", "Book"]
    assert cli.main(["--json", *args, "--dry-run"]) == 0
    board = _board(capsys)
    assert (board["source"], board["paragraphs"], board["left_out"], board["status"]) == (
        "text:book",
        2,
        ["Perspective sheet"],
        "dry-run",
    )
    assert cli.main(["--json", *args]) == 0
    board = _board(capsys)
    assert board["created"] is True and board["next"].endswith("--source text:book --dry-run")
    assert "paragraph" not in json.dumps(board).replace("paragraphs", "")  # counts, never text
    assert cli.main(["--json", "claims", "status", "--config", config, "--kind", "text"]) == 0
    assert _board(capsys)["sources"] == 1
    assert cli.main(["--json", *args]) == 0
    assert _board(capsys)["created"] is False
    assert (
        cli.main(
            [
                "--json",
                "text",
                "import",
                str(tmp_path / "nope.md"),
                "--config",
                config,
                "--name",
                "x",
            ]
        )
        == 1
    )
    assert "does not exist" in _board(capsys)["error"]


def test_a_missing_model_transport_is_named_without_a_traceback(tmp_path, capsys, monkeypatch):
    """Seen 2026-10-13: the first live `claims build` fell over with a traceback because the
    `llm` extra was not installed in corpuslm's own environment."""
    import builtins

    config = _claims_world(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    real_import = builtins.__import__

    def no_litellm(name, *args, **kwargs):
        if name == "litellm":
            raise ModuleNotFoundError("No module named 'litellm'", name="litellm")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_litellm)
    assert cli.main(["--json", "claims", "build", "--config", config, "--source", "bee:1"]) == 1
    assert "litellm is not installed; run: pip install -e '.[llm]'" in _board(capsys)["error"]


def test_concepts_verbs_count_rule_and_show_edges(tmp_path, capsys, monkeypatch):
    from corpuslm.store import Store

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = _claims_world(tmp_path)
    assert cli.main(["--json", "claims", "import-digests", "--config", config]) == 0
    _board(capsys)
    assert cli.main(["--json", "concepts", "build", "--config", config, "--dry-run"]) == 0
    board = _board(capsys)
    assert (board["status"], board["sources_to_build"], board["calls_billed"]) == ("dry-run", 1, 1)
    assert cli.main(["--json", "concepts", "build", "--config", config]) == 1
    assert "GEMINI_API_KEY is not set" in _board(capsys)["error"]
    database = db.Database(tmp_path / "transcript.sqlite")
    store = Store(database)
    claim = store.claims("Xq3vT9bLk2A")[0]["id"]
    store.tag_claims(
        {claim: [("choice", "Choice"), ("beat", "beat")]}, build_key="k", created_by="t"
    )
    store.refresh_concept_counts()
    database.close()
    assert cli.main(["--json", "concepts", "status", "--config", config]) == 0
    board = _board(capsys)
    assert board["states"] == {"in": 0, "out": 0, "boundary": 2} and board["concepts"] == 2
    assert board["sources"]["none"] == 1 and board["sources"]["no_claims"] == 1
    assert cli.main(["--json", "concepts", "verdict", "Choice", "--config", config, "--in"]) == 0
    assert _board(capsys)["verdict"] == "in"
    assert cli.main(["--json", "concepts", "list", "--config", config, "--state", "in"]) == 0
    board = _board(capsys)
    assert [(row["key"], row["verdict"]) for row in board["concepts"]] == [("choice", "in")]
    assert cli.main(["--json", "concepts", "edges", "beat", "--config", config]) == 0
    board = _board(capsys)
    assert [(edge["key"], edge["shared_claims"]) for edge in board["edges"]] == [("choice", 1)]
    assert cli.main(["--json", "concepts", "edges", "nope", "--config", config]) == 1
    assert cli.main(["concepts", "status", "--config", config]) == 0
    assert "choice" not in capsys.readouterr().out.replace("Choice", "")  # a status shows counts


def test_review_serve_dry_run_names_the_concepts_page_when_the_table_is_set(tmp_path, capsys):
    config = _claims_world(tmp_path)
    assert cli.main(["--json", "review", "serve", "--config", config, "--dry-run"]) == 0
    board = _board(capsys)
    assert board["concepts_url"].endswith("/concepts")
    (tmp_path / "bare").mkdir()
    bare = _claims_world(tmp_path / "bare", claims_table=False)
    assert cli.main(["--json", "review", "serve", "--config", bare, "--dry-run"]) == 0
    assert "concepts_url" not in _board(capsys)


def test_moments_verbs_import_count_list_and_rule(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = _claims_world(tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "Group 1 - 1.md").write_text(
        "## 123456 (not in the database yet)\n\n### Moment 1: a choice\n- 0 to 1 seconds.\n"
    )
    assert (
        cli.main(["--json", "moments", "import", str(notes), "--config", config, "--dry-run"]) == 0
    )
    board = _board(capsys)
    assert (board["imported"], board["unknown_source"], board["status"]) == (0, 1, "dry-run")
    from corpuslm.store import Store

    database = db.Database(tmp_path / "transcript.sqlite")
    Store(database).add_source("bee:123456", "l", created_by="t", kind="bee")
    Store(database).replace_segments("bee:123456", SEGMENTS)
    database.close()
    assert cli.main(["--json", "moments", "import", str(notes), "--config", config]) == 0
    assert _board(capsys)["imported"] == 1
    assert cli.main(["--json", "moments", "status", "--config", config]) == 0
    board = _board(capsys)
    assert board["states"] == {"approved": 1, "declined": 0, "proposed": 0}
    assert board["sources"]["no_claims"] >= 1
    assert cli.main(["--json", "moments", "list", "--config", config, "--state", "approved"]) == 0
    listed = _board(capsys)["moments"]
    assert [(m["source_id"], m["origin"], m["insight"]) for m in listed] == [
        ("bee:123456", "import", "a choice")
    ]
    assert (
        cli.main(["--json", "moments", "verdict", listed[0]["id"], "--config", config, "--decline"])
        == 0
    )
    assert _board(capsys)["verdict"] == "declined"
    assert cli.main(["--json", "claims", "import-digests", "--config", config]) == 0
    _board(capsys)
    assert cli.main(["--json", "moments", "propose", "--config", config, "--dry-run"]) == 0
    board = _board(capsys)
    assert board["status"] == "dry-run" and board["sources_to_build"] == 1  # the imported video
    assert cli.main(["--json", "review", "serve", "--config", config, "--dry-run"]) == 0
    assert _board(capsys)["moments_url"].endswith("/moments")


def test_body_and_sheet_verbs_against_the_fake_document_store(tmp_path, capsys, monkeypatch):
    from corpuslm.store import Store

    monkeypatch.setattr("corpuslm.document.FAKE_DIR", tmp_path / "docs")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = _claims_world(tmp_path)
    database = db.Database(tmp_path / "transcript.sqlite")
    Store(database).add_source("text:book", "l", created_by="t", kind="text")
    database.close()
    assert cli.main(["--json", "body", "add", "Book", "--text", "text:book", "--manuscript", "Book",
                     "--directory", str(tmp_path / "ms"), "--note", "the standing one",
                     "--config", config]) == 0  # fmt: skip
    assert _board(capsys)["kind"] == "standing"
    assert cli.main(["--json", "body", "add", "Talk", "--source", "bee:1", "--config", config]) == 0
    assert _board(capsys)["kind"] == "occasional"
    assert cli.main(["--json", "body", "add", "Nope", "--corpus", "nope", "--config", config]) == 1
    assert "unknown corpus" in _board(capsys)["error"]
    assert cli.main(["--json", "body", "list", "--config", config]) == 0
    board = _board(capsys)
    assert [(b["name"], b["kind"], b["sheet"]) for b in board["bodies"]] == [
        ("Book", "standing", "tab"), ("Talk", "occasional", "no")
    ]  # fmt: skip
    assert cli.main(["--json", "sheet", "draft", "Book", "--config", config, "--dry-run"]) == 0
    board = _board(capsys)
    assert board["status"] == "dry-run" and board["claims_sampled"] == 0 and board["tokens_in"] > 0
    assert cli.main(["--json", "sheet", "status", "Book", "--config", config]) == 0
    assert _board(capsys)["sheet"] == "none"  # no tab yet
    assert cli.main(["--json", "sheet", "draft", "Book", "--config", config]) == 1
    assert "GEMINI_API_KEY is not set" in _board(capsys)["error"]  # the tab's skeleton is made
    assert cli.main(["--json", "sheet", "refresh", "Book", "--config", config]) == 0
    assert _board(capsys)["vocabulary"] == 0
    assert cli.main(["--json", "sheet", "status", "Book", "--config", config]) == 0
    board = _board(capsys)
    assert board["sheet"] == "empty" and len(board["version"]) == 64
    assert cli.main(["--json", "sheet", "status", "Talk", "--config", config]) == 0
    assert _board(capsys)["sheet"] == "none"


def test_monologue_verbs_against_the_fake_document_store(tmp_path, capsys, monkeypatch):
    from corpuslm.store import Store

    monkeypatch.setattr("corpuslm.document.FAKE_DIR", tmp_path / "docs")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = _claims_world(tmp_path)
    database = db.Database(tmp_path / "transcript.sqlite")
    store = Store(database)
    moment = store.add_moment("bee:1", 0, 1, "a choice", origin="import", created_by="t")
    store.append_moment_verdict(
        "bee:1", store.moment_subject("bee:1", 0, 1), "approved",
        answered_through="review file", created_by="t",
    )  # fmt: skip
    database.close()
    assert (
        cli.main(
            [
                "--json",
                "monologue",
                "new",
                "Karma Walk",
                "--series",
                "SpiritualHindu",
                "--brief",
                "karma",
                "--config",
                config,
            ]
        )
        == 0
    )
    board = _board(capsys)
    assert (board["series"], board["file"], board["series_created"]) == (
        "SpiritualHindu",
        "karma-walk.md",
        True,
    )
    assert board["name"] == "karma-walk" and board["next"].startswith(
        "corpuslm monologue attach karma-walk"
    )
    assert (
        cli.main(
            ["--json", "monologue", "attach", "karma-walk", "--moment", moment, "--config", config]
        )
        == 0
    )
    assert _board(capsys)["attached"] is True
    assert (
        cli.main(
            ["--json", "monologue", "attach", "karma-walk", "--body", "nope", "--config", config]
        )
        == 1
    )
    assert "no body of work" in _board(capsys)["error"]
    assert cli.main(["--json", "monologue", "status", "karma-walk", "--config", config]) == 0
    assert _board(capsys)["ingredients"] == "none"
    assert (
        cli.main(["--json", "monologue", "run", "karma-walk", "--config", config, "--dry-run"]) == 0
    )
    board = _board(capsys)
    assert board["status"] == "dry-run" and board["moments"] == 1 and board["estimated_usd"] > 0
    assert cli.main(["--json", "monologue", "run", "karma-walk", "--config", config]) == 1
    assert "GEMINI_API_KEY is not set" in _board(capsys)["error"]
    assert (
        cli.main(
            ["--json", "monologue", "detach", "karma-walk", "--moment", moment, "--config", config]
        )
        == 0
    )
    assert _board(capsys)["detached"] is True
    assert cli.main(["--json", "monologue", "list", "--config", config]) == 0
    board = _board(capsys)
    assert [(m["name"], m["series"], m["moments"], m["runs"]) for m in board["monologues"]] == [
        ("karma-walk", "SpiritualHindu", 0, 0)
    ]


def test_monologue_export_and_the_teleprompter_url(tmp_path, capsys, monkeypatch):
    from corpuslm import document

    monkeypatch.setattr("corpuslm.document.FAKE_DIR", tmp_path / "docs")
    config = _claims_world(tmp_path)
    assert (
        cli.main(
            [
                "--json",
                "monologue",
                "new",
                "karma",
                "--series",
                "SpiritualHindu",
                "--title",
                "T",
                "--config",
                config,
            ]
        )
        == 0
    )
    _board(capsys)
    assert cli.main(["--json", "monologue", "export", "karma", "--config", config]) == 1
    assert "no script yet" in _board(capsys)["error"]

    doc = document.FakeDocumentStore(tmp_path / "docs")
    doc.write_tab("SpiritualHindu", "karma.md", "# Script v1: T\n\nSpoken words here.\n")
    out = tmp_path / "karma.txt"
    assert (
        cli.main(["--json", "monologue", "export", "karma", "--to", str(out), "--config", config])
        == 0
    )
    board = _board(capsys)
    assert board["words"] == 3 and out.read_text().startswith("monologue: mon-")
    assert cli.main(["--json", "review", "serve", "--config", config, "--dry-run"]) == 0
    board = _board(capsys)
    assert board["teleprompter_url"].endswith("/teleprompter/<monologue>")
    assert board["monologues_url"].endswith("/monologues")
