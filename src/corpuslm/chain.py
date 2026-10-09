"""Chain: the generic clean-up builders, the same normalize -> protected vocabulary -> spell fix
chain for every source kind.

A tool describes its source with a `SourceKind` (where the raw segments come from, the
protected vocabulary, the language, where the files go) and builds a `Chain` over a `Store`.
`build_normalize`, `build_protected_vocabulary` and `build_spell_fix` then each do the same
thing: compute the hashes of the step's inputs, return the recorded artifact when those hashes
and the file are unchanged, otherwise build the document, write it only if its bytes differ and
record the artifact (`Store.record_artifact`). `canonical_sha256`, `write_if_changed`,
`is_current` and `record` are the bookkeeping those builders share, usable by a tool's own
builders too.

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

Terms:
- **source kind**: the origin-specific half of the chain: YouTube video or Bee conversation
  (`SourceKind`).
- **chain shape**: the names a source kind stores its chain under (`ChainShape`).
- **built**: a builder's result says whether this call built the artifact (`Built.built`) or
  found it current.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import cleanup, files, transcript
from .db import loads
from .identity import canonical_sha256
from .store import Store

__all__ = [
    "Built",
    "Chain",
    "ChainShape",
    "RawTranscript",
    "SourceKind",
    "build_normalize",
    "build_protected_vocabulary",
    "build_spell_fix",
    "canonical_sha256",
    "is_current",
    "record",
    "write_if_changed",
]


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

    def vocabulary(self, source: str) -> cleanup.Vocabulary:
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


@dataclass(frozen=True, slots=True)
class Built:
    """A builder's result: the artifact's sha256, and whether this call built it."""

    sha: str
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
    sha = _sha256_bytes(data)
    if not path.exists() or path.read_bytes() != data:
        files.write_atomic(path, data)
    return sha


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
    sha: str,
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
        sha256=sha,
        state=state,
        inputs=inputs,
        provenance=provenance,
        created_by=created_by,
    )


def _current_sha(chain: Chain, source: str, node: str, inputs: dict) -> str | None:
    """The recorded sha256 when the artifact is current and its file exists."""
    if is_current(chain.store, source, node, inputs) and chain.path(source, node).exists():
        row = chain.store.artifact(source, node)
        return row["sha256"] if row else None
    return None


def _finish(
    chain: Chain, source: str, node: str, inputs: dict, document: dict, provenance: dict
) -> Built:
    path = chain.path(source, node)
    sha = write_if_changed(path, chain.dumps(document))
    record(
        chain.store,
        source,
        node,
        root=chain.root,
        path=path,
        sha=sha,
        inputs=inputs,
        provenance=provenance,
        created_by=chain.created_by,
    )
    return Built(sha, True)


def build_normalize(chain: Chain, source: str) -> Built:
    """The RAW transcript document of the source, from its raw segments."""
    shape = chain.kind.shape
    raw = chain.kind.load_segments(source)
    current = _current_sha(chain, source, shape.normalize, raw.inputs)
    if current is not None:
        return Built(current, False)
    document, provenance = raw.build()
    return _finish(chain, source, shape.normalize, raw.inputs, document, provenance)


def build_protected_vocabulary(chain: Chain, source: str, normalize_sha: str) -> Built:
    """Protected vocabulary (corpuslm.cleanup.protect_vocabulary) over the normalize artifact."""
    shape = chain.kind.shape
    vocabulary = chain.kind.vocabulary(source)
    inputs = {
        shape.normalize: normalize_sha,
        "vocabulary": canonical_sha256(vocabulary.fingerprint()),
        shape.step_key: shape.protected_vocabulary_step,
    }
    current = _current_sha(chain, source, shape.protected_vocabulary, inputs)
    if current is not None:
        return Built(current, False)
    document, report = cleanup.protect_vocabulary(chain.load(source, shape.normalize), vocabulary)
    provenance = (
        report | {"sources": vocabulary.sources}
        if (shape.vocabulary_sources_in_provenance)
        else report
    )
    return _finish(chain, source, shape.protected_vocabulary, inputs, document, provenance)


def build_spell_fix(chain: Chain, source: str, protected_vocabulary_sha: str) -> Built:
    """Spell fix (corpuslm.cleanup.spell_fix) over the protected-vocabulary artifact."""
    shape = chain.kind.shape
    language = chain.kind.language(source)
    inputs = {
        shape.protected_vocabulary: protected_vocabulary_sha,
        "language": language,
        shape.step_key: shape.spell_fix_step,
    }
    current = _current_sha(chain, source, shape.spell_fix, inputs)
    if current is not None:
        return Built(current, False)
    document, report = cleanup.spell_fix(
        chain.load(source, shape.protected_vocabulary), language=language
    )
    return _finish(chain, source, shape.spell_fix, inputs, document, report)
