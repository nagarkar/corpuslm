"""Graph: the pipeline nodes every source kind shares, as data.

A `Node` is one stage of the pipeline as a typed dependency graph: its scope, what it
depends on, and its kind. `CORE_NODES` are the stages from the transcript document onward
(normalize, the two basic cleanup steps, the AI cleanup, the speaker map, the
exports and the corpus stages). A tool puts its own fetching stages in front: ytlm's
`NODES` is its YouTube nodes followed by `CORE_NODES`. The propagation table (which change
dirties which dependent) stays with the tool that runs the graph.

The names are the stored artifact names of the first tool, so they do not change:
`pass1` is the protected-vocabulary step and `pass2` is the spell-fix step
(corpuslm.basic_cleanup). beelm records the same two steps under the plainer names
`protected_vocabulary` and `spell_fix` (corpuslm.chain).

Never imports: db, store, clikit, any model transport.

Decisions:
- The shared nodes are one tuple in one place, in build order, so two tools cannot drift on
  what the stages are. Enforced by:
  tests/test_graph.py::test_core_nodes_are_the_shared_stages_in_build_order.

Terms:
- **node**: one stage of the pipeline; an artifact of a source or corpus carries its name.
- **scope**: what the node is built for: `video` (one source), `corpus`, `owner` or
  `config`. `video` is the legacy name for one source of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Node:
    name: str
    scope: str  # video | corpus | owner | config
    deps: tuple[str, ...]
    kind: str  # deterministic | llm | owner | export | config
    mode: str  # push | pull
    build_step: int  # the build step that builds it


CORE_NODES: tuple[Node, ...] = (
    # per video
    Node("normalize", "video", ("captions",), "deterministic", "push", 1),
    Node(
        "pass1",
        "video",
        ("normalize", "metadata", "corpus_glossary_apply"),
        "deterministic",
        "push",
        2,
    ),
    Node("pass2", "video", ("pass1",), "deterministic", "push", 2),
    Node("pass3", "video", ("pass2", "review_verdicts"), "llm", "push", 2),
    Node(
        "speaker_map", "video", ("pass3", "metadata", "review_verdicts"), "deterministic", "push", 2
    ),
    Node("extraction", "video", ("transcript", "speaker_map"), "llm", "push", 4),
    Node("srt", "video", ("transcript",), "export", "push", 1),
    Node("fts", "video", ("transcript",), "export", "push", 1),
    Node(
        "vault_note",
        "video",
        ("transcript", "metadata", "speaker_map", "membership"),
        "export",
        "push",
        1,
    ),
    # per corpus
    Node("membership", "corpus", (), "deterministic", "push", 1),
    Node(
        "corpus_glossary_candidates",
        "corpus",
        ("membership", "metadata", "normalize"),
        "deterministic",
        "push",
        3,
    ),
    Node("corpus_glossary_normalize", "corpus", ("corpus_glossary_candidates",), "llm", "push", 3),
    Node("critic", "corpus", ("corpus_glossary_normalize",), "llm", "push", 3),
    Node(
        "corpus_glossary_apply",
        "corpus",
        ("critic", "corpus_glossary_ratifications"),
        "deterministic",
        "push",
        3,
    ),
    Node("corpus_note", "corpus", ("membership", "corpus_glossary_apply"), "export", "push", 1),
    Node(
        "renders", "corpus", ("extraction", "corpus_glossary_apply", "membership"), "llm", "pull", 4
    ),
    # owner inputs (upstream nodes)
    Node("review_verdicts", "owner", (), "owner", "push", 2),
    Node("corpus_glossary_ratifications", "owner", (), "owner", "push", 3),
    Node("config", "config", (), "config", "push", 1),
)
