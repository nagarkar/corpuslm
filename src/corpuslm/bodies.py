"""Bodies of work and perspective sheets: what a monologue draws on, and the prose that keeps it
in one voice.

A body of work is a reference a monologue draws on, so that monologues are enriched by it and,
across many monologues, read as one story. A STANDING body (a manuscript brought in as a text
source, or a corpus) carries a perspective sheet; an OCCASIONAL one (a single source attached to
one monologue) does not. `bodies_of_work` is the history of what was used: every monologue
records the bodies it drew on, and a new monologue looks there first
(nagarkar/corpuslm#49, milestone "Artifacts: monologues").

The perspective sheet is one to two pages of prose, stances, moves and tone, drafted by one model
pass (`draft`) over the body's claims and concepts, staged as revisions into a tab named
`Perspective sheet` in the body's own manuscript through the document store
(corpuslm.document), and edited and resolved by the owner there. The tab's vocabulary section is
code-owned: `refresh_vocabulary` rewrites it from the body's concepts in force and leaves the
owner's prose alone. The hash of the resolved tab text is the sheet version
(`bodies_of_work.sheet_sha256`), which every recipe run records; `status` reads the tab and
records a new version when the text changed. The whole tab is wrapped in `[Omit: all]`, so
authorlm's own passes leave it alone and the text source import leaves it out.

Never imports: cli, clikit. Reads the database only through `Store`; reaches the Doc only
through `corpuslm.document`.

Decisions:
- A body of work is standing (with a sheet) or occasional (without); both may be attached to one
  monologue; the table is the history of what was used, not a registry to maintain. (owner)
- The perspective sheet is prose the owner edits once, a tab in the body's own manuscript,
  stored as markdown locally in authorlm as every tab is. (owner)
- The only thing corpuslm writes into a body of work's manuscript is its sheet tab (the
  contract, corpuslm.document). Enforced by:
  tests/test_bodies.py::test_the_draft_touches_only_the_sheet_tab.
- The sheet never defines concepts in prose; the vocabulary section is generated from the
  concepts in force and rewritten without touching the owner's prose. Enforced by:
  tests/test_bodies.py::test_refreshing_the_vocabulary_leaves_the_owners_prose_alone.

Terms:
- **body of work**: a reference a monologue draws on, a row of `bodies_of_work`: standing (a
  text source or a corpus, with a sheet) or occasional (one source, without).
- **perspective sheet**: the standing body's prose, stances, moves and tone plus a generated
  vocabulary, in the tab `Perspective sheet` of its manuscript; its resolved text's hash is the
  sheet version.
- **sheet version**: `bodies_of_work.sheet_sha256`, the hash of the resolved sheet text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import concepts as concepts_module
from . import document, llm, prompts
from .store import Store

STAGE = "sheet"
SHEET_FILE = "perspective-sheet.md"
SHEET_TITLE = "Perspective sheet"
OMIT_OPEN, OMIT_CLOSE = "[Omit: all]", "[/Omit]"
SECTIONS = ("Stances", "Moves", "Tone")
VOCABULARY = "Vocabulary"
SAMPLE_CLAIMS = 80
SAMPLE_CONCEPTS = 60
ASSUMED_OUTPUT_TOKENS = 1500


def prompt() -> prompts.Prompt:
    return prompts.load("sheet")


def settings(config: dict) -> dict:
    section = config.get("sheet") or {}
    if not section.get("model"):
        raise llm.NotConfigured("[sheet].model is missing from the settings file")
    return {"model": str(section["model"])}


# ---- the bodies -------------------------------------------------------------------------


def add(
    store: Store,
    name: str,
    *,
    text_source_id: str | None = None,
    corpus_id: str | None = None,
    source_id: str | None = None,
    note: str = "",
    manuscript: str = "",
    directory: str = "",
    created_by: str,
) -> dict:
    """A standing body (a text source or a corpus) or an occasional one (a source)."""
    if sum(value is not None for value in (text_source_id, corpus_id, source_id)) != 1:
        raise ValueError("a body of work is exactly one of a text source, a corpus or a source")
    if store.body(name):
        raise ValueError(f"a body of work named {name!r} exists")
    if text_source_id is not None and not store.has_source(text_source_id):
        raise KeyError(f"unknown source {text_source_id!r}")
    if source_id is not None and not store.has_source(source_id):
        raise KeyError(f"unknown source {source_id!r}")
    if corpus_id is not None and store.corpus(corpus_id) is None:
        raise KeyError(f"unknown corpus {corpus_id!r}")
    kind = "occasional" if source_id is not None else "standing"
    if kind == "standing" and bool(manuscript) != bool(directory):
        raise ValueError("a sheet needs both --manuscript and --directory, or neither")
    made = store.add_body(
        name,
        kind,
        text_source_id=text_source_id,
        corpus_id=corpus_id,
        source_id=source_id,
        note=note,
        manuscript=manuscript,
        directory=directory,
        created_by=created_by,
    )
    return {"id": made, "name": name, "kind": kind}


def _body(store: Store, name: str) -> dict:
    row = store.body(name)
    if row is None:
        raise KeyError(f"no body of work named {name!r}")
    return row


def source_ids_of(store: Store, body: dict) -> list[str]:
    """The sources a body's claims come from."""
    if body["text_source_id"]:
        return [body["text_source_id"]]
    if body["source_id"]:
        return [body["source_id"]]
    return store.member_source_ids(body["corpus_id"], include_ignored=False)


