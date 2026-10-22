"""The AI cleanup builder (`src/corpuslm/ai_cleanup_chain.py`) for a source kind that is not
YouTube: Bee's scheme with fixed labels, scripted replies, no vendor ever called. Sanitized
synthetic text only."""

import json
from datetime import UTC, datetime

import pytest

from corpuslm import ai_cleanup_chain, basic_cleanup, chain, db, llm, review, transcript
from corpuslm.speaker_map import bee_scheme
from corpuslm.store import Store

SOURCE = "any-source"
MODEL = "gemini/gemini-2.5-flash-lite"
SEGMENTS = [
    {
        "idx": segment_index,
        "t0": float(segment_index),
        "t1": float(segment_index + 1),
        "speaker": "Quill",
        "text": f"line {segment_index} here",
    }
    for segment_index in range(6)
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
        return basic_cleanup.Vocabulary(terms=["Quillfeather"], substitutions={}, sources={})

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
        self.counts, self.errors, self.notes, self.stopped_by = {}, {}, {}, ""

    def count(self, node, amount=1):
        self.counts[node] = self.counts.get(node, 0) + amount


class Verdicts(ai_cleanup_chain.NoReview):
    def __init__(self):
        self.value = {}

    def verdicts(self, source):
        return {key: {"verdict": verdict} for key, verdict in self.value.items()}


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
        self.spend_cap_stop = ai_cleanup_chain.SpendCapStop()
        self.report = Report()
        self.scripts = tmp_path / "scripts"
        self.clients = []
        normalized = chain.build_normalize(self.chain, SOURCE)
        protected = chain.build_protected_vocabulary(self.chain, SOURCE, normalized.sha256)
        self.spell_fix = chain.build_spell_fix(self.chain, SOURCE, protected.sha256).sha256

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
            made = llm.client(
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
            "spend_cap_stop": self.spend_cap_stop,
            "review": self.review,
        }
        return ai_cleanup_chain.AiCleanupSetup(**(base | changes))

    def build(self, **changes):
        return ai_cleanup_chain.build_ai_cleanup(
            self.context(**changes), SOURCE, self.spell_fix, self.report
        )

    def row(self):
        return self.store.artifact(SOURCE, "model_cleanup")

    def batches(self):
        return ai_cleanup_chain.ai_cleanup.batches(
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
    sha256 = world.build()
    row = world.row()
    assert sha256 == row["sha256"] and row["state"] == "built"
    assert row["path"] == f"t/{SOURCE}/model_cleanup.json"
    inputs = json.loads(row["inputs"])
    assert inputs["spell_fix"] == world.spell_fix
    assert inputs["step"] == "corpuslm.model_cleanup/1"
    assert {"prompt", "model", "batch_tokens", "overlap_segments", "verdicts", "memory"} <= set(
        inputs
    )
    assert inputs["memory_substitution"] == "none"
    assert world.report.counts == {"model_cleanup": 1}
    lines = (world.root / "logs" / "usage.jsonl").read_text().splitlines()
    assert [json.loads(line)["stage"] for line in lines] == ["model_cleanup"] * len(world.batches())


def test_source_cleaned_is_published_once_the_artifact_is_committed(world):
    from eventlm.events import Events

    bus = Events.fake()
    try:
        seen = {}
        real_publish = bus.publish

        def publish(producer, kind, subject=None, facts=None):
            seen["row"] = world.row() is not None
            seen["in_transaction"] = world.database.conn.in_transaction
            return real_publish(producer, kind, subject, facts)

        bus.publish = publish  # type: ignore[method-assign]
        script_all(world)
        assert world.build(events=bus) is not None
        (event,) = bus.tail()
        assert (event.producer, event.kind, event.subject) == (
            "corpuslm", "source.cleaned", {"source": SOURCE},
        )  # fmt: skip
        assert set(event.facts) == {"edits", "ambiguities", "replayed"}
        assert seen == {"row": True, "in_transaction": False}
        # a build that is current publishes nothing again
        assert world.build(events=bus) is not None and len(bus.tail()) == 1
    finally:
        bus.close()


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
    assert [segment["speaker"] for segment in document["segments"]] == ["Quill"] * 6


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
    assert world.spend_cap_stop.cap == llm.RUN_CAP
    assert world.report.counts["model_cleanup_spend_cap"] == 1
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
    ai_cleanup_setup = world.context()
    result = ai_cleanup_chain.estimate(ai_cleanup_setup, {SOURCE: world.spell_fix})
    assert result.sources == 1 and result.batches == len(world.batches())
    assert result.from_cache == 0
    assert result.tokens_in > 0 and result.usd is not None and result.usd > 0
    assert result.model == MODEL
    assert sum(client.calls for client in world.clients) == 0
    script_all(world)
    world.build()
    done = ai_cleanup_chain.estimate(world.context(), {SOURCE: world.spell_fix})
    assert done.sources == 0 and done.batches == 0 and done.usd == 0


def land_a_verdict(world):
    review.record_verdict(
        world.database,
        source_id=SOURCE,
        key="amb-0001",
        kind="ambiguity",
        verdict="Quillfeather",
        span="quill feather",
        created_by="test",
    )


def test_a_landed_verdict_is_rebuilt_from_the_cache_and_the_estimate_says_so(world):
    """A verdict changes the step's inputs; the replies are cached, so the rebuild is free: the
    estimate counts the source under `from_cache`, bills nothing, and the rebuild makes no call."""
    script_all(world)
    reviewing = ai_cleanup_chain.DatabaseReview(world.store)
    first = world.build(review=reviewing)
    land_a_verdict(world)
    before = ai_cleanup_chain.estimate(world.context(review=reviewing), {SOURCE: world.spell_fix})
    assert before.sources == 1 and before.from_cache == 1
    assert (before.batches, before.tokens_in, before.tokens_out, before.usd) == (0, 0, 0, 0.0)
    clients = len(world.clients)
    rebuilt = world.build(review=reviewing)
    assert rebuilt != first  # the transcript changed
    assert [client.calls for client in world.clients[clients:]] == [
        0
    ]  # the transport was never called
    after = ai_cleanup_chain.estimate(world.context(review=reviewing), {SOURCE: world.spell_fix})
    assert after.sources == 0


def test_a_batch_the_cache_lacks_is_billed_with_every_batch_after_it(world):
    script_all(world)
    reviewing = ai_cleanup_chain.DatabaseReview(world.store)
    world.build(review=reviewing)
    calls = json.loads(world.row()["provenance"])["calls"]
    assert len(calls) == 2
    land_a_verdict(world)
    cache = world.root / llm.CACHE_DIR
    (cache / f"{calls[1]['payload_sha256']}.json").unlink()
    result = ai_cleanup_chain.estimate(world.context(review=reviewing), {SOURCE: world.spell_fix})
    assert (result.sources, result.from_cache, result.batches) == (1, 0, 1)
    assert result.usd is not None and result.usd > 0
    (cache / f"{calls[0]['payload_sha256']}.json").unlink()
    result = ai_cleanup_chain.estimate(world.context(review=reviewing), {SOURCE: world.spell_fix})
    assert (result.sources, result.from_cache, result.batches) == (1, 0, 2)


def test_a_term_rule_never_rewrites_a_segment_the_model_did_not_flag(world):
    """nagarkar/ytlm#120: the memory's term rules were substituted into every segment of every
    source ("a" -> "AI" in 202 of 205 YouTube transcripts). An answer fixes its own spot; the
    memory answers a span only where the model flags it again."""
    world.store.add_source("other", "locator", created_by="test", kind="youtube")
    world.store.append_correction(
        span_norm="here",
        verdict="Hereford",
        kind="term",
        origin_source="other",
        origin_key="amb-0001",
        created_by="test",
    )
    script_all(world)
    world.build(review=ai_cleanup_chain.DatabaseReview(world.store))
    document = world.chain.load(SOURCE, "model_cleanup")
    assert [segment["text"] for segment in document["segments"]] == [
        segment["text"] for segment in SEGMENTS
    ]
    assert "memory" not in document.get("passes", {})


def test_a_build_made_with_the_memory_substitution_is_replayed_from_the_cache(world):
    """A build recorded before the substitution was removed has no `memory_substitution`
    input: the next run rebuilds it once, from the reply cache, with no model call."""
    script_all(world)
    reviewing = ai_cleanup_chain.DatabaseReview(world.store)
    world.build(review=reviewing)
    row = world.row()
    old_inputs = json.loads(row["inputs"])
    del old_inputs["memory_substitution"]
    world.database.update("artifacts", row["id"], {"inputs": json.dumps(old_inputs)})
    estimate = ai_cleanup_chain.estimate(world.context(review=reviewing), {SOURCE: world.spell_fix})
    assert (estimate.sources, estimate.from_cache, estimate.usd) == (1, 1, 0.0)
    clients = len(world.clients)
    world.build(review=reviewing)
    assert sum(client.calls for client in world.clients[clients:]) == 0
    assert json.loads(world.row()["inputs"])["memory_substitution"] == "none"


def ambiguous_reply(index):
    return {
        "edits": {},
        "ambiguities": [
            {
                "idx": index,
                "span": "line",
                "candidates": ["line", "lime"],
                "probabilities": [0.25, 0.75],
                "pick": "lime",
            }
        ],
        "bindings": [],
    }


def test_building_the_ai_cleanup_records_its_suggestions(world):
    batches = world.batches()
    for batch in batches:
        world.script(f"{SOURCE}-{batch.tag_suffix}", ambiguous_reply(batch.start))
    sha256 = world.build()
    held = world.store.model_suggestions(SOURCE)
    assert list(held) == [f"amb-{number:04d}" for number in range(1, len(batches) + 1)]
    first = held["amb-0001"]
    assert first["span_norm"] == "line" and first["suggestion"] == "lime"
    assert first["suggestion_probability"] == 0.75
    assert first["candidates"] == ["line", "lime"] and first["probabilities"] == [0.25, 0.75]
    assert first["model"] == MODEL and first["artifact_sha256"] == sha256
    # the artifact file itself is the same bytes the recorded hash names
    assert world.row()["sha256"] == sha256
    # the owner's verdict rebuilds the artifact (new bytes); its rows are replaced, and the
    # suggestion stays the model's pick, not the owner's choice
    reviewing = ai_cleanup_chain.DatabaseReview(world.store)
    review.record_verdict(
        world.database,
        source_id=SOURCE,
        key="amb-0001",
        kind="ambiguity",
        verdict="lime green",
        span="line",
        created_by="test",
    )
    again = world.build(review=reviewing)
    assert again != sha256
    rows = world.store.model_suggestions(SOURCE)
    assert {row["artifact_sha256"] for row in rows.values()} == {again}
    assert len(rows) == len(held) and rows["amb-0001"]["suggestion"] == "lime"
