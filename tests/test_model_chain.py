"""The model clean-up builder (`src/corpuslm/model_chain.py`) for a source kind that is not
YouTube: Bee's scheme with fixed labels, scripted replies, no vendor ever called. Sanitized
synthetic text only."""

import json
from datetime import UTC, datetime

import pytest

from corpuslm import chain, cleanup, db, llm, model_chain, review, transcript
from corpuslm.speaker_map import bee_scheme
from corpuslm.store import Store

SOURCE = "any-source"
MODEL = "gemini/gemini-2.5-flash-lite"
SEGMENTS = [
    {"idx": i, "t0": float(i), "t1": float(i + 1), "speaker": "Quill", "text": f"line {i} here"}
    for i in range(6)
]
EMPTY_REPLY = {"edits": {}, "ambiguities": [], "bindings": []}


class FakeKind:
    shape = chain.ChainShape(source_label="a test conversation")

    def __init__(self, folder):
        self.folder = folder

    def load_segments(self, source):
        return chain.RawTranscript(
            {"segments": chain.canonical_sha256(SEGMENTS)},
            lambda: (
                transcript.from_segments(
                    source,
                    SEGMENTS,
                    {"language": "en", "generated": None, "fetcher": "test"},
                    "test.transcript/1",
                    speaker_source="diarized",
                ),
                {"segments": len(SEGMENTS)},
            ),
        )

    def vocabulary(self, source):
        return cleanup.Vocabulary(terms=["Quillfeather"], substitutions={}, sources={})

    def language(self, source):
        return "en"

    def workspace_dir(self, source):
        return self.folder / source

    def file_name(self, node):
        return f"{node}.json"

    def refined_schema(self):
        return transcript.with_free_labels()

    def row(self, source):
        return {"id": source}

    def export_file_name(self, source, node):
        return {"transcript": "transcript.json", "subtitles": "subtitles.srt"}.get(
            node, f"{node}.json"
        )

    def validate(self, name, document):
        pass


class Report:
    def __init__(self):
        self.counts, self.errors, self.notes, self.stopped_by = {}, {}, {}, None

    def count(self, node, n=1):
        self.counts[node] = self.counts.get(node, 0) + n


class Verdicts(model_chain.NoReview):
    def __init__(self):
        self.value = {}

    def verdicts(self, source):
        return {k: {"verdict": v} for k, v in self.value.items()}


class World:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.database = db.Database(tmp_path / "transcript.sqlite")
        self.store = Store(self.database)
        self.store.add_source(SOURCE, "locator", created_by="test", kind="bee")
        self.chain = chain.Chain(self.store, FakeKind(tmp_path / "t"), tmp_path, "test")
        self.config = {
            "cleanup": {"model": MODEL, "batch_tokens": 30, "overlap_segments": 1},
            "llm": {},
            "logs": {},
        }
        self.review = Verdicts()
        self.spending = model_chain.Spending()
        self.report = Report()
        self.scripts = tmp_path / "scripts"
        self.clients = []
        normalized = chain.build_normalize(self.chain, SOURCE)
        protected = chain.build_protected_vocabulary(self.chain, SOURCE, normalized.sha)
        self.spell_fix = chain.build_spell_fix(self.chain, SOURCE, protected.sha).sha

    def script(self, tag, reply, tokens=(100, 20)):
        folder = self.scripts / "model_cleanup"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{tag}.json").write_text(
            json.dumps(
                {"reply": json.dumps(reply), "tokens_in": tokens[0], "tokens_out": tokens[1]}
            )
        )

    def context(self, **changes):
        def client():
            made = llm.Client(
                self.root,
                self.config,
                transport=llm.ScriptedTransport(self.scripts),
                clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            )
            self.clients.append(made)
            return made

        base = {
            "chain": self.chain,
            "scheme": bee_scheme("Quill"),
            "config": self.config,
            "client": client,
            "spending": self.spending,
            "review": self.review,
        }
        return model_chain.ModelCleanup(**(base | changes))

    def build(self, **changes):
        return model_chain.build_model_cleanup(
            self.context(**changes), SOURCE, self.spell_fix, self.report
        )

    def row(self):
        return self.store.artifact(SOURCE, "model_cleanup")

    def batches(self):
        return model_chain.model_cleanup.batches(
            self.chain.load(SOURCE, "spell_fix"), batch_tokens=30, overlap_segments=1
        )


@pytest.fixture
def world(tmp_path):
    made = World(tmp_path)
    yield made
    made.database.close()


def script_all(world, reply=EMPTY_REPLY, tokens=(100, 20)):
    batches = world.batches()
    assert len(batches) >= 2
    for batch in batches:
        world.script(f"{SOURCE}-{batch.tag_suffix}", reply, tokens)
    return batches


def test_the_shape_names_the_node_the_stage_and_the_inputs(world):
    script_all(world)
    sha = world.build()
    row = world.row()
    assert sha == row["sha256"] and row["state"] == "built"
    assert row["path"] == f"t/{SOURCE}/model_cleanup.json"
    inputs = json.loads(row["inputs"])
    assert inputs["spell_fix"] == world.spell_fix
    assert inputs["step"] == "corpuslm.model_cleanup/1"
    assert {"prompt", "model", "batch_tokens", "overlap_segments", "verdicts", "memory"} <= set(
        inputs
    )
    assert world.report.counts == {"model_cleanup": 1}
    lines = (world.root / "logs" / "usage.jsonl").read_text().splitlines()
    assert [json.loads(line)["stage"] for line in lines] == ["model_cleanup"] * len(world.batches())


