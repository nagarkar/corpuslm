"""The generic clean-up builders (`src/corpuslm/chain.py`): a source kind that is not
YouTube or Bee runs the chain, a second run builds and rewrites nothing, the names a tool
stores come from its shape, and the input hashes are byte-equal to the ones ytlm computed
before the builders moved here. Sanitized synthetic text only."""

import hashlib
import json
import os

import pytest

from corpuslm import chain, cleanup, db, transcript
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
        return cleanup.Vocabulary(terms=["Quillfeather"], substitutions={}, sources={})

    def language(self, source):
        return self.lang

    def workspace_dir(self, source):
        return self.folder / source

    def file_name(self, node):
        return f"{node}.json"

    def refined_schema(self):
        return None


@pytest.fixture
def store(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    repository = Store(database)
    repository.add_source(SOURCE, "any-locator", created_by="test", kind="bee")
    yield repository
    database.close()


def _run(c):
    first = chain.build_normalize(c, SOURCE)
    second = chain.build_protected_vocabulary(c, SOURCE, first.sha)
    third = chain.build_spell_fix(c, SOURCE, second.sha)
    return first, second, third


def test_canonical_sha256_is_byte_equal_to_the_hash_ytlm_computed_before():
    def old(obj):
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    samples = [
        {},
        [],
        {"b": 1, "a": [1, 2.5, None, True], "c": {"z": "é–日本", "y": ""}},
        {"vocabulary": ["Quillfeather", "Śrī"], "language": "en"},
        [{"idx": 0, "t0": 0.0, "t1": 1.5, "speaker": "", "text": 'a "quoted" word'}],
    ]
    assert [chain.canonical_sha256(s) for s in samples] == [old(s) for s in samples]
    assert chain.canonical_sha256 is db.canonical_sha256


def test_the_chain_builds_three_artifacts_and_records_their_inputs(store, tmp_path):
    c = chain.Chain(store, FakeKind(tmp_path / "ws" / "t"), tmp_path / "ws", "test")
    results = _run(c)
    assert [r.built for r in results] == [True, True, True]
    rows = {r["node"]: r for r in store.artifacts_of(SOURCE)}
    assert set(rows) == {"normalize", "protected_vocabulary", "spell_fix"}
    assert rows["normalize"]["path"] == f"t/{SOURCE}/normalize.json"
    assert json.loads(rows["spell_fix"]["inputs"]) == {
        "protected_vocabulary": rows["protected_vocabulary"]["sha256"],
        "language": "en",
        "step": "corpuslm.cleanup.spell_fix/1",
    }
    assert rows["spell_fix"]["sha256"] == results[2].sha
    assert rows["spell_fix"]["created_by"] == "test"


def test_a_second_run_builds_nothing_and_rewrites_nothing(store, tmp_path, monkeypatch):
    c = chain.Chain(store, FakeKind(tmp_path / "t"), tmp_path, "test")
    first = _run(c)

    def forbidden(*args, **kwargs):
        raise AssertionError("a current step wrote a file")

    monkeypatch.setattr(os, "replace", forbidden)
    again = _run(c)
    assert [r.built for r in again] == [False, False, False]
    assert [r.sha for r in again] == [r.sha for r in first]


def test_changed_segments_rebuild_the_whole_chain(store, tmp_path):
    kind = FakeKind(tmp_path / "t")
    c = chain.Chain(store, kind, tmp_path, "test")
    _run(c)
    kind.segments = [*SEGMENTS, {"idx": 2, "t0": 2.0, "t1": 3.0, "speaker": "", "text": "more"}]
    assert [r.built for r in _run(c)] == [True, True, True]


def test_a_missing_file_is_rebuilt_even_when_its_row_is_current(store, tmp_path):
    kind = FakeKind(tmp_path / "t")
    c = chain.Chain(store, kind, tmp_path, "test")
    first = _run(c)
    c.path(SOURCE, "spell_fix").unlink()
    again = _run(c)
    assert [r.built for r in again] == [False, False, True]
    assert again[2].sha == first[2].sha


def test_the_shape_names_the_nodes_and_the_step_in_the_inputs(store, tmp_path):
    shape = chain.ChainShape(
        protected_vocabulary="pass1",
        spell_fix="pass2",
        step_key="pass",
        protected_vocabulary_step="cleanup.pass1/1",
        spell_fix_step="cleanup.pass2/1",
        vocabulary_sources_in_provenance=True,
    )
    c = chain.Chain(store, FakeKind(tmp_path / "t", shape=shape), tmp_path, "test")
    _run(c)
    rows = {r["node"]: r for r in store.artifacts_of(SOURCE)}
    assert set(rows) == {"normalize", "pass1", "pass2"}
    assert set(json.loads(rows["pass1"]["inputs"])) == {"normalize", "vocabulary", "pass"}
    assert json.loads(rows["pass1"]["inputs"])["pass"] == "cleanup.pass1/1"
    assert set(json.loads(rows["pass2"]["inputs"])) == {"pass1", "language", "pass"}
    assert "sources" in json.loads(rows["pass1"]["provenance"])
    shapeless = chain.Chain(store, FakeKind(tmp_path / "u"), tmp_path, "test")
    _run(shapeless)
    assert "sources" not in json.loads(store.artifact(SOURCE, "protected_vocabulary")["provenance"])


def test_spell_fix_is_skipped_for_another_language_and_recorded(store, tmp_path):
    c = chain.Chain(store, FakeKind(tmp_path / "t", language="hi"), tmp_path, "test")
    _run(c)
    row = store.artifact(SOURCE, "spell_fix")
    assert json.loads(row["provenance"])["status"] == "skipped"


def test_write_if_changed_writes_only_when_the_bytes_differ(tmp_path, monkeypatch):
    target = tmp_path / "a" / "doc.json"
    sha = chain.write_if_changed(target, b"one")
    assert sha == hashlib.sha256(b"one").hexdigest()
    before = target.stat().st_ino
    assert chain.write_if_changed(target, b"one") == sha
    assert target.stat().st_ino == before
    assert chain.write_if_changed(target, b"two") == hashlib.sha256(b"two").hexdigest()
    assert target.read_bytes() == b"two"
    assert [p.name for p in target.parent.iterdir()] == ["doc.json"]


def test_is_current_needs_a_built_row_with_equal_inputs(store, tmp_path):
    assert not chain.is_current(store, SOURCE, "normalize", {"a": 1})
    chain.record(
        store, SOURCE, "normalize", root=tmp_path, path=tmp_path / "x" / "n.json", sha="s",
        inputs={"a": 1}, provenance={}, created_by="test",
    )  # fmt: skip
    assert store.artifact(SOURCE, "normalize")["path"] == "x/n.json"
    assert chain.is_current(store, SOURCE, "normalize", {"a": 1})
    assert not chain.is_current(store, SOURCE, "normalize", {"a": 2})
    chain.record(
        store, SOURCE, "normalize", root=tmp_path, path=None, sha="", inputs={"a": 1},
        provenance={}, created_by="test", state="failed",
    )  # fmt: skip
    assert store.artifact(SOURCE, "normalize")["path"] == ""
    assert not chain.is_current(store, SOURCE, "normalize", {"a": 1})
