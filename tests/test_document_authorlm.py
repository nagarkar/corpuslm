"""The authorlm-backed store (corpuslm.document_authorlm) reaches authorlm only through
the frozen entry points of the contract (corpuslm.document; metalm: never write into
another system's state). A stand-in `authorlm` package is installed in sys.modules, and
its database object fails any SQL sent to it, so a read or write of authorlm's tables
from here fails the test."""

from __future__ import annotations

import inspect
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from corpuslm import document as adapter
from corpuslm import document_authorlm


class SqlFromConsumer(AssertionError):
    pass


class GuardedDb:
    """authorlm's database handle as the store sees it: opaque. Any query or
    write from the store is a boundary breach."""

    def __getattr__(self, name):
        raise SqlFromConsumer(f"the store called authorlm's db.{name} directly")


class FakeAuthorlm:
    """The library calls the store may make, with a record of what happened."""

    def __init__(self, root: Path):
        self.root = root
        self.manuscripts: dict[str, dict] = {}
        self.callers: dict[tuple[str, str], dict] = {}
        self.outstanding: dict[str, int] = {}
        self.withdrawn: list[tuple[str, str]] = []
        self.pushed: list[str] = []
        self.renamed: list[str] = []
        self.collected: list[object] = []
        self.pulled: list[str] = []
        self.resolved: list[tuple[str, str]] = []

    def install(self, monkeypatch) -> None:
        package: Any = types.ModuleType("authorlm")
        api: Any = types.ModuleType("authorlm.api")
        docs: Any = types.ModuleType("authorlm.docs")
        gdocs: Any = types.ModuleType("authorlm.gdocs")
        paths: Any = types.ModuleType("authorlm.paths")
        fake = self

        api.NO_EPISODE = object()
        api.open_db = lambda workspace: GuardedDb()
        api.resolve_workspace = lambda workspace: (fake.root / "authorlm-ws", "")

        def get_manuscript(db, name=None):
            if name not in fake.manuscripts:
                raise LookupError(f"no manuscript named '{name}'")
            return dict(fake.manuscripts[name])

        def register_manuscript(db, name, path, extraction=True):
            assert extraction is False
            fake.manuscripts[name] = {"id": f"ms-{name}", "name": name, "path": str(path)}
            return dict(fake.manuscripts[name])

        def caller_metadata(db, manuscript, origin):
            return dict(fake.callers.get((manuscript["name"], origin), {}))

        def set_caller_metadata(db, manuscript, origin, values):
            own = fake.callers.setdefault((manuscript["name"], origin), {})
            own.update(values)
            return dict(own)

        def outstanding_revisions(db, manuscript, origin):
            assert origin == "ytlm"
            return dict(fake.outstanding)

        def withdraw_revisions(db, manuscript, file, origin):
            assert origin == "ytlm"
            count = fake.outstanding.pop(file, 0)
            fake.withdrawn.append((manuscript["name"], file))
            return [
                {"id": f"dt{revision_index}", "old": "", "new": "x", "note": ""}
                for revision_index in range(count)
            ]

        def stage_revisions(db, manuscript, file, revisions, origin, drive, docsvc, config=None):
            return {"written": list(revisions), "failed": [], "url": "u", "warnings": []}

        def collect(db, manuscript, config, episode=None):
            fake.collected.append(episode)
            return {"unchanged": True}

        def resolve_revisions(db, manuscript, file, origin, drive, docsvc, config=None):
            fake.resolved.append((manuscript["name"], file))
            return {
                "landed": True,
                "state": "resolved",
                "accepted": 1,
                "modified": 0,
                "declined": 1,
                "revisions": [
                    {"old": "", "new": "Kept.", "verdict": "accepted"},
                    {"old": "", "new": "Dropped.", "verdict": "declined"},
                ],
                "warnings": [],
            }

        def pull_doc(db, manuscript, query, service=None, with_comments=True, docs_service=None):
            assert with_comments is False
            fake.pulled.append(manuscript["name"])
            return {"changed": ["a.md"], "unchanged": [], "conflicts": []}

        api.get_manuscript = get_manuscript
        api.collect = collect
        api.resolve_revisions = resolve_revisions
        api.register_manuscript = register_manuscript
        api.caller_metadata = caller_metadata
        api.set_caller_metadata = set_caller_metadata
        api.outstanding_revisions = outstanding_revisions
        api.withdraw_revisions = withdraw_revisions
        api.stage_revisions = stage_revisions

        class Files:
            def update(self, fileId, body):
                fake.renamed.append(body["name"])
                return types.SimpleNamespace(execute=lambda: None)

        drive = types.SimpleNamespace(files=lambda: Files())
        gdocs.get_service = lambda config, workspace, interactive: drive
        gdocs.get_docs_service = lambda config, workspace, interactive: object()
        gdocs.doc_status = lambda db, manuscript: {"_master_id": "MASTER"}
        gdocs.sync_tab_structure = lambda db, manuscript, docsvc: None

        def push_doc(db, manuscript, file, service=None, docs_service=None):
            fake.pushed.append(file)

        def read_manuscript(db, manuscript, drive, docsvc):
            return {
                "url": "u",
                "files": {
                    "a.md": {
                        "settled": "# A\n",
                        "pushed": True,
                        "forms": [],
                        "comments": [],
                        "state": "unchanged",
                    }
                },
            }

        gdocs.push_doc = push_doc
        gdocs.pull_doc = pull_doc
        gdocs.read_manuscript = read_manuscript
        docs.add_doc = lambda manuscript, file, title: None
        paths.load_env = lambda: None
        paths.config_path = lambda: fake.root / "no-config.toml"

        package.api, package.docs, package.gdocs, package.paths = api, docs, gdocs, paths
        for name, mod in {
            "authorlm": package,
            "authorlm.api": api,
            "authorlm.docs": docs,
            "authorlm.gdocs": gdocs,
            "authorlm.paths": paths,
        }.items():
            monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture
def real(tmp_path, monkeypatch):
    monkeypatch.setattr("corpuslm.document.FAKE_DIR", None)
    fake = FakeAuthorlm(tmp_path)
    fake.install(monkeypatch)
    workspace = tmp_path / "ytlm-ws"
    workspace.mkdir()
    store = adapter.store({}, workspace, origin="ytlm")
    return fake, store, workspace


def test_ensure_registers_and_keeps_its_title_through_authorlm(real, tmp_path):
    fake, store, _ = real
    book = tmp_path / "book"
    book.mkdir()
    out = store.ensure("dharma", book, "Dharma talks")
    assert out == {"created": True, "url": "https://docs.google.com/document/d/MASTER/edit"}
    assert fake.callers[("dharma", "ytlm")] == {"title": "Dharma talks"}
    again = store.ensure("dharma", book, "Dharma talks")
    assert again["created"] is False


def test_ensure_declines_a_name_authorlm_holds_elsewhere(real, tmp_path):
    fake, store, _ = real
    fake.manuscripts["dharma"] = {"id": "x", "name": "dharma", "path": str(tmp_path / "other")}
    (tmp_path / "book").mkdir()
    with pytest.raises(adapter.DocumentUnavailable, match="cannot share its name"):
        store.ensure("dharma", tmp_path / "book", "T")


def test_write_tab_names_the_doc_once_through_caller_settings(real, tmp_path):
    fake, store, _ = real
    book = tmp_path / "book"
    book.mkdir()
    store.ensure("dharma", book, "Dharma talks")
    store.write_tab("dharma", "about.md", "# About\n")
    store.write_tab("dharma", "about.md", "# About\n")
    assert fake.renamed == ["Dharma talks"]
    assert fake.callers[("dharma", "ytlm")]["named"] is True


def test_read_counts_pending_through_authorlm(real, tmp_path):
    fake, store, _ = real
    book = tmp_path / "book"
    book.mkdir()
    store.ensure("dharma", book, "T")
    fake.outstanding = {"a.md": 3}
    tabs = store.read("dharma")["tabs"]
    assert [(tab["file"], tab["pending"]) for tab in tabs] == [("a.md", 3)]


def test_withdraw_goes_through_authorlm_then_rebuilds_the_tab(real, tmp_path):
    fake, store, _ = real
    book = tmp_path / "book"
    book.mkdir()
    store.ensure("dharma", book, "T")
    fake.outstanding = {"a.md": 2}
    assert store.withdraw("dharma", "a.md") == 2
    assert fake.withdrawn == [("dharma", "a.md")]
    assert fake.pushed == ["a.md"]


def test_pull_collects_with_no_episode_so_no_learning_pass_runs(real, tmp_path):
    """The contract (corpuslm.document): a document collects with no
    episode by default."""
    fake, store, _ = real
    _book(store, tmp_path)
    out = store.pull("dharma")
    assert out == {"changed": ["a.md"], "unchanged": [], "conflicts": []}
    assert fake.pulled == ["dharma"]
    assert fake.collected == [sys.modules["authorlm.api"].NO_EPISODE]


def test_resolve_lands_through_authorlm_and_returns_the_declined(real, tmp_path):
    fake, store, _ = real
    _book(store, tmp_path)
    out = store.resolve("dharma", "a.md")
    assert (out["accepted"], out["modified"], out["declined"]) == (1, 0, 1)
    assert out["declined_revisions"] == [{"old": "", "new": "Dropped."}]
    assert fake.resolved == [("dharma", "a.md")]