# ---- the sheet tab ------------------------------------------------------------------------


def skeleton() -> str:
    body = "\n\n".join(f"## {section}" for section in (*SECTIONS, VOCABULARY))
    return f"# {SHEET_TITLE}\n\n{OMIT_OPEN}\n\n{body}\n\n{OMIT_CLOSE}\n"


def vocabulary_lines(store: Store, config: dict) -> list[str]:
    """One line per concept in force: `- label: definition` (the definition when there is one)."""
    try:
        states = concepts_module.states(store, config)
    except llm.NotConfigured:
        return []
    lines = []
    for concept in store.concepts():
        if states.get(concept["key"]) != "in":
            continue
        definition = concept["definition"].strip()
        lines.append(f"- {concept['label']}" + (f": {definition}" if definition else ""))
    return lines


def with_vocabulary(settled: str, lines: list[str]) -> str:
    """The sheet text with its vocabulary section replaced by `lines`; the rest untouched."""
    blocks = document.paragraphs(settled)
    try:
        start = blocks.index(f"## {VOCABULARY}")
    except ValueError:
        start = len(blocks)
        blocks.append(f"## {VOCABULARY}")
    end = next(
        (
            index
            for index in range(start + 1, len(blocks))
            if blocks[index] == OMIT_CLOSE or blocks[index].startswith("## ")
        ),
        len(blocks),
    )
    body = ["\n".join(lines)] if lines else []
    return "\n\n".join(blocks[: start + 1] + body + blocks[end:]) + "\n"


