"""Monologues (`src/corpuslm/monologues.py`) against the file-backed document store: attaching
moments and bodies, the ingredients hash, a run that writes a script tab, the stored run for the
same ingredients, the prior as style guide, `--fresh`, staleness reasons, the estimate and the
v14 -> v15 step. Synthetic text only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from corpuslm import bodies, claims, db, document, llm, monologues
from corpuslm.store import Store

MODEL = "gemini/gemini-2.5-flash-lite"
CONFIG = {
    "claims": {"model": MODEL, "section_chars": 6000},
    "concepts": {"model": MODEL, "min_sections": 1, "min_claims": 1, "batch_claims": 120},
    "monologue": {"model": MODEL, "minutes": 2},
    "llm": {},
}
BEE, TEXT = "bee:1", "text:smsttd"
REPLY = {
    "title": "As of now",
    "paragraphs": ["I was walking with my son.", "A choice is right only as of now."],
}


class World:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "ws"
        self.scripts = tmp_path / "scripts"
        self.database = db.Database(tmp_path / "transcript.sqlite")
        self.store = Store(self.database)
        self.doc = document.FakeDocumentStore(tmp_path / "docs")
        for source_id, kind in ((BEE, "bee"), (TEXT, "text")):
            self.store.add_source(source_id, "l", created_by="t", kind=kind)
            self.store.replace_segments(
                source_id,
                [
                    {"idx": i, "t0": 10.0 * i, "t1": 10.0 * i + 10, "speaker": "Quill",
                     "text": f"said thing {i}"}
                    for i in range(4)
                ],
            )  # fmt: skip
            self.store.replace_claims(
                source_id,
                [{"section": 0, "segments": [1], "text": f"Claim of {source_id}.",
                  "kind": "assertion"}],
                build_key=f"k-{source_id}", created_by="t",
            )  # fmt: skip
            self.store.record_artifact(
                source_id, claims.NODE, path="", sha256=f"k-{source_id}", state="built",
                inputs={}, provenance={}, created_by="t",
            )  # fmt: skip
        self.moment = self.store.add_moment(
            BEE, 0, 2, "A choice is right as of now", origin="import", created_by="t"
        )
        self.store.append_moment_verdict(
            BEE,
            self.store.moment_subject(BEE, 0, 2),
            "approved",
            answered_through="review file",
            created_by="t",
        )
        self.unapproved = self.store.add_moment(
            BEE, 3, 3, "Unruled", origin="model", created_by="t"
        )
        bodies.add(
            self.store, "SMSTTD", text_source_id=TEXT, manuscript="SMSTTD",
            directory=str(tmp_path / "ms"), note="the standing one", created_by="t",
        )  # fmt: skip
        bodies.add(self.store, "a talk", source_id=TEXT, created_by="t")
        monologues.create(self.store, "Karma Walk", brief="karma, from the walk", created_by="t")

    def script(self, tag: str, reply: dict = REPLY) -> None:
        folder = self.scripts / monologues.STAGE
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{tag}.json").write_text(
            json.dumps({"reply": json.dumps(reply), "tokens_in": 900, "tokens_out": 400})
        )

    def client(self, config: dict | None = None) -> llm.Client:
        return llm.Client(
            self.root, config or CONFIG, transport=llm.ScriptedTransport(self.scripts),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )  # fmt: skip

    def key(self, config: dict | None = None) -> str:
        return monologues.ingredients_sha256(
            monologues.ingredients(self.store, self.doc, "karma-walk", config or CONFIG)
        )

    def run(self, config: dict | None = None, **kw) -> dict:
        self.script(f"karma-walk-{self.key(config)[:12]}")
        return monologues.run(
            self.store, self.client(config), self.doc, "karma-walk",
            directory=self.root / "monologues" / "karma-walk", created_by="t", **kw,
        )  # fmt: skip


@pytest.fixture
def world(tmp_path):
    made = World(tmp_path)
    yield made
    made.database.close()


def test_create_attach_and_detach_check_what_they_are_given(world):
    assert world.store.monologue("karma-walk")["brief"] == "karma, from the walk"
    with pytest.raises(ValueError, match="exists"):
        monologues.create(world.store, "karma walk", created_by="t")
    with pytest.raises(ValueError, match="not approved"):
        monologues.attach(world.store, "karma-walk", moment_id=world.unapproved)
    assert monologues.attach(world.store, "karma-walk", moment_id=world.moment)["attached"]
    assert not monologues.attach(world.store, "karma-walk", moment_id=world.moment)["attached"]
    assert monologues.attach(world.store, "karma-walk", body="SMSTTD")["attached"]
    with pytest.raises(KeyError, match="no body of work"):
        monologues.attach(world.store, "karma-walk", body="nope")
    with pytest.raises(ValueError, match="one moment"):
        monologues.attach(world.store, "karma-walk", moment_id=world.moment, body="SMSTTD")
    with pytest.raises(KeyError, match="no monologue"):
        monologues.attach(world.store, "nope", body="SMSTTD")
    row = world.store.monologue("karma-walk")
    assert [m["insight"] for m in world.store.monologue_moments(row["id"])] == [
        "A choice is right as of now"
    ]
    assert monologues.detach(world.store, "karma-walk", moment_id=world.moment)["detached"]
    assert monologues.detach(world.store, "karma-walk", body="SMSTTD")["detached"]
    assert (
        world.store.monologue_moments(row["id"]) == []
        and world.store.monologue_bodies(row["id"]) == []
    )


def test_a_run_writes_a_script_tab_an_about_tab_and_the_run_row(world):
    monologues.attach(world.store, "karma-walk", moment_id=world.moment)
    monologues.attach(world.store, "karma-walk", body="SMSTTD")
    monologues.attach(world.store, "karma-walk", body="a talk")
    standing = monologues.status(world.store, world.doc, "karma-walk", CONFIG)
    assert (standing.state, standing.runs) == ("none", 0)
    out = world.run()
    assert (out["run"], out["unchanged"], out["tab"], out["paragraphs"], out["prior"]) == (
        1,
        False,
        "script.md",
        2,
        "",
    )
    assert out["cost_usd"] > 0 and out["cached"] is False
    row = world.store.monologue("karma-walk")
    assert (row["manuscript"], row["title"]) == ("monologue-karma-walk", "As of now")
    tabs = {tab["file"]: tab for tab in world.doc.read("monologue-karma-walk")["tabs"]}
    assert set(tabs) == {"script.md", monologues.ABOUT}
    assert (
        tabs["script.md"]["settled"] == "# Script v1: As of now\n\nI was walking with my son.\n\n"
        "A choice is right only as of now.\n"
    )
    about = tabs[monologues.ABOUT]["settled"]
    assert (
        "- SMSTTD (standing)" in about
        and "- a talk (occasional)" in about
        and "- v1 on 2026" in about
    )
    (run_row,) = world.store.monologue_runs(row["id"])
    assert (run_row["number"], run_row["fresh"], run_row["prior_run_id"], run_row["title"]) == (
        1,
        0,
        "",
        "As of now",
    )
    record = json.loads(run_row["ingredients"])
    assert [b["name"] for b in record["bodies"]] == ["SMSTTD", "a talk"]
    assert record["bodies"][1]["claims"] == ["k-text:smsttd"] and record["moments"][0][
        "claims"
    ] == ["Claim of bee:1."]
    assert world.store.body("SMSTTD")["last_used_at"] == run_row["created_at"]
    assert monologues.status(world.store, world.doc, "karma-walk", CONFIG).state == "current"


def test_the_same_ingredients_return_the_stored_run_and_spend_nothing(world):
    monologues.attach(world.store, "karma-walk", moment_id=world.moment)
    world.run()
    estimate = monologues.estimate(world.store, world.client(), world.doc, "karma-walk")
    assert estimate.stored is True and estimate.usd == 0.0
    again = monologues.run(
        world.store, world.client(), world.doc, "karma-walk",
        directory=world.root / "x", created_by="t",
    )  # fmt: skip
    assert again["unchanged"] is True and again["cost_usd"] == 0.0 and again["run"] == 1
    assert len(world.store.monologue_runs(world.store.monologue("karma-walk")["id"])) == 1
    assert len(world.doc.read("monologue-karma-walk")["tabs"]) == 2


def test_a_changed_ingredient_runs_again_with_the_settled_prior_as_style_guide(world):
    monologues.attach(world.store, "karma-walk", moment_id=world.moment)
    world.run()
    # the owner edits the script in the Doc (the fake lets a test write the settled text)
    world.doc.write_tab(
        "monologue-karma-walk", "script.md",
        "# Script v1: As of now\n\nI was walking with my son, as I often do.\n\n"
        "A choice is right only as of now.\n",
    )  # fmt: skip
    world.store.update_monologue("karma-walk", {"brief": "karma, and choosing again"})
    standing = monologues.status(world.store, world.doc, "karma-walk", CONFIG)
    assert (standing.state, standing.changes) == ("stale", ["brief changed"])
    estimate = monologues.estimate(world.store, world.client(), world.doc, "karma-walk")
    assert estimate.stored is False and estimate.tokens_in > 0
    world.script(
        f"karma-walk-{world.key()[:12]}", REPLY | {"paragraphs": ["Revised.", "Still as of now."]}
    )
    out = monologues.run(
        world.store, world.client(), world.doc, "karma-walk",
        directory=world.root / "monologues" / "karma-walk", created_by="t",
    )  # fmt: skip
    assert (out["run"], out["prior"], out["changes"], out["tab"]) == (
        2,
        "script.md",
        ["brief changed"],
        "script.md",
    )
    runs = world.store.monologue_runs(world.store.monologue("karma-walk")["id"])
    assert runs[1]["prior_run_id"] == runs[0]["id"]
    # the prior the model saw was the owner's edited text, not the model's own first output
    cache = list((world.root / "llm-cache").glob("*.json"))
    assert len(cache) == 2
    payloads = " ".join(path.read_text() for path in cache)
    assert "as I often do" in payloads
    tabs = {tab["file"]: tab for tab in world.doc.read("monologue-karma-walk")["tabs"]}
    assert set(tabs) == {"script.md", monologues.ABOUT}
    assert tabs["script.md"]["settled"].startswith("# Script v2: As of now\n\nRevised.")
    assert (
        "as I often do" in runs[1]["prior"] and runs[0]["prior"] == ""
    )  # the edited prior is kept
    # a fresh run drops the prior
    monologues.attach(world.store, "karma-walk", body="a talk")
    world.script(f"karma-walk-{world.key()[:12]}")
    fresh = monologues.run(
        world.store, world.client(), world.doc, "karma-walk",
        directory=world.root / "monologues" / "karma-walk", fresh=True, created_by="t",
    )  # fmt: skip
    assert (fresh["run"], fresh["fresh"], fresh["prior"]) == (3, True, "script.md")
    assert (
        world.store.monologue_runs(world.store.monologue("karma-walk")["id"])[2]["prior_run_id"]
        == ""
    )
    assert fresh["changes"] == ["body a talk attached"]


def test_status_names_what_changed_in_the_sheet_the_moments_and_the_recipe(world):
    monologues.attach(world.store, "karma-walk", moment_id=world.moment)
    monologues.attach(world.store, "karma-walk", body="SMSTTD")
    world.run()
    bodies.refresh_vocabulary(world.store, world.doc, "SMSTTD", CONFIG)  # makes the sheet tab
    standing = monologues.status(world.store, world.doc, "karma-walk", CONFIG)
    assert standing.state == "stale" and standing.changes == [
        "body SMSTTD changed (its sheet or claims)"
    ]
    world.store.detach_moment(
        world.store.monologue("karma-walk")["id"], BEE, world.store.moment_subject(BEE, 0, 2)
    )
    standing = monologues.status(world.store, world.doc, "karma-walk", CONFIG)
    assert "1 moment(s) removed" in standing.changes
    other = CONFIG | {"monologue": {"model": "other/model", "minutes": 2}}
    assert "model changed" in monologues.status(world.store, world.doc, "karma-walk", other).changes
    with pytest.raises(ValueError, match="no approved moments"):
        world.run()


def test_the_estimate_calls_no_model_and_the_cap_stops_the_run(tmp_path):
    world = World(tmp_path)
    try:
        monologues.attach(world.store, "karma-walk", moment_id=world.moment)
        estimate = monologues.estimate(world.store, world.client(), world.doc, "karma-walk")
        assert (estimate.moments, estimate.bodies, estimate.stored) == (1, 0, False)
        assert estimate.usd is not None and estimate.usd > 0
        assert not (world.root / "logs").exists()
        capped = world.client(CONFIG | {"llm": {"max_usd_per_run": 0.0}})
        with pytest.raises(llm.SpendCapReached):
            monologues.run(
                world.store, capped, world.doc, "karma-walk", directory=world.root, created_by="t"
            )
        assert world.store.monologue_runs(world.store.monologue("karma-walk")["id"]) == []
    finally:
        world.database.close()


def test_listing_shows_each_monologue_with_its_standing(world):
    monologues.attach(world.store, "karma-walk", moment_id=world.moment)
    listed = monologues.listing(world.store, world.doc, CONFIG)
    assert [(m["name"], m["moments"], m["runs"], m["ingredients"]) for m in listed] == [
        ("karma-walk", 1, 0, "none")
    ]
    assert monologues.listing(world.store, world.doc, None)[0].get("ingredients") is None


def test_a_version_14_database_gains_the_monologue_tables_after_a_verified_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    first = db.Database(path)
    Store(first).add_source(BEE, "l", created_by="t", kind="bee")
    first.close()
    connection = sqlite3.connect(path)
    for statement in (
        "DROP TRIGGER monologues_change_log",
        "DROP TABLE monologue_runs",
        "DROP TABLE monologue_bodies",
        "DROP TABLE monologue_moments",
        "DROP TABLE monologues",
        "PRAGMA user_version = 14",
    ):
        connection.execute(statement)
    connection.commit()
    connection.close()
    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 14 and opened.migration["to"] == db.USER_VERSION == 16
        assert opened.migration["backup"].endswith("-before-v16.sqlite")
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        store = Store(opened)
        made = store.add_monologue("x", created_by="t")
        store.update_monologue("x", {"title": "X"})
        assert (
            opened.one("SELECT COUNT(*) AS n FROM change_log WHERE table_name = 'monologues'")["n"]
            == 1
        )
        assert store.attach_moment(made, BEE, "a" * 64) and not store.attach_moment(
            made, BEE, "a" * 64
        )
        run_id = store.add_monologue_run(
            made, {"ingredients_sha256": "b" * 64, "ingredients": "{}", "recipe": "monologue",
                   "recipe_sha256": "c", "model": "m", "output": "text"}, created_by="t",
        )  # fmt: skip
        assert store.monologue_run_by_ingredients(made, "b" * 64)["id"] == run_id
        with pytest.raises(sqlite3.IntegrityError):  # the same ingredients never run twice
            store.add_monologue_run(
                made, {"ingredients_sha256": "b" * 64, "ingredients": "{}", "recipe": "monologue",
                       "recipe_sha256": "c", "model": "m", "output": "again"}, created_by="t",
            )  # fmt: skip
    finally:
        opened.close()
