"""Bodies of work and perspective sheets (`src/corpuslm/bodies.py`) against the file-backed
document store: adding bodies, drafting the sheet into its tab, the code-owned vocabulary, the
version hash, the estimate, and the v13 -> v14 step. Synthetic text only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from llm_support import client as llm_client
from llmlm import llm

from corpuslm import bodies, claims, db, document
from corpuslm.store import Store

MODEL = "gemini/gemini-2.5-flash-lite"
CONFIG = {
    "claims": {"model": MODEL, "section_chars": 6000},
    "concepts": {"model": MODEL, "min_sections": 1, "min_claims": 1, "batch_claims": 120},
    "sheet": {"model": MODEL},
    "llm": {},
}
TEXT = "text:smsttd"
REPLY = {
    "stances": ["Choice precedes consciousness.", "Rank is a state, re-earned each beat."],
    "moves": ["I reach ethics from metaphysics, never the reverse."],
    "tone": "Plain, second person when teaching, no hedging.",
}


class World:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "ws"
        self.scripts = tmp_path / "scripts"
        self.docs = tmp_path / "docs"
        self.database = db.Database(tmp_path / "transcript.sqlite")
        self.store = Store(self.database)
        self.store.add_source(TEXT, "l", created_by="t", kind="text")
        self.store.replace_segments(
            TEXT, [{"idx": i, "t0": float(i), "t1": i + 1.0, "text": f"p {i}"} for i in range(4)]
        )
        self.store.replace_claims(
            TEXT,
            [
                {"section": 0, "segments": [0], "text": "Time counts beats.", "kind": "assertion"},
                {"section": 1, "segments": [1], "text": "Rank is a state.", "kind": "definition"},
                {"section": 2, "segments": [2], "text": "Beats again.", "kind": "assertion"},
                {"section": 3, "segments": [3], "text": "And again.", "kind": "assertion"},
            ],
            build_key="k",
            created_by="t",
        )
        self.store.record_artifact(
            TEXT, claims.NODE, path="", sha256="k", state="built", inputs={}, provenance={},
            created_by="t",
        )  # fmt: skip
        claim_ids = [row["id"] for row in self.store.claims(TEXT)]
        self.store.tag_claims(
            {claim_ids[0]: [("beat", "beat")], claim_ids[1]: [("rank", "rank")],
             claim_ids[2]: [("beat", "beat")], claim_ids[3]: [("beat", "beat")]},
            build_key="k", created_by="t",
        )  # fmt: skip  # beat: 3 sections (in); rank: 1 section (boundary, not in the sheet)
        self.store.refresh_concept_counts()
        self.store.add_corpus({"name": "Advaita"}, created_by="t")
        self.doc = document.FakeDocumentStore(self.docs)

    def script(self, tag: str, reply: dict = REPLY) -> None:
        folder = self.scripts / bodies.STAGE
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{tag}.json").write_text(
            json.dumps({"reply": json.dumps(reply), "tokens_in": 500, "tokens_out": 300})
        )

    def client(self, config: dict | None = None) -> llm.Client:
        return llm_client(
            self.root,
            config or CONFIG,
            transport=llm.ScriptedTransport(self.scripts),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

    def body(self, **changes) -> dict:
        return bodies.add(
            self.store,
            "SMSTTD",
            **({"text_source_id": TEXT, "manuscript": "SMSTTD", "directory": str(self.docs / "ms")}
               | changes),
            created_by="t",
        )  # fmt: skip


@pytest.fixture
def world(tmp_path):
    made = World(tmp_path)
    yield made
    made.database.close()


def test_add_makes_standing_and_occasional_bodies_and_names_what_is_wrong(world):
    assert world.body() == {
        "id": world.store.body("SMSTTD")["id"],
        "name": "SMSTTD",
        "kind": "standing",
    }
    corpus_id = world.store.corpus_by_name("Advaita")["id"]
    assert (
        bodies.add(world.store, "Advaita", corpus_id=corpus_id, created_by="t")["kind"]
        == "standing"
    )
    assert (
        bodies.add(world.store, "one talk", source_id=TEXT, created_by="t")["kind"] == "occasional"
    )
    with pytest.raises(ValueError, match="exactly one"):
        bodies.add(world.store, "x", text_source_id=TEXT, source_id=TEXT, created_by="t")
    with pytest.raises(ValueError, match="exists"):
        bodies.add(world.store, "SMSTTD", text_source_id=TEXT, created_by="t")
    with pytest.raises(KeyError, match="unknown source"):
        bodies.add(world.store, "y", text_source_id="text:nope", created_by="t")
    with pytest.raises(ValueError, match="both --manuscript and --directory"):
        bodies.add(world.store, "z", text_source_id=TEXT, manuscript="only", created_by="t")
    listed = bodies.listing(world.store)
    assert [(row["name"], row["kind"], row["sheet"]) for row in listed] == [
        ("Advaita", "standing", "no"),
        ("SMSTTD", "standing", "tab"),
        ("one talk", "occasional", "no"),
    ]
    assert bodies.source_ids_of(world.store, world.store.body("one talk")) == [TEXT]


def test_the_draft_touches_only_the_sheet_tab(world):
    world.body()
    world.doc.ensure("SMSTTD", world.docs / "ms", "SMSTTD")
    world.doc.add_tab("SMSTTD", "chapter.md", "A chapter")
    world.doc.write_tab("SMSTTD", "chapter.md", "# A chapter\n\nThe owner's prose.\n")
    world.script(f"sheet-{world.store.body('SMSTTD')['id']}")
    out = bodies.draft(world.store, world.client(), world.doc, "SMSTTD", created_by="t")
    assert (out["staged"], out["failed"], out["vocabulary"], out["cached"]) == (4, 0, 1, False)
    tabs = {tab["file"]: tab for tab in world.doc.read("SMSTTD")["tabs"]}
    assert set(tabs) == {"chapter.md", bodies.SHEET_FILE}
    assert tabs["chapter.md"]["settled"] == "# A chapter\n\nThe owner's prose.\n"
    sheet = tabs[bodies.SHEET_FILE]
    assert sheet["title"] == "Perspective sheet"
    blocks = document.paragraphs(sheet["settled"])
    assert blocks[:3] == ["# Perspective sheet", "[Omit: all]", "## Stances"]
    assert blocks[-2:] == ["- beat", "[/Omit]"]
    assert [(r["after"], r["new"][:12]) for r in sheet["open"]] == [
        (3, "Choice prece"), (3, "Rank is a st"), (4, "I reach ethi"), (5, "Plain, secon")
    ]  # fmt: skip
    world.doc.resolve("SMSTTD", bodies.SHEET_FILE)
    settled = document.paragraphs(world.doc.read("SMSTTD")["tabs"][1]["settled"])
    assert settled[3:5] == [
        "Choice precedes consciousness.",
        "Rank is a state, re-earned each beat.",
    ]
    assert (
        settled[settled.index("## Tone") + 1] == "Plain, second person when teaching, no hedging."
    )
    with pytest.raises(ValueError, match="no manuscript"):
        bodies.add(
            world.store,
            "bare",
            corpus_id=world.store.corpus_by_name("Advaita")["id"],
            created_by="t",
        )
        bodies.draft(world.store, world.client(), world.doc, "bare", created_by="t")


def test_a_tab_with_open_revisions_takes_no_draft(world):
    world.body()
    tag = f"sheet-{world.store.body('SMSTTD')['id']}"
    world.script(tag)
    bodies.draft(world.store, world.client(), world.doc, "SMSTTD", created_by="t")
    with pytest.raises(ValueError, match="open revisions"):
        bodies.draft(world.store, world.client(), world.doc, "SMSTTD", created_by="t")
    with pytest.raises(ValueError, match="open revisions"):
        bodies.refresh_vocabulary(world.store, world.doc, "SMSTTD", CONFIG)


def test_refreshing_the_vocabulary_leaves_the_owners_prose_alone(world):
    world.body()
    world.script(f"sheet-{world.store.body('SMSTTD')['id']}")
    bodies.draft(world.store, world.client(), world.doc, "SMSTTD", created_by="t")
    world.doc.resolve("SMSTTD", bodies.SHEET_FILE)
    # the owner edits a stance by hand (the fake lets a test write the settled text)
    settled = world.doc.read("SMSTTD")["tabs"][0]["settled"]
    edited = settled.replace("Choice precedes consciousness.", "Choice comes before consciousness.")
    world.doc.write_tab("SMSTTD", bodies.SHEET_FILE, edited)
    concept = world.store.concept("beat")
    world.store.db.update(
        "concepts", concept["id"], {"definition": "the atomic interval of choice"}
    )
    world.store.add_source("bee:9", "l", created_by="t", kind="bee")
    out = bodies.refresh_vocabulary(world.store, world.doc, "SMSTTD", CONFIG)
    assert out == {"body": "SMSTTD", "vocabulary": 1, "changed": True}
    text = world.doc.read("SMSTTD")["tabs"][0]["settled"]
    assert "Choice comes before consciousness." in text
    assert "- beat: the atomic interval of choice\n\n[/Omit]" in text and "rank" not in text
    assert bodies.refresh_vocabulary(world.store, world.doc, "SMSTTD", CONFIG)["changed"] is False
    no_concepts = {"claims": CONFIG["claims"], "sheet": CONFIG["sheet"], "llm": {}}
    assert bodies.vocabulary_lines(world.store, no_concepts) == []


def test_status_records_the_sheet_version_when_the_resolved_text_changes(world):
    world.body()
    assert bodies.status(world.store, world.doc, "SMSTTD")["sheet"] == "none"
    world.script(f"sheet-{world.store.body('SMSTTD')['id']}")
    bodies.draft(world.store, world.client(), world.doc, "SMSTTD", created_by="t")
    before = bodies.status(world.store, world.doc, "SMSTTD")
    assert (before["sheet"], before["open_revisions"], before["changed"]) == ("empty", 4, True)
    assert world.store.body("SMSTTD")["sheet_sha256"] == before["version"]
    world.doc.resolve("SMSTTD", bodies.SHEET_FILE)
    after = bodies.status(world.store, world.doc, "SMSTTD")
    assert (after["sheet"], after["open_revisions"], after["paragraphs"]) == ("drafted", 0, 4)
    assert after["version"] != before["version"] and after["changed"] is True
    assert bodies.status(world.store, world.doc, "SMSTTD")["changed"] is False
    assert bodies.listing(world.store)[0]["sheet"] == "yes"


def test_the_estimate_calls_no_model_and_the_cap_stops_the_draft(tmp_path):
    world = World(tmp_path)
    try:
        world.body()
        estimate = bodies.estimate(world.store, world.client(), "SMSTTD")
        assert (estimate.claims, estimate.concepts, estimate.cached) == (4, 1, False)
        assert estimate.tokens_in > 0 and estimate.usd is not None and estimate.usd > 0
        assert not (world.root / "logs").exists()
        capped = world.client(CONFIG | {"llm": {"max_usd_per_run": 0.0}})
        with pytest.raises(llm.SpendCapReached):
            bodies.draft(world.store, capped, world.doc, "SMSTTD", created_by="t")
        with pytest.raises(KeyError, match="no body of work"):
            bodies.estimate(world.store, world.client(), "nope")
    finally:
        world.database.close()


def test_with_vocabulary_replaces_only_that_section():
    text = bodies.skeleton().replace("## Stances", "## Stances\n\nA stance.")
    out = bodies.with_vocabulary(text, ["- beat", "- rank"])
    assert "A stance." in out and "- beat\n- rank" in out and out.endswith("[/Omit]\n")
    assert bodies.with_vocabulary(out, []) == text
    assert "## Vocabulary" in bodies.with_vocabulary("# Sheet\n\nProse.\n", ["- x"])


def test_a_version_13_database_gains_the_bodies_table_after_a_verified_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    first = db.Database(path)
    Store(first).add_source(TEXT, "l", created_by="t", kind="text")
    first.close()
    connection = sqlite3.connect(path)
    for statement in (
        "DROP TRIGGER bodies_of_work_change_log",
        "DROP TABLE bodies_of_work",
        "PRAGMA user_version = 13",
    ):
        connection.execute(statement)
    connection.commit()
    connection.close()
    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 13 and opened.migration["to"] == db.USER_VERSION == 18
        assert opened.migration["backup"].endswith("-before-v18.sqlite")
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        store = Store(opened)
        store.add_body("SMSTTD", "standing", text_source_id=TEXT, created_by="t")
        store.update_body("SMSTTD", {"note": "the standing philosophy"})
        logged = opened.one(
            "SELECT COUNT(*) AS n FROM change_log WHERE table_name = 'bodies_of_work'"
        )
        assert logged["n"] == 1  # the change-log trigger came with it
        with pytest.raises(sqlite3.IntegrityError):
            store.add_body("two", "standing", text_source_id=TEXT, source_id=TEXT, created_by="t")
    finally:
        opened.close()


def test_a_bodys_vocabulary_is_only_its_own_concepts(world):
    world.body()
    other = "bee:9"
    world.store.add_source(other, "l", created_by="t", kind="bee")
    world.store.replace_segments(
        other, [{"idx": i, "t0": float(i), "t1": i + 1.0, "text": f"q {i}"} for i in range(3)]
    )
    world.store.replace_claims(
        other,
        [
            {"section": i, "segments": [i], "text": f"Sim {i}.", "kind": "assertion"}
            for i in range(3)
        ],
        build_key="k",
        created_by="t",
    )
    ids = [row["id"] for row in world.store.claims(other)]
    world.store.tag_claims(
        {claim_id: [("simulation", "simulation")] for claim_id in ids},
        build_key="k",
        created_by="t",
    )
    world.store.refresh_concept_counts()
    shared = bodies.vocabulary_lines(world.store, CONFIG)
    assert "- simulation" in shared  # in force across the whole lexicon
    assert bodies.vocabulary_lines(world.store, CONFIG, world.store.body("SMSTTD")) == ["- beat"]


def test_a_tab_with_bold_and_repeated_vocabulary_headings_keeps_one_section():
    text = (
        "# **Perspective sheet**\n\n[Omit]\n\n## **Stances**\n\nA stance.\n\n## **Vocabulary**\n\n"
        "- old\n\n## **Vocabulary**\n\n- older\n\n## Vocabulary\n\n- oldest\n\n[/Omit]\n"
    )
    out = bodies.with_vocabulary(text, ["- beat"])
    assert out.count("Vocabulary") == 1 and "- beat" in out and "old" not in out
    assert "A stance." in out and out.endswith("[/Omit]\n")