def _sample_claims(store: Store, body: dict) -> list[str]:
    rows = [claim for source_id in source_ids_of(store, body) for claim in store.claims(source_id)]
    if not rows:
        return []
    step = max(1, len(rows) // SAMPLE_CLAIMS)
    return [row["text"] for row in rows[::step][:SAMPLE_CLAIMS]]


def _variables(store: Store, body: dict, config: dict) -> dict:
    labels = [line[2:].split(":")[0] for line in vocabulary_lines(store, config)[:SAMPLE_CONCEPTS]]
    return {
        "body": body["name"],
        "note": body["note"] or "(none)",
        "concepts": "\n".join(f"- {label}" for label in labels) or "(none yet)",
        "claims": "\n".join(f"- {text}" for text in _sample_claims(store, body)) or "(none yet)",
    }


@dataclass(frozen=True, slots=True)
class Estimate:
    claims: int
    concepts: int
    tokens_in: int
    tokens_out: int
    usd: float | None
    model: str
    cached: bool


def estimate(store: Store, client: llm.Client, name: str) -> Estimate:
    body = _body(store, name)
    found = settings(client.config)
    variables = _variables(store, body, client.config)
    cached = client.lookup(STAGE, prompt(), variables) is not None
    tokens_in = 0 if cached else max(1, len(prompt().render(variables)) // 4 + 1)
    tokens_out = 0 if cached else ASSUMED_OUTPUT_TOKENS
    return Estimate(
        len(_sample_claims(store, body)),
        len(vocabulary_lines(store, client.config)),
        tokens_in,
        tokens_out,
        llm.price_usd(found["model"], tokens_in, tokens_out),
        found["model"],
        cached,
    )


def _ensure_tab(doc: document.DocumentStore, body: dict) -> dict:
    """The manuscript registered and the sheet tab present; the tab as it stands."""
    if not body["manuscript"] or not body["directory"]:
        raise ValueError(f"{body['name']} has no manuscript for a sheet; add it with --manuscript")
    doc.ensure(body["manuscript"], Path(body["directory"]), body["name"])
    tabs = {tab["file"]: tab for tab in doc.read(body["manuscript"])["tabs"]}
    if SHEET_FILE not in tabs:
        doc.add_tab(body["manuscript"], SHEET_FILE, SHEET_TITLE)
        doc.write_tab(body["manuscript"], SHEET_FILE, skeleton())
        tabs = {tab["file"]: tab for tab in doc.read(body["manuscript"])["tabs"]}
    return tabs[SHEET_FILE]


def draft(
    store: Store, client: llm.Client, doc: document.DocumentStore, name: str, *, created_by: str
) -> dict:
    """Draft the sheet's prose and stage it as revisions into the tab, section by section, after
    each section's heading; the vocabulary is written from the concepts in force. A tab with
    open revisions takes none: resolve them first. Returns counts and the Doc's URL."""
    body = _body(store, name)
    tab = _ensure_tab(doc, body)
    if tab["open"]:
        raise ValueError(
            f"the sheet of {name!r} has {len(tab['open'])} open revisions; resolve them first"
        )
    reply = client.complete(
        STAGE, prompt(), _variables(store, body, client.config), tag=f"sheet-{body['id']}"
    )
    settled = with_vocabulary(tab["settled"], vocabulary_lines(store, client.config))
    doc.write_tab(body["manuscript"], SHEET_FILE, settled)
    blocks = document.paragraphs(settled)
    revisions = []
    for section, texts in (
        ("Stances", reply.parsed["stances"]),
        ("Moves", reply.parsed["moves"]),
        ("Tone", [reply.parsed["tone"]]),
    ):
        # every paragraph of a section anchors at its heading, in order: an addition's anchor
        # names the settled text as it stands, and the resolve moves later anchors past the
        # additions landed at or before them (corpuslm.document)
        heading = blocks.index(f"## {section}") + 1  # 1-based position of the heading
        for text in texts:
            revisions.append(
                {
                    "old": "",
                    "new": " ".join(text.split()),
                    "after": heading,
                    "note": f"sheet draft: {section.lower()}",
                }
            )
    staged = doc.stage(body["manuscript"], SHEET_FILE, revisions)
    return {
        "body": name,
        "manuscript": body["manuscript"],
        "tab": SHEET_FILE,
        "staged": staged["written"],
        "failed": len(staged["failed"]),
        "vocabulary": len(vocabulary_lines(store, client.config)),
        "url": staged.get("url", ""),
        "cached": reply.cached,
        "cost_usd": round(reply.cost_usd or 0.0, 6),
    }


def refresh_vocabulary(store: Store, doc: document.DocumentStore, name: str, config: dict) -> dict:
    """Rewrite the vocabulary section from the concepts in force; the owner's prose stays. A tab
    with open revisions is left alone."""
    body = _body(store, name)
    tab = _ensure_tab(doc, body)
    if tab["open"]:
        raise ValueError(
            f"the sheet of {name!r} has {len(tab['open'])} open revisions; resolve them first"
        )
    lines = vocabulary_lines(store, config)
    text = with_vocabulary(tab["settled"], lines)
    changed = text != tab["settled"]
    if changed:
        doc.write_tab(body["manuscript"], SHEET_FILE, text)
    return {"body": name, "vocabulary": len(lines), "changed": changed}


def sheet_sha256(settled: str) -> str:
    return hashlib.sha256(settled.encode("utf-8")).hexdigest()


def status(store: Store, doc: document.DocumentStore, name: str) -> dict:
    """The sheet as it stands: its version hash (recorded on the body when it changed), the
    open revisions, and whether it has any prose yet."""
    body = _body(store, name)
    if not body["manuscript"]:
        return {"body": name, "sheet": "none", "version": "", "open_revisions": 0}
    try:
        tabs = {tab["file"]: tab for tab in doc.read(body["manuscript"])["tabs"]}
    except document.DocumentUnavailable:
        return {"body": name, "sheet": "none", "version": "", "open_revisions": 0}
    tab = tabs.get(SHEET_FILE)
    if tab is None:
        return {"body": name, "sheet": "none", "version": "", "open_revisions": 0}
    version = sheet_sha256(tab["settled"])
    changed = version != body["sheet_sha256"]
    if changed:
        store.update_body(name, {"sheet_sha256": version})
    prose = [
        block
        for block in document.paragraphs(tab["settled"])
        if not block.startswith(("#", "- ", OMIT_OPEN, OMIT_CLOSE))
    ]
    return {
        "body": name,
        "sheet": "drafted" if prose else "empty",
        "version": version,
        "changed": changed,
        "open_revisions": len(tab["open"]),
        "paragraphs": len(prose),
    }


def listing(store: Store) -> list[dict]:
    return [
        {
            "name": row["name"],
            "kind": row["kind"],
            "is": row["text_source_id"] or row["corpus_id"] or row["source_id"],
            "sheet": "yes" if row["sheet_sha256"] else ("tab" if row["manuscript"] else "no"),
            "last_used_at": row["last_used_at"],
            "note": row["note"],
        }
        for row in store.bodies()
    ]


__all__ = [
    "Estimate",
    "add",
    "draft",
    "estimate",
    "listing",
    "refresh_vocabulary",
    "settings",
    "sheet_sha256",
    "skeleton",
    "source_ids_of",
    "status",
    "vocabulary_lines",
    "with_vocabulary",
]
