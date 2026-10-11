"""Chain: the generic builders, the same normalize -> protected vocabulary -> spell fix ->
AI cleanup -> speaker map -> subtitle file -> search index chain for every source kind.

A tool describes its source with a `SourceKind` (where the raw segments come from, the
protected vocabulary, the language, where the files go) and builds a `Chain` over a `Store`.
`build_normalize`, `build_protected_vocabulary` and `build_spell_fix` then each do the same
thing: compute the hashes of the step's inputs, return the recorded artifact when those hashes
and the file are unchanged, otherwise build the document, write it only if its bytes differ and
record the artifact (`Store.record_artifact`). `canonical_sha256`, `write_if_changed`,
`is_current` and `record` are the bookkeeping those builders share, usable by a tool's own
builders too. After the AI cleanup (`corpuslm.ai_cleanup_chain`, which calls a model) the
deterministic builders here finish the chain: `build_speaker_map` (who each label is),
`publish_best` (the best transcript with the speaker map applied), `build_subtitles` (the
subtitle file) and `build_search_index` (the source's rows in the full-text index: the table of
searched segments, or, for a tool whose raw lines live in `segments`, the separate table of
cleaned ones).

What a tool keeps: the segments' origin (a YouTube caption file, Bee's stored segments), the
vocabulary's origin (video metadata, a conversation's title), the names it stores
(`ChainShape`), and the steps after spell fix (the model step, the speaker map, exports).

Never imports: llm, clikit, cli.

Decisions:
- Hashes of inputs are canonical JSON, keys sorted, UTF-8 (`corpuslm.identity.canonical_sha256`,
  also `corpuslm.db.canonical_sha256`). Why: the same text a tool hashed before the builders
  moved here, so no stored artifact becomes stale. Enforced by:
  tests/test_chain.py::test_canonical_sha256_is_byte_equal_to_the_hash_ytlm_computed_before.
- A step whose inputs hash is unchanged and whose file exists builds nothing and rewrites no
  file. Enforced by: tests/test_chain.py::test_a_second_run_builds_nothing_and_rewrites_nothing.
- A file is rewritten only when its bytes differ, through `files.write_atomic`. Enforced by:
  tests/test_chain.py::test_write_if_changed_writes_only_when_the_bytes_differ.
- The names a tool stores (node names, the key and text that name the step in `inputs`) are
  its own `ChainShape`; the builders never hard-code them. Why: ytlm stores `pass1` and `pass2`
  and beelm stores `protected_vocabulary` and `spell_fix`; renaming either would make every
  stored artifact stale. Enforced by:
  tests/test_chain.py::test_the_shape_names_the_nodes_and_the_step_in_the_inputs.
- The speaker map, the subtitle file and the search index follow the same rule: their inputs
  hash names the transcript they were built from, an unchanged input builds nothing, and the
  stored names are the tool's. Enforced by:
  tests/test_chain.py::test_the_export_builders_build_once_and_then_nothing.
- A tool whose raw lines live in `segments` (beelm) names `cleaned_segments` as its search table:
  `build_search_index` then writes the cleaned rows and leaves `segments` as it found them; a
  tool that names nothing keeps writing `segments`. Enforced by:
  tests/test_chain.py::test_a_cleaned_search_table_is_filled_and_the_raw_segments_are_not_touched.
- `publish_best` writes the best transcript available (the first node of the preference that
  is built, with the speaker map applied) only when its bytes change. Enforced by:
  tests/test_chain.py::test_publish_best_takes_the_first_built_node_and_applies_the_speaker_map.

Terms:
- **source kind**: the origin-specific half of the chain: YouTube video or Bee conversation
  (`SourceKind`).
- **chain shape**: the names a source kind stores its chain under (`ChainShape`).
- **report**: what a run keeps while it builds: counts per node, errors and notes per source
  (`Report`); a tool's run board is made from it.
- **built**: a builder's result says whether this call built the artifact (`Built.built`) or
  found it current.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import basic_cleanup, files, schemas, transcript
from . import speaker_map as speaker_map_module
from .db import loads
from .identity import canonical_sha256
from .speaker_map import SpeakerScheme
from .store import Store

__all__ = [
    "SEARCH_TABLES",
    "Built",
    "Chain",
    "ChainShape",
    "RawTranscript",
    "Report",
    "SourceKind",
    "build_normalize",
    "build_protected_vocabulary",
    "build_search_index",
    "build_speaker_map",
    "build_spell_fix",
    "build_subtitles",
    "best_transcript",
    "canonical_sha256",
    "is_current",
    "publish_best",
    "record",
    "search_index_current",
    "transcript_sha256",
    "write_if_changed",
]


SEARCH_TABLES = ("segments", "cleaned_segments")


@dataclass(frozen=True, slots=True)
class ChainShape:
    """The names a source kind stores its three steps under."""

    normalize: str = "normalize"
    protected_vocabulary: str = "protected_vocabulary"
    spell_fix: str = "spell_fix"
    # The key in `inputs` that names the step, and its text for each step.
    step_key: str = "step"
    protected_vocabulary_step: str = "corpuslm.cleanup.protect_vocabulary/1"
    spell_fix_step: str = "corpuslm.cleanup.spell_fix/1"
    # Whether the protected-vocabulary artifact's provenance lists where each term came from.
    vocabulary_sources_in_provenance: bool = False
    # The steps after spell fix: their node names, and the text that names the step in `inputs`.
    # The node names are also the counters a report keeps and the stage the ledger records.
    ai_cleanup: str = "model_cleanup"
    speaker_map: str = "speaker_map"
    subtitles: str = "subtitles"
    search_index: str = "search_index"
    # The table the search index rows go to (`SEARCH_TABLES`): `segments` when the transcript is
    # what the tool searches (ytlm), `cleaned_segments` when the tool's raw lines live in
    # `segments` and are never overwritten (beelm).
    search_table: str = "segments"
    ai_cleanup_step: str = "corpuslm.model_cleanup/1"
    speaker_map_step: str = "corpuslm.speaker_map/1"
    # The words the model prompt calls one source of this kind ("a Bee conversation").
    source_label: str = "a recording"

    def __post_init__(self) -> None:
        if self.search_table not in SEARCH_TABLES:
            raise ValueError(
                f"search_table must be one of {', '.join(SEARCH_TABLES)}, not {self.search_table!r}"
            )


@dataclass(frozen=True, slots=True)
class RawTranscript:
    """What a source kind hands the normalize step: the hashes of its inputs, and a builder
    that is called only when the artifact is not current (it returns the RAW transcript
    document and the artifact's provenance)."""

    inputs: dict
    build: Callable[[], tuple[dict, dict]]


class SourceKind(Protocol):
    """The origin-specific half of the chain."""

    shape: ChainShape

    def load_segments(self, source: str) -> RawTranscript:
        """The source's raw segments, as the normalize step's inputs and builder."""
        ...

    def vocabulary(self, source: str) -> basic_cleanup.Vocabulary:
        """The protected vocabulary of the source."""
        ...

    def language(self, source: str) -> str:
        """The language spell fix runs in; anything but English skips it."""
        ...

    def workspace_dir(self, source: str) -> Path:
        """The folder the source's transcript files go in."""
        ...

    def file_name(self, node: str) -> str:
        """The file name of one node's transcript document inside `workspace_dir`."""
        ...

    def refined_schema(self) -> dict | None:
        """The tool's refinement of the transcript schema, or None for the shared one."""
        ...

    def row(self, source: str) -> dict:
        """The source's database row (what a speaker scheme resolves default names from)."""
        ...

    def export_file_name(self, source: str, node: str) -> str:
        """The file name inside `workspace_dir` of a node that is not a transcript document:
        the speaker map, the published transcript (`transcript`), the subtitle file."""
        ...

    def validate(self, name: str, document: dict) -> None:
        """Raise `corpuslm.schemas.SchemaError` when a document the builders write (the
        `speaker_map`) does not match the tool's schema for it."""
        ...


class Report(Protocol):
    """What a run keeps while it builds. `count` adds to a counter named for a node;
    `errors` and `notes` are keyed by source or by a short name; `stopped_by` names the spend
    cap that stopped the run, empty if none did."""

    errors: dict[str, str]
    notes: dict[str, str]
    stopped_by: str

    def count(self, node: str, n: int = 1) -> None: ...


@dataclass(frozen=True, slots=True)
class Built:
    """A builder's result: the artifact's sha256, and whether this call built it."""

    sha256: str
    built: bool


@dataclass(frozen=True, slots=True)
class Chain:
    """A source kind over a store. Artifact paths are recorded relative to `root`."""

    store: Store
    kind: SourceKind
    root: Path
    created_by: str

    def path(self, source: str, node: str) -> Path:
        return self.kind.workspace_dir(source) / self.kind.file_name(node)

    def load(self, source: str, node: str) -> dict:
        text = self.path(source, node).read_text("utf-8")
        return transcript.loads(text, refined_schema=self.kind.refined_schema())

    def dumps(self, document: dict) -> bytes:
        text = transcript.dumps(document, refined_schema=self.kind.refined_schema())
        return text.encode("utf-8")


def write_if_changed(path: Path, data: bytes) -> str:
    """Write `data` only when the file's bytes differ; return its sha256. Staleness is bytes."""
    sha256 = _sha256_bytes(data)
    if not path.exists() or path.read_bytes() != data:
        files.write_atomic(path, data)
    return sha256


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_current(store: Store, source: str, node: str, inputs: dict) -> bool:
    """Whether the source's artifact for `node` is built from exactly these inputs."""
    row = store.artifact(source, node)
    return bool(row) and row["state"] == "built" and loads(row["inputs"], {}) == inputs


def record(
    store: Store,
    source: str,
    node: str,
    *,
    root: Path,
    path: Path | None,
    sha256: str,
    inputs: dict,
    provenance: dict,
    created_by: str,
    state: str = "built",
) -> None:
    """Record one artifact of `source`; the path is stored relative to `root`."""
    store.record_artifact(
        source,
        node,
        path=str(path.relative_to(root)) if path else "",
        sha256=sha256,
        state=state,
        inputs=inputs,
        provenance=provenance,
        created_by=created_by,
    )


def _current_sha256(chain: Chain, source: str, node: str, inputs: dict) -> str | None:
    """The recorded sha256 when the artifact is current and its file exists."""
    if is_current(chain.store, source, node, inputs) and chain.path(source, node).exists():
        row = chain.store.artifact(source, node)
        return row["sha256"] if row else None
    return None


def _finish(
    chain: Chain, source: str, node: str, inputs: dict, document: dict, provenance: dict
) -> Built:
    path = chain.path(source, node)
    sha256 = write_if_changed(path, chain.dumps(document))
    record(
        chain.store,
        source,
        node,
        root=chain.root,
        path=path,
        sha256=sha256,
        inputs=inputs,
        provenance=provenance,
        created_by=chain.created_by,
    )
    return Built(sha256, True)


def build_normalize(chain: Chain, source: str) -> Built:
    """The RAW transcript document of the source, from its raw segments."""
    shape = chain.kind.shape
    raw = chain.kind.load_segments(source)
    current = _current_sha256(chain, source, shape.normalize, raw.inputs)
    if current is not None:
        return Built(current, False)
    document, provenance = raw.build()
    return _finish(chain, source, shape.normalize, raw.inputs, document, provenance)


def build_protected_vocabulary(chain: Chain, source: str, normalize_sha256: str) -> Built:
    """Protected vocabulary (corpuslm.basic_cleanup.protect_vocabulary) over the normalize
    artifact."""
    shape = chain.kind.shape
    vocabulary = chain.kind.vocabulary(source)
    inputs = {
        shape.normalize: normalize_sha256,
        "vocabulary": canonical_sha256(vocabulary.fingerprint()),
        shape.step_key: shape.protected_vocabulary_step,
    }
    current = _current_sha256(chain, source, shape.protected_vocabulary, inputs)
    if current is not None:
        return Built(current, False)
    document, report = basic_cleanup.protect_vocabulary(
        chain.load(source, shape.normalize), vocabulary
    )
    provenance = (
        report | {"sources": vocabulary.sources}
        if (shape.vocabulary_sources_in_provenance)
        else report
    )
    return _finish(chain, source, shape.protected_vocabulary, inputs, document, provenance)


def build_spell_fix(chain: Chain, source: str, protected_vocabulary_sha256: str) -> Built:
    """Spell fix (corpuslm.basic_cleanup.spell_fix) over the protected-vocabulary artifact."""
    shape = chain.kind.shape
    language = chain.kind.language(source)
    inputs = {
        shape.protected_vocabulary: protected_vocabulary_sha256,
        "language": language,
        shape.step_key: shape.spell_fix_step,
    }
    current = _current_sha256(chain, source, shape.spell_fix, inputs)
    if current is not None:
        return Built(current, False)
    document, report = basic_cleanup.spell_fix(
        chain.load(source, shape.protected_vocabulary), language=language
    )
    return _finish(chain, source, shape.spell_fix, inputs, document, report)


# ---- the steps after the AI cleanup -----------------------------------------------------


def build_speaker_map(
    chain: Chain,
    source: str,
    ai_cleanup_sha256: str,
    *,
    scheme: SpeakerScheme,
    extra_inputs: dict | None = None,
    report: Report | None = None,
) -> dict:
    """The speaker map of the source (corpuslm.speaker_map.build_map) over the AI cleanup
    artifact (the transcript the model step wrote, whose label-to-name bindings it reads).

    `extra_inputs` are the hashes of whatever else the tool's map depends on (ytlm: the video's
    metadata). The owner's verdicts (`Store.current_speaker_names`) are an input: a rename
    rebuilds the map."""
    shape = chain.kind.shape
    owner_names = chain.store.current_speaker_names(source)
    inputs = {
        shape.ai_cleanup: ai_cleanup_sha256,
        **(extra_inputs or {}),
        "verdicts": canonical_sha256(owner_names),
        shape.step_key: shape.speaker_map_step,
    }
    path = chain.kind.workspace_dir(source) / chain.kind.export_file_name(source, shape.speaker_map)
    if is_current(chain.store, source, shape.speaker_map, inputs) and path.exists():
        existing = json.loads(path.read_text("utf-8"))
        chain.kind.validate("speaker_map", existing)
        return existing
    document = chain.load(source, shape.ai_cleanup)
    bindings = (document.get("passes", {}).get("pass3", {}) or {}).get("bindings", [])
    built = speaker_map_module.build_map(
        chain.kind.row(source),
        document,
        bindings,
        candidates=chain.kind.vocabulary(source).terms,
        scheme=scheme,
        owner_names=owner_names,
    )
    chain.kind.validate("speaker_map", built)
    sha256 = write_if_changed(
        path,
        (json.dumps(built, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    record(
        chain.store,
        source,
        shape.speaker_map,
        root=chain.root,
        path=path,
        sha256=sha256,
        inputs=inputs,
        provenance={
            "roles": {r: e["status"] for r, e in built["roles"].items()},
            "needs_author": speaker_map_module.review_items(built),
        },
        created_by=chain.created_by,
    )
    if report is not None:
        report.count(shape.speaker_map)
    return built


def best_transcript(
    chain: Chain,
    source: str,
    speaker_map: dict | None,
    *,
    preference: tuple[str, ...],
    scheme: SpeakerScheme,
) -> dict:
    """The best transcript available: the first node of `preference` whose artifact is built (or
    stale) and whose file exists, with the speaker map applied. Reads only; writes nothing."""
    for node in preference:
        row = chain.store.artifact(source, node)
        if row and row["state"] in ("built", "stale") and chain.path(source, node).exists():
            document = chain.load(source, node)
            if speaker_map:
                document = speaker_map_module.apply_map(document, speaker_map, scheme=scheme)
            return document
    raise schemas.SchemaError("no transcript artifact is built for this video")


def transcript_sha256(chain: Chain, document: dict) -> str:
    """The sha256 of the bytes a published transcript of `document` has, without writing it."""
    return _sha256_bytes(chain.dumps(document))


def publish_best(
    chain: Chain,
    source: str,
    speaker_map: dict | None,
    *,
    preference: tuple[str, ...],
    scheme: SpeakerScheme,
) -> tuple[dict, str]:
    """The best transcript available (`best_transcript`), written as the source's `transcript`
    file only when its bytes change; the sha of those bytes is what the subtitle file, the search
    index and a note depend on, so a rename re-fires exactly them."""
    document = best_transcript(chain, source, speaker_map, preference=preference, scheme=scheme)
    path = chain.kind.workspace_dir(source) / chain.kind.export_file_name(source, "transcript")
    return document, write_if_changed(path, chain.dumps(document))


def build_subtitles(
    chain: Chain,
    source: str,
    document: dict,
    transcript_sha256: str,
    *,
    max_line_chars: int,
    report: Report | None = None,
) -> bool:
    """The subtitle (SRT) file of the published transcript. True when this call built it."""
    shape = chain.kind.shape
    inputs = {"transcript": transcript_sha256, "max_line_chars": max_line_chars}
    path = chain.kind.workspace_dir(source) / chain.kind.export_file_name(source, shape.subtitles)
    if is_current(chain.store, source, shape.subtitles, inputs) and path.exists():
        return False
    text = transcript.to_srt(document, max_line_chars=max_line_chars)
    sha256 = write_if_changed(path, text.encode("utf-8"))
    record(
        chain.store,
        source,
        shape.subtitles,
        root=chain.root,
        path=path,
        sha256=sha256,
        inputs=inputs,
        provenance={"transcript_state": document["state"]},
        created_by=chain.created_by,
    )
    if report is not None:
        report.count(shape.subtitles)
    return True


def search_index_current(chain: Chain, source: str, document: dict, transcript_sha256: str) -> bool:
    """Whether the source's search index rows are built from this published transcript. Reads only.
    For the cleaned table the rows themselves are counted too, so an index whose rows are gone is
    rebuilt rather than trusted."""
    shape = chain.kind.shape
    if not is_current(chain.store, source, shape.search_index, {"transcript": transcript_sha256}):
        return False
    if shape.search_table == "cleaned_segments":
        return chain.store.cleaned_segment_count(source) == len(document["segments"])
    return True


def build_search_index(
    chain: Chain,
    source: str,
    document: dict,
    transcript_sha256: str,
    *,
    report: Report | None = None,
) -> bool:
    """The source's rows in the full-text index, rebuilt whole when the published transcript
    changed. True when this call built it.

    The rows go to the table the shape names (`ChainShape.search_table`): `segments` for a tool
    whose transcript is what it searches, `cleaned_segments` for a tool whose raw lines live in
    `segments` and are never overwritten (beelm)."""
    shape = chain.kind.shape
    inputs = {"transcript": transcript_sha256}
    cleaned = shape.search_table == "cleaned_segments"
    if search_index_current(chain, source, document, transcript_sha256):
        return False
    with chain.store.transaction():
        if cleaned:
            chain.store.replace_cleaned_segments(source, document["segments"])
        else:
            chain.store.replace_segments(source, document["segments"])
        record(
            chain.store,
            source,
            shape.search_index,
            root=chain.root,
            path=None,
            sha256=transcript_sha256,
            inputs=inputs,
            provenance={
                "segments": len(document["segments"]),
                "transcript_state": document["state"],
            },
            created_by=chain.created_by,
        )
    if report is not None:
        report.count(shape.search_index)
    return True
