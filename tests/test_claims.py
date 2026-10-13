"""The claim pass (`src/corpuslm/claims.py`): sections, the build with scripted replies, the
cache and the estimate, staleness reasons, the digest import, and the v9 -> v10 step. Synthetic
text only; no vendor is ever called."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from corpuslm import claims, db, llm, schemas
from corpuslm.store import Store

MODEL = "gemini/gemini-2.5-flash-lite"
SOURCE = "bee:1"
SEGMENTS = [
    {"idx": 0, "t0": 0.0, "t1": 1.0, "speaker": "Quill", "text": "a choice is right as of now"},
    {"idx": 1, "t0": 1.0, "t1": 2.0, "speaker": "Wren", "text": "and then the world moves"},
    {"idx": 2, "t0": 2.0, "t1": 3.0, "speaker": "Quill", "text": "so you choose again"},
    {"idx": 3, "t0": 3.0, "t1": 4.0, "speaker": "Quill", "text": "that is the whole job"},
]
FIRST = {
    "claims": [
        {"statement": "A choice is right only as of the moment it is made.", "speaker": "Quill",
         "segments": [0], "kind": "assertion"},
        {"statement": "A stray claim citing another section.", "speaker": "", "segments": [3],
         "kind": "assertion"},
    ]
}  # fmt: skip
SECOND = {
    "claims": [
        {"statement": "After the world moves one chooses again.", "speaker": "Quill",
         "segments": [2, 1, 9], "kind": "assertion"},
        {"statement": "Choosing again is the task itself.", "speaker": "Quill",
         "segments": [3], "kind": "definition"},
    ]
}  # fmt: skip


class World:
    def __init__(self, tmp_path: Path, *, section_chars: int = 40, caps: dict | None = None):
        self.root = tmp_path / "ws"
        self.scripts = tmp_path / "scripts"
        self.database = db.Database(tmp_path / "transcript.sqlite")
        self.store = Store(self.database)
        self.store.add_source(SOURCE, "l", created_by="test", kind="bee")
        self.store.update_source(SOURCE, {"title": "A walk", "published_at": "2026-10-01"})
        self.store.replace_segments(SOURCE, SEGMENTS)
        self.config = {
            "claims": {"model": MODEL, "section_chars": section_chars},
            "llm": caps or {},
        }

    def script(self, tag: str, reply: dict, tokens=(100, 20)) -> None:
        folder = self.scripts / claims.STAGE
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{tag}.json").write_text(
            json.dumps(
                {"reply": json.dumps(reply), "tokens_in": tokens[0], "tokens_out": tokens[1]}
            )
        )

    def client(self, config: dict | None = None) -> llm.Client:
        return llm.Client(
            self.root,
            config or self.config,
            transport=llm.ScriptedTransport(self.scripts),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )


@pytest.fixture
def world(tmp_path):
    made = World(tmp_path)
    made.script(f"{SOURCE}-s0", FIRST)
    made.script(f"{SOURCE}-s1", SECOND)
    yield made
    made.database.close()


def test_sections_cut_whole_segments_once_the_size_is_reached():
    parts = claims.sections(SEGMENTS, 40)
    assert [[segment["idx"] for segment in part] for part in parts] == [[0, 1], [2, 3]]
    assert claims.sections(SEGMENTS, 10_000) == [SEGMENTS]
    assert claims.sections([], 40) == []


def test_build_stores_claims_tied_to_segments_drops_strays_and_records_the_artifact(world):
    out = claims.build(world.store, world.client(), SOURCE, created_by="test")
    assert (out["sections"], out["claims"], out["dropped"], out["cached"]) == (2, 3, 1, False)
    assert out["cost_usd"] > 0
    rows = world.store.claims(SOURCE)
    assert [(row["position"], row["section"], row["segments"], row["kind"]) for row in rows] == [
        (0, 0, [0], "assertion"),
        (1, 1, [2], "assertion"),  # 1 is another section's line and 9 is no segment
        (2, 1, [3], "definition"),
    ]
    assert rows[0]["text"] == "A choice is right only as of the moment it is made."
    assert {row["build_key"] for row in rows} == {
        claims.build_key(claims.inputs(world.store, SOURCE, world.config))
    }
    artifact = world.store.artifact(SOURCE, claims.NODE)
    assert artifact["state"] == "built"
    assert set(json.loads(artifact["inputs"])) == {"text", "prompt", "model", "section_chars"}
    provenance = json.loads(artifact["provenance"])
    assert provenance["sections"] == 2 and provenance["prompt_id"] == "claims"
    (entry,) = claims.status(world.store, world.config, [SOURCE])
    assert (entry.state, entry.claims, entry.reason) == ("current", 3, "")


def test_the_same_inputs_are_served_from_cache_and_nothing_is_billed(world):
    claims.build(world.store, world.client(), SOURCE, created_by="test")
    estimate = claims.estimate(world.store, world.client(), [SOURCE])
    assert (estimate.sources, estimate.calls, estimate.usd) == (0, 0, 0.0)  # current: not built
    world.store.mark_stale(SOURCE, claims.NODE)
    estimate = claims.estimate(world.store, world.client(), [SOURCE])
    assert (estimate.sources, estimate.from_cache, estimate.calls) == (1, 1, 0)
    again = claims.build(world.store, world.client(), SOURCE, created_by="test")
    assert again["cached"] is True and again["cost_usd"] == 0.0
    assert world.store.claim_count(SOURCE) == 3


def test_a_changed_text_prompt_or_model_makes_the_source_stale_with_the_reason(world):
    claims.build(world.store, world.client(), SOURCE, created_by="test")
    changed = world.config | {"claims": {"model": "other/model", "section_chars": 40}}
    (entry,) = claims.status(world.store, changed, [SOURCE])
    assert (entry.state, entry.reason) == ("stale", "model changed")
    world.store.replace_segments(SOURCE, SEGMENTS[:3])
    (entry,) = claims.status(world.store, world.config, [SOURCE])
    assert (entry.state, entry.reason) == ("stale", "text changed")
    (entry,) = claims.status(world.store, None, [SOURCE])
    assert (entry.state, entry.reason) == ("stale", "no [claims] settings")


def test_the_estimate_calls_no_model_and_counts_what_would_be_billed(tmp_path):
    world = World(tmp_path)  # no scripted replies: a call would fail
    try:
        estimate = claims.estimate(world.store, world.client(), [SOURCE])
        assert (estimate.sources, estimate.from_cache, estimate.calls) == (1, 0, 2)
        assert estimate.tokens_in > 0 and estimate.tokens_out > 0
        assert estimate.usd is not None and estimate.usd > 0 and estimate.model == MODEL
        assert not (world.root / "logs").exists()  # no ledger line, no call
        assert world.store.claim_count() == 0
        (entry,) = claims.status(world.store, world.config, [SOURCE])
        assert entry.state == "none"
    finally:
        world.database.close()


def test_a_long_segment_list_is_kept_to_six_not_rejected(world):
    """Seen 2026-10-13: a reply listing seven segments for a claim failed the whole run."""
    long = {"claims": [FIRST["claims"][0] | {"segments": [0, 1, 0, 1, 0, 1, 0, 1]}]}
    world.script(f"{SOURCE}-s0", long)
    out = claims.build(world.store, world.client(), SOURCE, created_by="test")
    assert out["claims"] == 3 and out["dropped"] == 0
    assert world.store.claims(SOURCE)[0]["segments"] == [0, 1]  # duplicates folded, six at most


def test_a_bad_reply_fails_one_source_and_the_run_goes_on(world, tmp_path):
    from corpuslm import cli

    world.store.add_source("bee:2", "l2", created_by="test", kind="bee")
    world.store.replace_segments("bee:2", SEGMENTS)
    world.script("bee:2-s0", FIRST)
    world.script("bee:2-s1", SECOND)
    folder = world.scripts / claims.STAGE
    (folder / f"{SOURCE}-s0.json").write_text(
        json.dumps({"reply": "not json", "tokens_in": 1, "tokens_out": 1})
    )
    world.database.close()
    path = tmp_path / "review.toml"
    path.write_text(
        f'database = "{tmp_path / "transcript.sqlite"}"\n[kinds.bee]\ndirectory = "{tmp_path}"\n'
        f'[claims]\nmodel = "{MODEL}"\nworkspace = "{world.root}"\n'
    )
    import corpuslm.llm as llm_module

    real_client = llm_module.Client
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        llm_module,
        "Client",
        lambda root, config, **kw: real_client(
            root, config, transport=llm.ScriptedTransport(world.scripts), verb=kw.get("verb", "")
        ),
    )
    try:
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--json", "claims", "build", "--config", str(path)])
    finally:
        monkey.undo()
    board = json.loads(out.getvalue())
    assert code == 1 and board["ok"] is False
    assert board["built"] == 1 and board["failed"] == 1
    assert (
        list(board["failed_sources"]) == [SOURCE] and "BadReply" in board["failed_sources"][SOURCE]
    )
    reopened = db.Database(tmp_path / "transcript.sqlite")
    try:
        store = Store(reopened)
        # one 6000-character section: the first reply's two claims, both in range
        assert store.claim_count("bee:2") == 2 and store.claim_count(SOURCE) == 0
        assert store.artifact(SOURCE, claims.NODE)["state"] == "failed"
        (entry,) = claims.status(store, world.config, [SOURCE])
        assert entry.state == "stale"
    finally:
        reopened.close()


def test_the_spend_cap_stops_the_pass_before_the_first_call(world):
    capped = world.client(world.config | {"llm": {"max_usd_per_run": 0.0}})
    with pytest.raises(llm.SpendCapReached, match="per-run cap"):
        claims.build(world.store, capped, SOURCE, created_by="test")
    assert world.store.claim_count() == 0 and world.store.artifact(SOURCE, claims.NODE) is None


def test_best_segments_prefer_the_cleaned_ones_when_a_source_has_them(world):
    assert world.store.best_segments(SOURCE) == SEGMENTS
    cleaned = [SEGMENTS[0] | {"text": "a choice is right as of now, cleaned"}]
    world.store.replace_cleaned_segments(SOURCE, cleaned)
    assert world.store.best_segments(SOURCE) == cleaned
    assert claims.inputs(world.store, SOURCE, world.config)["text"] == claims.text_sha256(cleaned)


def test_replace_claims_rebuilds_whole_and_counts_by_kind(world):
    first = [{"section": 0, "segments": [0], "text": "One.", "kind": "assertion"}]
    assert world.store.replace_claims(SOURCE, first, build_key="k", created_by="test") == 1
    second = [
        {"section": 0, "segments": [1], "text": "Two.", "kind": "story", "speaker": "Wren"},
        {"section": 1, "segments": [2, 3], "text": "Three.", "kind": "story"},
    ]
    assert world.store.replace_claims(SOURCE, second, build_key="k2", created_by="test") == 2
    assert [row["text"] for row in world.store.claims(SOURCE)] == ["Two.", "Three."]
    assert world.store.claim_kind_counts() == {"story": 2}
    assert world.store.claim_counts_by_source() == {SOURCE: 2}
    with pytest.raises(sqlite3.IntegrityError):
        world.store.replace_claims(
            SOURCE, [first[0] | {"kind": "guess"}], build_key="k", created_by="test"
        )
    assert world.store.delete_claims(SOURCE) == 2


def test_the_claim_kinds_are_the_reply_schemas_enum():
    enum = schemas.schema("claims_reply")["properties"]["claims"]["items"]["properties"]["kind"]
    assert list(db.CLAIM_KINDS) == enum["enum"]


# ---- ytlm's digests ----------------------------------------------------------------------

VIDEO = "Xq3vT9bLk2A"


def _digest(folder: Path, video: str, statements: list[tuple[str, list[int], str]]) -> Path:
    path = folder / video / "digest.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "video_id": video,
                "schema": "ytlm.digest/1",
                "topics": [],
                "people": [],
                "perspective": "",
                "dropped": 0,
                "claims": [
                    {
                        "id": f"{video}:c{index:03d}",
                        "statement": statement,
                        "speaker": "",
                        "segments": segments,
                        "kind": kind,
                        "date": "",
                        "t0": 0.0,
                    }
                    for index, (statement, segments, kind) in enumerate(statements, 1)
                ],
                "provenance": {
                    "transcript_sha256": "t" * 64,
                    "prompt_sha256": "p" * 64,
                    "summary_prompt_sha256": "s" * 64,
                    "model": MODEL,
                    "section_chars": 40,
                    "tokens_in": 1,
                    "tokens_out": 1,
                    "cost_usd": 0.01,
                    "calls": [],
                },
            }
        )
    )
    return path


def test_an_imported_source_stays_imported_and_is_not_billed(world, tmp_path):
    world.store.add_source(VIDEO, "u", created_by="test", kind="youtube")
    world.store.replace_segments(VIDEO, SEGMENTS)
    world.store.add_source("other", "u2", created_by="test", kind="youtube")
    videos = tmp_path / "videos"
    _digest(
        videos,
        VIDEO,
        [
            ("Time is the counting of beats.", [0, 1], "assertion"),
            ("Rank is a state.", [2], "definition"),
            ("A claim citing nothing the database holds.", [40], "prediction"),
        ],
    )
    sources = [VIDEO, "other"]
    preview = claims.import_digests(world.store, videos, sources, created_by="test", dry_run=True)
    assert preview == {"imported": 1, "claims": 2, "dropped": 1, "skipped": 0, "missing": 1}
    assert world.store.claim_count() == 0 and world.store.artifact(VIDEO, claims.NODE) is None
    out = claims.import_digests(world.store, videos, sources, created_by="test", dry_run=False)
    assert out == preview
    rows = world.store.claims(VIDEO)
    assert [(row["section"], row["segments"], row["kind"]) for row in rows] == [
        (0, [0, 1], "assertion"),
        (1, [2], "definition"),
    ]
    artifact = world.store.artifact(VIDEO, claims.NODE)
    assert json.loads(artifact["provenance"])["imported_from"] == "ytlm.digest"
    assert json.loads(artifact["inputs"])["prompt"] == "p" * 64
    entries = {
        entry.source_id: entry for entry in claims.status(world.store, world.config, sources)
    }
    assert (entries[VIDEO].state, entries[VIDEO].claims) == ("imported", 2)
    assert entries["other"].state == "none"
    estimate = claims.estimate(world.store, world.client(), [VIDEO])
    assert (estimate.sources, estimate.calls) == (0, 0)
    again = claims.import_digests(world.store, videos, sources, created_by="test", dry_run=False)
    assert (again["imported"], again["skipped"]) == (0, 1)


# ---- the v9 -> v10 step -------------------------------------------------------------------


def test_a_version_9_database_gains_the_claims_table_after_a_verified_backup(tmp_path):
    path = tmp_path / "transcript.sqlite"
    first = db.Database(path)
    Store(first).add_source(SOURCE, "l", created_by="test", kind="bee")
    Store(first).replace_segments(SOURCE, SEGMENTS)
    first.close()
    connection = sqlite3.connect(path)
    for statement in ("DROP INDEX claims_source", "DROP TABLE claims", "PRAGMA user_version = 9"):
        connection.execute(statement)
    connection.commit()
    connection.close()
    opened = db.Database(path)
    try:
        assert opened.migration["from"] == 9 and opened.migration["to"] == db.USER_VERSION == 13
        assert opened.migration["backup"].endswith("-before-v13.sqlite")
        assert opened.one("PRAGMA integrity_check")[0] == "ok"
        assert opened.one("SELECT COUNT(*) FROM claims")[0] == 0
        assert opened.one("SELECT COUNT(*) FROM segments")[0] == len(SEGMENTS)
        store = Store(opened)
        store.replace_claims(
            SOURCE,
            [{"section": 0, "segments": [0], "text": "One.", "kind": "assertion"}],
            build_key="k",
            created_by="test",
        )
        assert store.claim_count(SOURCE) == 1
    finally:
        opened.close()
    copy = sqlite3.connect(opened.migration["backup"])
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 9
    copy.close()
