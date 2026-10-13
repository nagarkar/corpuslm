"""Document store: the one road from corpuslm's consumers to authorlm's Google Docs.

A document is a manuscript authorlm keeps: markdown files locally, one Google Doc with a tab per
file, revisions the owner rules on in the Doc, and margin comments. ytlm's corpus document and
the artifact layer (monologues, perspective sheets) are documents. authorlm owns the Google calls,
the files, the revisions, the comments and the history; this module is the only place a consumer
touches it, through the `DocumentStore` protocol: eight operations.

`store()` returns the real store (`corpuslm.document_authorlm`), or the file-backed fake when a
test set `FAKE_DIR` in-process. Document verbs are command-line only: a sign-in can open a
browser, so nothing here is reachable through a tool's MCP surface.

A revision is `{"old": str, "new": str, "after": int, "note": str}`: `old == ""` adds `new` as a
paragraph after paragraph `after` (1-based; 0 is the start of the tab); otherwise `new` replaces
the paragraph `old`.

Decisions (the authorlm contract, settled 2026-10-12, nagarkar/corpuslm#44):
- corpuslm owns the `DocumentStore` protocol and the fake. authorlm is its only implementation,
  reached only through the outside-caller entry points named in `corpuslm.document_authorlm`
  (its `ENTRY_POINTS`). No consumer imports any other authorlm module. Enforced by:
  tests/test_document_authorlm.py::test_the_real_store_touches_only_the_frozen_entry_points.
  (owner)
- A document registers with concept extraction off and collects with no episode by default.
  Authoring sessions, lens sweeps, critiques and intents are available on a document but run only
  when a verb asks, each behind `--dry-run` and the owner's yes. Nothing runs ambiently on a
  document. (owner)
- Comments flow into corpuslm as text and are answered through authorlm's comment workflow.
  Nothing a document says feeds authorlm's beliefs or concept graph. The only thing corpuslm
  writes into a body of work's own manuscript is its perspective sheet tab. (owner)
- `ENTRY_POINTS` is a frozen list. A change to it is a change to this contract and is raised with
  the owner first. (owner)
- The system of record for a document's content is the shared database; the Doc is the view the
  owner comments on. (owner)
- The fake is chosen in-process only (`FAKE_DIR`), never by an environment variable, so a switch
  left exported in a shell or a launchd plist changes nothing in a real run.

Never imports: db, store, llm, clikit.

Terms:
- **document**: a manuscript authorlm keeps for a consumer (a corpus document, a monologue, a
  perspective sheet), reached only through `DocumentStore`.
- **document store**: the `DocumentStore` protocol: ensure, read, add_tab, write_tab, stage, pull,
  withdraw, resolve. The real store is authorlm; the fake is one JSON file per manuscript.
- **tab**: one markdown file of a document, shown as one tab of its Google Doc.
- **revision**: authorlm's old/new form in a tab (old struck through, new in green) that the
  owner accepts, modifies or declines in the Doc; `resolve` lands the rulings.
- **settled text**: a tab's text with every ruled revision landed and none open.
- **origin**: the consumer's name on the revisions it staged (`ytlm`, `corpuslm`), so each
  consumer withdraws and resolves only its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .files import write_atomic
from .model import AdapterError


class DocumentUnavailable(AdapterError):
    """The store cannot be reached: authorlm is not installed, or the Google
    sign-in has lapsed. The message says what to run."""


class DocumentStore(Protocol):
    warnings: list[str]
    """Best-effort steps the store skipped, named for the verb's board."""

    def ensure(self, name: str, path: Path, title: str) -> dict:
        """Register the manuscript if new. → {"created": bool, "url": str}"""
        ...

    def read(self, name: str) -> dict:
        """The manuscript as it stands in the Doc. → {"url": str, "tabs":
        [{"file", "title", "settled", "open": [revision], "comments": [str]}]}
        in reading order, each also carrying `pending`: revisions staged
        and not yet landed by a resolve, which can exceed `len(open)` when
        the owner removed the markers by hand. Changes nothing locally."""
        ...

    def add_tab(self, name: str, file: str, title: str, *, before: str | None = None) -> None:
        """A new empty tab, placed before the tab `before` (end when None)."""
        ...

    def write_tab(self, name: str, file: str, text: str) -> None:
        """Replace a tab the code owns (about.md) outright; never a revision."""
        ...

    def stage(self, name: str, file: str, revisions: list[dict]) -> dict:
        """Put revisions in the tab. A tab with open revisions takes none.
        → {"written": int, "failed": [{"index", "revision", "reason"}],
        "url": str}; `index` is the revision's place in the list given."""
        ...

    def pull(self, name: str) -> dict:
        """Bring the Doc's settled text into the local files, for every tab.
        Open revisions stay in the Doc; margin comments are left open.
        → {"changed": [file], "unchanged": [file], "conflicts": [file]}"""
        ...

    def withdraw(self, name: str, file: str) -> int:
        """Take back every revision staged on the tab and not yet resolved:
        the tab returns to its settled text. → how many were taken back."""
        ...

    def resolve(self, name: str, file: str) -> dict:
        """Land the owner's rulings on one tab. → {"accepted": int,
        "modified": int, "declined": int, "declined_revisions": [revision]}"""
        ...


