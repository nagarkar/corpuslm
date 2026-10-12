"""The generic cleanup builders (`src/corpuslm/chain.py`): a source kind that is not
YouTube or Bee runs the chain, a second run builds and rewrites nothing, the names a tool
stores come from its shape, and the input hashes are byte-equal to the ones ytlm computed
before the builders moved here. Sanitized synthetic text only."""

import hashlib
import json
import os

import pytest

from corpuslm import basic_cleanup, chain, db, transcript
from corpuslm.store import Store

SOURCE = "any-source"
SEGMENTS = [
    {"idx": 0, "t0": 0.0, "t1": 1.0, "speaker": "", "text": "quill feather spoke about karma"},
    {"idx": 1, "t0": 1.0, "t1": 2.0, "speaker": "", "text": "and the teh thing"},
]


class FakeKind:
    """A source kind of its own: segments held in memory, vocabulary fixed."""

    def __init__(self, folder, *, shape=None, language="en"):
        self.shape = shape or chain.ChainShape()
        self.folder, self.lang = folder, language
        self.segments = list(SEGMENTS)

    def load_segments(self, source):
        return chain.RawTranscript(
            {"segments": chain.canonical_sha256(self.segments)},
            lambda: (
                transcript.from_segments(
                    source,
                    self.segments,
                    {"language": "en", "generated": None, "fetcher": "test"},
                    "test.transcript/1",
                ),
                {"segments": len(self.segments)},
            ),
        )

    def vocabulary(self, source):
        return basic_cleanup.Vocabulary(terms=["Quillfeather"], substitutions={}, sources={})

    def language(self, source):
        return self.lang

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