def test_write_pace_record_lives_in_the_workspace(real, tmp_path, monkeypatch):
    fake, store, workspace = real
    system_temporary_directory = tmp_path / "system-tmp"
    system_temporary_directory.mkdir()
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(system_temporary_directory))
    book = tmp_path / "book"
    book.mkdir()
    store.ensure("dharma", book, "T")
    store.stage("dharma", "a.md", [{"old": "", "new": "x", "after": 1}])
    assert (workspace / "document-writes.json").exists()
    assert list(system_temporary_directory.iterdir()) == []


def test_the_real_store_needs_the_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr("corpuslm.document.FAKE_DIR", None)
    FakeAuthorlm(tmp_path).install(monkeypatch)
    with pytest.raises(adapter.DocumentUnavailable, match="workspace"):
        adapter.store({})


# ---- best-effort steps say when they fail (metalm coding.md: no silent
# fallback; the ytlm.adapters rules)


def _book(store, tmp_path):
    book = tmp_path / "book"
    book.mkdir()
    store.ensure("dharma", book, "Dharma talks")


def test_a_tab_order_sync_that_fails_is_a_named_warning(real, tmp_path, monkeypatch):
    fake, store, _ = real
    _book(store, tmp_path)

    def broken(db, manuscript, docsvc):
        raise RuntimeError("tabs API down")

    monkeypatch.setattr(sys.modules["authorlm.gdocs"], "sync_tab_structure", broken)
    out = store.stage("dharma", "a.md", [{"old": "", "new": "x", "after": 1}])
    assert out["written"] == 1
    assert len(store.warnings) == 1
    assert "tab order" in store.warnings[0] and "tabs API down" in store.warnings[0]


def test_a_doc_rename_that_fails_is_a_named_warning_and_is_tried_again(real, tmp_path):
    fake, store, _ = real
    _book(store, tmp_path)

    class NoPermission:
        def update(self, fileId, body):
            raise RuntimeError("403 insufficient permissions")

    store._drive = types.SimpleNamespace(files=lambda: NoPermission())
    store._docsvc = object()
    store.write_tab("dharma", "about.md", "# About\n")
    assert len(store.warnings) == 1
    assert "Dharma talks" in store.warnings[0] and "403" in store.warnings[0]
    assert fake.callers[("dharma", "ytlm")].get("named") is not True


def test_a_write_record_that_cannot_be_saved_is_a_named_warning(real, tmp_path, monkeypatch):
    fake, store, workspace = real
    _book(store, tmp_path)
    real_write = document_authorlm.write_atomic

    def no_record(path, text, *args, **kwargs):
        if Path(path).name == "document-writes.json":
            raise OSError(28, "No space left on device")
        return real_write(path, text, *args, **kwargs)

    monkeypatch.setattr(document_authorlm, "write_atomic", no_record)
    store.stage("dharma", "a.md", [{"old": "", "new": "x", "after": 1}])
    assert any(
        "document-writes.json" in warning and "No space left" in warning
        for warning in store.warnings
    ), store.warnings


# ---- the contract's frozen list (corpuslm.document, "ENTRY_POINTS is a frozen list")

# a module name after `self._` or standing alone; never inside a URL or a path
ATTRIBUTE = re.compile(r"(?<![\w./])(?:self\._)?(api|docs|gdocs|paths)\.(\w+)")


def _names_used() -> dict[str, set[str]]:
    source = inspect.getsource(document_authorlm)
    used: dict[str, set[str]] = {"api": set(), "docs": set(), "gdocs": set(), "paths": set()}
    for match in ATTRIBUTE.finditer(source):
        used[match.group(1)].add(match.group(2))
    return used


def test_the_real_store_touches_only_the_frozen_entry_points():
    """Every authorlm name the store calls is in ENTRY_POINTS, and every
    listed name is called: a new call or a dropped one is a contract change
    the owner rules on first."""
    used = _names_used()
    frozen = {module: set(names) for module, names in document_authorlm.ENTRY_POINTS.items()}
    assert used == frozen


def test_the_stand_in_authorlm_offers_exactly_the_frozen_entry_points(tmp_path, monkeypatch):
    """The stand-in in this file is the contract made executable: it offers
    the frozen names and nothing else, so the store cannot pass these tests
    while calling anything outside the list."""
    FakeAuthorlm(tmp_path).install(monkeypatch)
    for module, names in document_authorlm.ENTRY_POINTS.items():
        offered = {
            name for name in vars(sys.modules[f"authorlm.{module}"]) if not name.startswith("__")
        }
        assert offered == set(names), module