def paragraphs(text: str) -> list[str]:
    """Blocks separated by blank lines: the unit a revision addresses."""
    return [block.strip("\n") for block in text.split("\n\n") if block.strip()]


def apply_revision(text: str, rev: dict) -> str:
    """The settled text after one accepted revision (the fake's resolve, and
    the reference for what a revision means)."""
    blocks = paragraphs(text)
    if rev["old"]:
        if rev["old"] not in blocks:
            raise ValueError("the paragraph to replace is not in the tab")
        blocks[blocks.index(rev["old"])] = rev["new"]
    else:
        blocks.insert(min(int(rev.get("after", len(blocks))), len(blocks)), rev["new"])
    return "\n\n".join(block for block in blocks if block) + "\n"


class FakeDocumentStore:
    """One JSON file per manuscript under a directory. A test plays the
    owner by editing a tab's `open` list before `resolve`: a revision with
    `"verdict": "decline"` is declined, one whose `new` was changed and
    carries `"verdict": "modify"` lands as modified, the rest are accepted."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.warnings: list[str] = []

    def _path(self, name: str) -> Path:
        return self.directory / f"{name}.json"

    def _load(self, name: str) -> dict:
        manuscript_path = self._path(name)
        if not manuscript_path.exists():
            raise DocumentUnavailable(f"no manuscript named {name!r}")
        return json.loads(manuscript_path.read_text(encoding="utf-8"))

    def _save(self, name: str, manuscript: dict) -> None:
        write_atomic(self._path(name), json.dumps(manuscript, ensure_ascii=False, indent=1))

    def _tab(self, manuscript: dict, file: str) -> dict:
        for tab in manuscript["tabs"]:
            if tab["file"] == file:
                return tab
        raise KeyError(f"no tab {file!r}")

    def ensure(self, name: str, path: Path, title: str) -> dict:
        created = not self._path(name).exists()
        if created:
            self._save(name, {"title": title, "path": str(path), "tabs": []})
        return {"created": created, "url": f"fake://{name}"}

    def read(self, name: str) -> dict:
        manuscript = self._load(name)
        return {
            "url": f"fake://{name}",
            "tabs": [
                {
                    "file": tab["file"],
                    "title": tab["title"],
                    "settled": tab["settled"],
                    "open": [
                        {
                            key: revision[key]
                            for key in ("old", "new", "after", "note")
                            if key in revision
                        }
                        for revision in tab["open"]
                    ],
                    "pending": len(tab["open"]),
                    "comments": list(tab.get("comments", [])),
                }
                for tab in manuscript["tabs"]
            ],
        }

    def add_tab(self, name: str, file: str, title: str, *, before: str | None = None) -> None:
        manuscript = self._load(name)
        if any(existing_tab["file"] == file for existing_tab in manuscript["tabs"]):
            return
        tab = {"file": file, "title": title, "settled": f"# {title}\n", "open": []}
        insert_index = next(
            (
                tab_index
                for tab_index, existing_tab in enumerate(manuscript["tabs"])
                if existing_tab["file"] == before
            ),
            len(manuscript["tabs"]),
        )
        manuscript["tabs"].insert(insert_index, tab)
        self._save(name, manuscript)

    def write_tab(self, name: str, file: str, text: str) -> None:
        manuscript = self._load(name)
        self._tab(manuscript, file)["settled"] = text
        self._save(name, manuscript)

    def stage(self, name: str, file: str, revisions: list[dict]) -> dict:
        manuscript = self._load(name)
        tab = self._tab(manuscript, file)
        url = f"fake://{name}#{file}"
        if tab["open"]:
            return {
                "written": 0,
                "url": url,
                "failed": [
                    {
                        "index": revision_index,
                        "revision": revision,
                        "reason": "the tab has open revisions",
                    }
                    for revision_index, revision in enumerate(revisions)
                ],
            }
        blocks = paragraphs(tab["settled"])
        written, failed = 0, []
        for revision_index, revision in enumerate(revisions):
            if "\n" in revision["new"]:  # authorlm's rule: one paragraph, one line
                failed.append(
                    {
                        "index": revision_index,
                        "revision": revision,
                        "reason": "a revision is one line",
                    }
                )
                continue
            if "*" in revision["new"] and not revision["old"]:
                # authorlm takes back an addition whose inline markup the
                # Doc's export does not return verbatim
                failed.append(
                    {
                        "index": revision_index,
                        "revision": revision,
                        "reason": "export proof: inline markup",
                    }
                )
                continue
            if revision["old"] and revision["old"] not in blocks:
                failed.append(
                    {
                        "index": revision_index,
                        "revision": revision,
                        "reason": "the paragraph to replace is not in the tab",
                    }
                )
                continue
            tab["open"].append(dict(revision))
            written += 1
        self._save(name, manuscript)
        return {"written": written, "failed": failed, "url": url}

    def pull(self, name: str) -> dict:
        manuscript = self._load(name)  # the fake has no separate local copy
        return {
            "changed": [],
            "unchanged": [tab["file"] for tab in manuscript["tabs"]],
            "conflicts": [],
        }

    def withdraw(self, name: str, file: str) -> int:
        manuscript = self._load(name)
        tab = self._tab(manuscript, file)
        withdrawn_count, tab["open"] = len(tab["open"]), []
        self._save(name, manuscript)
        return withdrawn_count

    def resolve(self, name: str, file: str) -> dict:
        manuscript = self._load(name)
        tab = self._tab(manuscript, file)
        counts = {"accepted": 0, "modified": 0, "declined": 0}
        declined, added = [], []
        # additions are staged against the settled text as it was; apply in
        # order, moving each insertion point past what was added at or
        # before it
        for revision in tab["open"]:
            verdict = revision.get("verdict", "accept")
            if verdict == "decline":
                counts["declined"] += 1
                declined.append(
                    {
                        key: revision[key]
                        for key in ("old", "new", "after", "note")
                        if key in revision
                    }
                )
                continue
            rev = dict(revision)
            if not rev["old"]:
                insertion_point = int(rev.get("after", 0))
                rev["after"] = insertion_point + sum(
                    1 for added_position in added if added_position <= insertion_point
                )
                added.append(insertion_point)
            tab["settled"] = apply_revision(tab["settled"], rev)
            counts["modified" if verdict == "modify" else "accepted"] += 1
        tab["open"] = []
        self._save(name, manuscript)
        return counts | {"declined_revisions": declined}


# The fake store's directory. Set only by a test suite, in-process
# (monkeypatch); a real run leaves it None and reads no environment switch
# for it.
FAKE_DIR: Path | None = None


def store(
    config: dict | None = None, workspace: Path | None = None, *, origin: str = "corpuslm"
) -> DocumentStore:
    """`workspace` is the consumer's workspace directory, where the real
    store keeps its runtime state (the Docs write record). `origin` is the
    consumer's name on the revisions it stages."""
    if FAKE_DIR is not None:
        return FakeDocumentStore(FAKE_DIR)
    if workspace is None:
        raise DocumentUnavailable("the document store needs the consumer's workspace directory")
    try:
        from .document_authorlm import AuthorlmStore

        return AuthorlmStore(config or {}, workspace, origin=origin)
    except ImportError as error:
        # the authorlm import happens inside the store, so both the module and its
        # construction are covered
        raise DocumentUnavailable(
            "documents need authorlm installed from its checkout: "
            f"pip install -e ../authorllm/authorlm ({error})"
        ) from error