def test_a_second_build_asks_nothing_and_rewrites_nothing(world):
    script_all(world)
    first = world.build()
    calls = sum(client.calls for client in world.clients)
    path = world.chain.path(SOURCE, "model_cleanup")
    before = path.stat().st_mtime_ns
    assert world.build() == first
    assert sum(client.calls for client in world.clients) == calls
    assert path.stat().st_mtime_ns == before


def test_fixed_labels_keep_the_sources_speaker_source(world):
    script_all(world)
    world.build()
    document = world.chain.load(SOURCE, "model_cleanup")
    assert document["state"] == "LLM-CLEANED"
    assert document["speaker_source"] == "diarized"
    assert [seg["speaker"] for seg in document["segments"]] == ["Quill"] * 6


def test_a_changed_prompt_applies_forward_only_and_a_patch_input_replays_the_cache(world):
    script_all(world)
    built = world.build()
    calls = sum(client.calls for client in world.clients)
    # a CONFIG input changes (the model): the build is held, nothing is asked
    world.config["cleanup"]["model"] = "gemini/gemini-2.5-flash"
    assert world.build() == built
    assert world.row()["state"] == "built"
    assert sum(client.calls for client in world.clients) == calls
    # a PATCH input changes while the build is held: replayed from the cache with no script left
    for script in (world.scripts / "model_cleanup").glob("*.json"):
        script.unlink()
    world.review.value = {"amb-0001": "x"}
    replayed = world.build()
    row = world.row()
    assert replayed == row["sha256"]
    provenance = json.loads(row["provenance"])
    assert provenance["replayed"] is True and provenance["cached"] == len(world.batches())
    assert json.loads(row["inputs"])["model"] == MODEL  # the held build's own model
    assert world.report.counts["model_cleanup_replayed"] == 1


def test_the_run_cap_stops_the_step_and_marks_the_rest_failed(world):
    script_all(world, tokens=(2_000_000, 2_000_000))
    world.config["llm"] = {"max_usd_per_run": 0.1}
    assert world.build() is None
    row = world.row()
    assert row["state"] == "failed"
    assert json.loads(row["provenance"])["transient"] is True
    assert world.report.stopped_by == llm.RUN_CAP
    assert world.spending.cap == llm.RUN_CAP
    assert world.report.counts["model_cleanup_budget"] == 1
    assert "budget" in world.report.errors[SOURCE]
    # a later source in the same run is declined without a call
    calls = sum(client.calls for client in world.clients)
    world.config["llm"] = {}
    assert world.build() is None
    assert sum(client.calls for client in world.clients) == calls
    assert world.row()["state"] == "failed"


def test_a_reply_outside_its_batch_fails_the_source_transiently(world):
    batches = world.batches()
    bad = {"edits": {}, "ambiguities": [], "bindings": [{"role": "Quill", "name": "Q", "idx": 99}]}
    world.script(f"{SOURCE}-{batches[0].tag_suffix}", bad)
    assert world.build() is None
    row = world.row()
    assert row["state"] == "failed"
    provenance = json.loads(row["provenance"])
    assert provenance["transient"] is True and provenance["reason"].startswith("bad_reply")
    assert not list((world.root / llm.CACHE_DIR).glob("*.json"))
    assert world.report.counts["model_cleanup_failed"] == 1


def test_a_source_the_tool_declines_is_skipped_with_its_reason(world):
    assert world.build(declined=lambda source: "turned off for this source") is None
    row = world.row()
    assert row["state"] == "skipped"
    assert json.loads(row["provenance"]) == {"reason": "turned off for this source"}
    assert world.clients == []


def test_an_absent_section_skips_the_step_and_calls_no_model(world):
    del world.config["cleanup"]
    assert world.build() is None
    assert json.loads(world.row()["provenance"])["reason"] == "[cleanup] is absent from config.toml"
    assert world.clients == []


def test_the_estimate_calls_no_model_and_counts_what_would_be_billed(world):
    ctx = world.context()
    result = model_chain.estimate(ctx, {SOURCE: world.spell_fix})
    assert result.sources == 1 and result.batches == len(world.batches())
    assert result.tokens_in > 0 and result.usd is not None and result.usd > 0
    assert result.model == MODEL
    assert world.clients == []
    script_all(world)
    world.build()
    done = model_chain.estimate(world.context(), {SOURCE: world.spell_fix})
    assert done.sources == 0 and done.batches == 0 and done.usd == 0


def test_a_recorded_verdict_changes_the_inputs_and_a_term_rule_is_applied(world):
    script_all(world)
    reviewing = model_chain.DatabaseReview(world.store)
    assert reviewing.memory_vocabulary().terms == []
    world.build(review=reviewing)
    before = json.loads(world.row()["inputs"])
    asked = sum(client.calls for client in world.clients)
    review.record_verdict(
        world.database,
        source_id=SOURCE,
        key="amb-0001",
        kind="ambiguity",
        verdict="Quillfeather",
        span="quill feather",
        created_by="test",
    )
    assert reviewing.memory_vocabulary().terms == ["Quillfeather"]
    world.build(review=reviewing)
    after = json.loads(world.row()["inputs"])
    assert after["verdicts"] != before["verdicts"] and after["memory"] != before["memory"]
    assert after["prompt"] == before["prompt"]
    assert sum(client.calls for client in world.clients) == asked  # served from the cache