@pytest.fixture
def store(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    repository = Store(database)
    repository.add_source(SOURCE, "any-locator", created_by="test", kind="bee")
    yield repository
    database.close()


def _run(chain_instance):
    first = chain.build_normalize(chain_instance, SOURCE)
    second = chain.build_protected_vocabulary(chain_instance, SOURCE, first.sha256)
    third = chain.build_spell_fix(chain_instance, SOURCE, second.sha256)
    return first, second, third


def test_canonical_sha256_is_byte_equal_to_the_hash_ytlm_computed_before():
    def old(value):
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    samples = [
        {},
        [],
        {"b": 1, "a": [1, 2.5, None, True], "c": {"z": "é–日本", "y": ""}},
        {"vocabulary": ["Quillfeather", "Śrī"], "language": "en"},
        [{"idx": 0, "t0": 0.0, "t1": 1.5, "speaker": "", "text": 'a "quoted" word'}],
    ]
    assert [chain.canonical_sha256(sample) for sample in samples] == [
        old(sample) for sample in samples
    ]
    assert chain.canonical_sha256 is db.canonical_sha256


def test_the_chain_builds_three_artifacts_and_records_their_inputs(store, tmp_path):
    chain_instance = chain.Chain(store, FakeKind(tmp_path / "ws" / "t"), tmp_path / "ws", "test")
    results = _run(chain_instance)
    assert [result.built for result in results] == [True, True, True]
    rows = {row["node"]: row for row in store.artifacts_of(SOURCE)}
    assert set(rows) == {"normalize", "protected_vocabulary", "spell_fix"}
    assert rows["normalize"]["path"] == f"t/{SOURCE}/normalize.json"
    assert json.loads(rows["spell_fix"]["inputs"]) == {
        "protected_vocabulary": rows["protected_vocabulary"]["sha256"],
        "language": "en",
        "step": "corpuslm.cleanup.spell_fix/1",
    }
    assert rows["spell_fix"]["sha256"] == results[2].sha256
    assert rows["spell_fix"]["created_by"] == "test"


def test_a_second_run_builds_nothing_and_rewrites_nothing(store, tmp_path, monkeypatch):
    chain_instance = chain.Chain(store, FakeKind(tmp_path / "t"), tmp_path, "test")
    first = _run(chain_instance)

    def forbidden(*args, **kwargs):
        raise AssertionError("a current step wrote a file")

    monkeypatch.setattr(os, "replace", forbidden)
    again = _run(chain_instance)
    assert [result.built for result in again] == [False, False, False]
    assert [result.sha256 for result in again] == [result.sha256 for result in first]


def test_changed_segments_rebuild_the_whole_chain(store, tmp_path):
    kind = FakeKind(tmp_path / "t")
    chain_instance = chain.Chain(store, kind, tmp_path, "test")
    _run(chain_instance)
    kind.segments = [*SEGMENTS, {"idx": 2, "t0": 2.0, "t1": 3.0, "speaker": "", "text": "more"}]
    assert [result.built for result in _run(chain_instance)] == [True, True, True]


def test_a_missing_file_is_rebuilt_even_when_its_row_is_current(store, tmp_path):
    kind = FakeKind(tmp_path / "t")
    chain_instance = chain.Chain(store, kind, tmp_path, "test")
    first = _run(chain_instance)
    chain_instance.path(SOURCE, "spell_fix").unlink()
    again = _run(chain_instance)
    assert [result.built for result in again] == [False, False, True]
    assert again[2].sha256 == first[2].sha256


def test_the_shape_names_the_nodes_and_the_step_in_the_inputs(store, tmp_path):
    shape = chain.ChainShape(
        protected_vocabulary="pass1",
        spell_fix="pass2",
        step_key="pass",
        protected_vocabulary_step="cleanup.pass1/1",
        spell_fix_step="cleanup.pass2/1",
        vocabulary_sources_in_provenance=True,
    )
    chain_instance = chain.Chain(store, FakeKind(tmp_path / "t", shape=shape), tmp_path, "test")
    _run(chain_instance)
    rows = {row["node"]: row for row in store.artifacts_of(SOURCE)}
    assert set(rows) == {"normalize", "pass1", "pass2"}
    assert set(json.loads(rows["pass1"]["inputs"])) == {"normalize", "vocabulary", "pass"}
    assert json.loads(rows["pass1"]["inputs"])["pass"] == "cleanup.pass1/1"
    assert set(json.loads(rows["pass2"]["inputs"])) == {"pass1", "language", "pass"}
    assert "sources" in json.loads(rows["pass1"]["provenance"])
    shapeless = chain.Chain(store, FakeKind(tmp_path / "u"), tmp_path, "test")
    _run(shapeless)
    assert "sources" not in json.loads(store.artifact(SOURCE, "protected_vocabulary")["provenance"])


def test_spell_fix_is_skipped_for_another_language_and_recorded(store, tmp_path):
    chain_instance = chain.Chain(store, FakeKind(tmp_path / "t", language="hi"), tmp_path, "test")
    _run(chain_instance)
    row = store.artifact(SOURCE, "spell_fix")
    assert json.loads(row["provenance"])["status"] == "skipped"


def test_write_if_changed_writes_only_when_the_bytes_differ(tmp_path, monkeypatch):
    target = tmp_path / "a" / "doc.json"
    sha256 = chain.write_if_changed(target, b"one")
    assert sha256 == hashlib.sha256(b"one").hexdigest()
    before = target.stat().st_ino
    assert chain.write_if_changed(target, b"one") == sha256
    assert target.stat().st_ino == before
    assert chain.write_if_changed(target, b"two") == hashlib.sha256(b"two").hexdigest()
    assert target.read_bytes() == b"two"
    assert [path.name for path in target.parent.iterdir()] == ["doc.json"]


def test_is_current_needs_a_built_row_with_equal_inputs(store, tmp_path):
    assert not chain.is_current(store, SOURCE, "normalize", {"a": 1})
    chain.record(
        store, SOURCE, "normalize", root=tmp_path, path=tmp_path / "x" / "n.json", sha256="s",
        inputs={"a": 1}, provenance={}, created_by="test",
    )  # fmt: skip
    assert store.artifact(SOURCE, "normalize")["path"] == "x/n.json"
    assert chain.is_current(store, SOURCE, "normalize", {"a": 1})
    assert not chain.is_current(store, SOURCE, "normalize", {"a": 2})
    chain.record(
        store, SOURCE, "normalize", root=tmp_path, path=None, sha256="", inputs={"a": 1},
        provenance={}, created_by="test", state="failed",
    )  # fmt: skip
    assert store.artifact(SOURCE, "normalize")["path"] == ""
    assert not chain.is_current(store, SOURCE, "normalize", {"a": 1})


# ---- the steps after the AI cleanup ---------------------------------------------------


class CountingReport:
    def __init__(self):
        self.counts, self.errors, self.notes, self.stopped_by = {}, {}, {}, ""

    def count(self, node, amount=1):
        self.counts[node] = self.counts.get(node, 0) + amount


def _through_model_cleanup(store, tmp_path):
    """The chain up to spell fix, with the spell-fix document standing in for the model's."""
    chain_instance = chain.Chain(store, FakeKind(tmp_path / "t"), tmp_path, "test")
    _, _, third = _run(chain_instance)
    document = chain_instance.load(SOURCE, "spell_fix")
    document["segments"][0]["role"] = "Quill"
    document["segments"][1]["role"] = "Speaker 2"
    path = chain_instance.path(SOURCE, "model_cleanup")
    sha256 = chain.write_if_changed(path, chain_instance.dumps(document))
    chain.record(
        store,
        SOURCE,
        "model_cleanup",
        root=tmp_path,
        path=path,
        sha256=sha256,
        inputs={"spell_fix": third.sha256},
        provenance={},
        created_by="test",
    )
    return chain_instance, sha256


def test_the_export_builders_build_once_and_then_nothing(store, tmp_path):
    from corpuslm.speaker_map import bee_scheme

    scheme = bee_scheme("Quill")
    chain_instance, ai_cleanup_sha256 = _through_model_cleanup(store, tmp_path)
    report = CountingReport()
    speaker_map = chain.build_speaker_map(
        chain_instance,
        SOURCE,
        ai_cleanup_sha256,
        scheme=scheme,
        extra_inputs={"metadata": "m1"},
        report=report,
    )
    assert speaker_map["roles"]["Quill"] == {"name": "Quill", "status": "RESOLVED_FROM_METADATA"}
    assert speaker_map["roles"]["Speaker 2"]["status"] == "NEEDS_AUTHOR"
    row = store.artifact(SOURCE, "speaker_map")
    assert json.loads(row["inputs"]) == {
        "model_cleanup": ai_cleanup_sha256,
        "metadata": "m1",
        "verdicts": chain.canonical_sha256({}),
        "step": "corpuslm.speaker_map/1",
    }
    document, transcript_sha256 = chain.publish_best(
        chain_instance,
        SOURCE,
        speaker_map,
        preference=("model_cleanup", "spell_fix"),
        scheme=scheme,
    )
    assert chain.build_subtitles(
        chain_instance, SOURCE, document, transcript_sha256, max_line_chars=42, report=report
    )
    assert chain.build_search_index(
        chain_instance, SOURCE, document, transcript_sha256, report=report
    )
    assert store.artifact(SOURCE, "search_index")["sha256"] == transcript_sha256
    again = CountingReport()
    chain.build_speaker_map(
        chain_instance,
        SOURCE,
        ai_cleanup_sha256,
        scheme=scheme,
        extra_inputs={"metadata": "m1"},
        report=again,
    )
    assert not chain.build_subtitles(
        chain_instance, SOURCE, document, transcript_sha256, max_line_chars=42
    )
    assert not chain.build_search_index(chain_instance, SOURCE, document, transcript_sha256)
    assert again.counts == {}
    assert report.counts == {"speaker_map": 1, "subtitles": 1, "search_index": 1}
    srt = (tmp_path / "t" / SOURCE / "subtitles.srt").read_text()
    assert srt.startswith("1\n00:00:00")


def test_a_subtitle_width_change_rebuilds_only_the_subtitles(store, tmp_path):
    from corpuslm.speaker_map import bee_scheme

    chain_instance, ai_cleanup_sha256 = _through_model_cleanup(store, tmp_path)
    document, sha256 = chain.publish_best(
        chain_instance, SOURCE, None, preference=("model_cleanup",), scheme=bee_scheme("Quill")
    )
    assert chain.build_subtitles(chain_instance, SOURCE, document, sha256, max_line_chars=42)
    assert not chain.build_subtitles(chain_instance, SOURCE, document, sha256, max_line_chars=42)
    assert chain.build_subtitles(chain_instance, SOURCE, document, sha256, max_line_chars=20)


def test_publish_best_takes_the_first_built_node_and_applies_the_speaker_map(store, tmp_path):
    from corpuslm.speaker_map import bee_scheme

    scheme = bee_scheme("Quill")
    chain_instance, _ = _through_model_cleanup(store, tmp_path)
    speaker_map = {
        "source_id": SOURCE,
        "candidates": [],
        "roles": {
            "Quill": {"name": "Quill Q", "status": "AUTHOR"},
            "Speaker 2": {"name": "Ann", "status": "AUTHOR"},
        },
    }
    document, sha256 = chain.publish_best(
        chain_instance,
        SOURCE,
        speaker_map,
        preference=("model_cleanup", "spell_fix"),
        scheme=scheme,
    )
    assert [segment["speaker"] for segment in document["segments"]] == ["Quill Q", "Ann"]
    path = tmp_path / "t" / SOURCE / "transcript.json"
    assert chain.canonical_sha256(path.read_text()) and sha256 == chain.write_if_changed(
        path, chain_instance.dumps(document)
    )
    # the node ahead of the others is stale or missing: the next one is taken
    document, _ = chain.publish_best(
        chain_instance, SOURCE, None, preference=("never_built", "spell_fix"), scheme=scheme
    )
    assert document["segments"][0].get("role") is None
    with pytest.raises(chain.schemas.SchemaError):
        chain.publish_best(chain_instance, SOURCE, None, preference=("never_built",), scheme=scheme)
