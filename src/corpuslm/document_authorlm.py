"""The authorlm-backed document store (corpuslm.document). Imported only by
`document.store()`, so a consumer runs without authorlm installed until a document verb is used.

It calls the entry points authorlm built for outside callers (authorlm's
docs/outside-caller-design.md), and only those: `ENTRY_POINTS` below is the frozen list of the
contract (corpuslm.document). The manuscript lives in authorlm's own workspace database (the one
`AUTHORLM_WORKSPACE` in authorlm's `.env` names); its files live in the directory the consumer
hands over. The consumer's own runtime state here (the Docs write record) lives in the
consumer's workspace. Sign-in is never interactive here: a lapsed sign-in is reported with the
command that renews it.

`pull` goes through authorlm's library, never its `doc pull` verb: the verb opens an editorial
session, and closing one runs authorlm's learning pass over text a machine wrote (seen
2026-10-02). The collect after a pull passes `NO_EPISODE`, so the transitions attach to no goal.

Never imports: db, store, llm, clikit.
"""

from __future__ import annotations

from pathlib import Path

from .document import DocumentUnavailable
from .files import write_atomic

SIGN_IN = "authorlm doc auth"
# authorlm module → the names the real store may call. Frozen by the contract
# (corpuslm.document): tests/test_document_authorlm.py fails on any name used here
# that is not listed, and on any listed name no longer used.
ENTRY_POINTS: dict[str, frozenset[str]] = {
    "api": frozenset(
        {
            "NO_EPISODE",
            "caller_metadata",
            "collect",
            "get_manuscript",
            "open_db",
            "outstanding_revisions",
            "register_manuscript",
            "resolve_revisions",
            "resolve_workspace",
            "set_caller_metadata",
            "stage_revisions",
            "withdraw_revisions",
        }
    ),
    "docs": frozenset({"add_doc"}),
    "gdocs": frozenset(
        {
            "doc_status",
            "get_docs_service",
            "get_service",
            "pull_doc",
            "push_doc",
            "read_manuscript",
            "sync_tab_structure",
        }
    ),
    "paths": frozenset({"config_path", "load_env"}),
}

# Google allows 60 Docs writes a minute per user. Each call here is given an
# estimate of the writes it will make, and waits only as long as needed for
# the last minute's writes plus its own to fit under WRITE_BUDGET. Staging a
# tab spends about one write per revision; rebuilding a tab spends a few.
# (Found 2026-10-02: the third tab of five hit the limit.)
WRITE_ALLOWANCE = 50
WRITE_ALLOWANCE_PERIOD_SECONDS = 62
REBUILD_WRITES = 8
STAGE_OVERHEAD_WRITES = 8
WRITES_FILE = "document-writes.json"


class AuthorlmStore:
    def __init__(self, config: dict, workspace: Path, *, origin: str, now_seconds=None):
        from authorlm import api, docs, gdocs, paths  # pyright: ignore[reportMissingImports]

        from . import clock

        # seconds since the epoch; injectable so the pacing is tested offline
        self._time = now_seconds or clock.epoch_seconds

        self._api, self._docs, self._gdocs = api, docs, gdocs
        paths.load_env()
        import tomllib

        cfg_path = paths.config_path()
        self._config = tomllib.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        try:
            self._db = api.open_db(None)
        except (SystemExit, Exception) as error:  # noqa: BLE001 - authorlm reports many ways
            raise DocumentUnavailable(
                f"authorlm's workspace database is not reachable: {error}"
            ) from error
        self._workspace = str(api.resolve_workspace(None)[0])
        self._drive = self._docsvc = None
        # Best-effort steps that were skipped, each named for the verb's
        # board (no silent fallback): tab order, the Doc's title, the
        # write record.
        self.warnings: list[str] = []
        self._state_dir = Path(workspace)
        self._origin = origin

    # The last minute's writes are kept in a file in the consumer's workspace,
    # since each verb is its own process and verbs run back to back share
    # the limit.
    def _stamp(self) -> Path:
        return self._state_dir / WRITES_FILE

    def _recent(self) -> list[list[float]]:
        import json

        try:
            rows = json.loads(self._stamp().read_text())
        except (OSError, ValueError):
            return []
        now = self._time()
        return [
            row
            for row in rows
            if isinstance(row, list) and now - row[0] < WRITE_ALLOWANCE_PERIOD_SECONDS
        ]

    def _pace(self, writes: int) -> None:
        """Wait until `writes` more fit in the minute's allowance."""
        import time

        writes = min(writes, WRITE_ALLOWANCE)
        while True:
            rows = self._recent()
            if sum(row[1] for row in rows) + writes <= WRITE_ALLOWANCE or not rows:
                return
            time.sleep(
                max(
                    1.0, min(row[0] for row in rows) + WRITE_ALLOWANCE_PERIOD_SECONDS - self._time()
                )
            )

    def _wrote(self, writes: int) -> None:
        import json

        try:
            write_atomic(self._stamp(), json.dumps(self._recent() + [[self._time(), writes]]))
        except OSError as error:
            self.warnings.append(
                f"the Docs write record {self._stamp()} could not be saved ({error}); the next "
                f"verb's pacing does not count these {writes} writes"
            )

    # -- plumbing --
    def _services(self):
        if self._drive is None:
            try:
                self._drive = self._gdocs.get_service(
                    self._config, self._workspace, interactive=False
                )
                self._docsvc = self._gdocs.get_docs_service(
                    self._config, self._workspace, interactive=False
                )
            except Exception as error:  # noqa: BLE001 - any credential failure reads the same
                raise DocumentUnavailable(
                    f"Google sign-in is missing or has lapsed ({error}). Run: {SIGN_IN}"
                ) from error
        return self._drive, self._docsvc

    def _find(self, name: str) -> dict | None:
        try:
            return self._api.get_manuscript(self._db, name)
        except LookupError:
            return None

    def _ms(self, name: str) -> dict:
        manuscript = self._find(name)
        if manuscript is None:
            raise DocumentUnavailable(f"authorlm has no manuscript named {name!r}")
        return manuscript

    def _url(self, manuscript: dict) -> str:
        master = self._gdocs.doc_status(self._db, manuscript).get("_master_id")
        return f"https://docs.google.com/document/d/{master}/edit" if master else ""

    def _toc(self, manuscript: dict) -> list[str]:
        import tomllib

        toc_path = Path(manuscript["path"]) / "toc.toml"
        if not toc_path.exists():
            return []
        return [
            chapter["file"]
            for chapter in tomllib.loads(toc_path.read_text("utf-8")).get("chapter", [])
        ]

    def _write_toc(self, manuscript: dict, files: list[str]) -> None:
        text = "".join(f'[[chapter]]\nfile = "{file}"\n\n' for file in files)
        write_atomic(Path(manuscript["path"]) / "toc.toml", text)

    def _sync_order(self, manuscript: dict) -> None:
        """Move the Doc's tabs to match toc.toml. authorlm does this from its
        own `doc push` verb, not from the library calls used here. It acts
        only when the owner has not moved tabs in the Doc since the last
        sync. Best effort: tab order never fails a verb, and a failed sync is
        a named warning; the next write tries again."""
        try:
            self._gdocs.sync_tab_structure(
                self._db, self._ms(manuscript["name"]), self._services()[1]
            )
        except Exception as error:  # noqa: BLE001 - any failure reads the same: order unsynced
            self.warnings.append(
                f"tab order not synced to the Doc ({str(error)[:200]}); toc.toml keeps the "
                "order and the next write tries again"
            )

    def _name_doc(self, manuscript: dict) -> None:
        """The master Doc is created under the manuscript's name; give it the
        title the owner should see. Best effort: a failure is a named
        warning, and the next write tries again (`named` stays unset)."""
        master = self._gdocs.doc_status(self._db, manuscript).get("_master_id")
        own = self._api.caller_metadata(self._db, manuscript, self._origin)
        title = own.get("title")
        if master and title and not own.get("named"):
            try:
                self._services()[0].files().update(fileId=master, body={"name": title}).execute()
            except Exception as error:  # noqa: BLE001 - a cosmetic step never fails the verb
                self.warnings.append(
                    f"the Doc was not renamed to {title!r} ({str(error)[:200]}); the next "
                    "write tries again"
                )
                return
            self._api.set_caller_metadata(self._db, manuscript, self._origin, {"named": True})

    # -- the store --
    def ensure(self, name: str, path: Path, title: str) -> dict:
        row = self._find(name)
        created = row is None
        if row is not None and Path(row["path"]).resolve() != Path(path).resolve():
            raise DocumentUnavailable(
                f"authorlm already has a manuscript named {name!r} at {row['path']}; "
                "a document cannot share its name"
            )
        if created:
            self._api.register_manuscript(self._db, name, str(path), extraction=False)
            self._api.set_caller_metadata(self._db, self._ms(name), self._origin, {"title": title})
        return {"created": created, "url": self._url(self._ms(name))}

    def read(self, name: str) -> dict:
        manuscript = self._ms(name)
        drive, docsvc = self._services()
        state = self._gdocs.read_manuscript(self._db, manuscript, drive, docsvc)
        root = Path(manuscript["path"])
        order = [file for file in self._toc(manuscript) if file in state["files"]]
        order += sorted(
            file for file in state["files"] if file not in order and file.endswith(".md")
        )
        out_still = self._api.outstanding_revisions(self._db, manuscript, self._origin)
        tabs = []
        for file in order:
            file_state = state["files"][file]
            local = (root / file).read_text("utf-8") if (root / file).exists() else ""
            # a tab never agreed with its file is empty in the Doc; the file is the text
            settled = (
                file_state["settled"]
                if file_state.get("pushed") and file_state["settled"] is not None
                else local
            )
            # Google's export bolds a heading: "# **Title**"
            heading = next(
                (
                    line[2:].strip().strip("*").strip()
                    for line in settled.splitlines()
                    if line.startswith("# ")
                ),
                file,
            )
            tabs.append(
                {
                    "file": file,
                    "title": heading,
                    "settled": settled,
                    "open": [
                        {
                            "old": form["old"] if form["kind"] == "replace" else "",
                            "new": form["new"],
                        }
                        for form in file_state.get("forms") or []
                    ],
                    "comments": [
                        comment["content"]
                        + (f' (on: "{comment["quote"]}")' if comment.get("quote") else "")
                        for comment in file_state.get("comments") or []
                    ],
                    # revisions staged and not yet landed by a resolve, whether or
                    # not their markers are still in the tab (the owner may have
                    # ruled by removing the braces by hand)
                    "pending": out_still.get(file, 0),
                    "state": file_state["state"],
                }
            )
        return {"url": state.get("url") or self._url(manuscript), "tabs": tabs}

    def add_tab(self, name: str, file: str, title: str, *, before: str | None = None) -> None:
        manuscript = self._ms(name)
        try:
            self._docs.add_doc(manuscript, file, title)
        except FileExistsError:
            pass
        files = self._toc(manuscript)
        if file in files:
            return  # a tab keeps the place it was given
        insert_index = files.index(before) if before in files else len(files)
        files.insert(insert_index, file)
        self._write_toc(manuscript, files)

    def write_tab(self, name: str, file: str, text: str) -> None:
        manuscript = self._ms(name)
        write_atomic(Path(manuscript["path"]) / file, text)
        drive, docsvc = self._services()
        try:
            self._gdocs.push_doc(self._db, manuscript, file, service=drive, docs_service=docsvc)
        except LookupError as error:
            raise DocumentUnavailable(f"{file} could not be pushed: {error}") from error
        self._name_doc(manuscript)

    def stage(self, name: str, file: str, revisions: list[dict]) -> dict:
        manuscript = self._ms(name)
        drive, docsvc = self._services()
        sent = []
        for revision in revisions:
            rev = {"old": revision["old"], "new": revision["new"], "note": revision.get("note", "")}
            if not revision["old"]:
                rev["anchor_paragraph"] = int(revision["after"])
            sent.append(rev)
        writes = len(sent) + STAGE_OVERHEAD_WRITES
        self._pace(writes)
        try:
            out = self._api.stage_revisions(
                self._db, manuscript, file, sent, self._origin, drive, docsvc, config=self._config
            )
        except Exception as error:  # noqa: BLE001 - Google's errors arrive as HttpError
            self._wrote(writes)
            if "RATE_LIMIT_EXCEEDED" in str(error) or "429" in str(error):
                raise DocumentUnavailable(
                    f"Google's write limit was reached while staging {file}; wait a minute "
                    "and run the verb again"
                ) from error
            raise DocumentUnavailable(f"staging {file} failed: {str(error)[:300]}") from error
        self._wrote(writes)
        self._name_doc(manuscript)
        self._sync_order(manuscript)
        return {
            "written": len(out["written"]),
            "failed": [
                {
                    "index": next(
                        sent_index
                        for sent_index, sent_revision in enumerate(sent)
                        if sent_revision is rev
                    ),
                    "revision": rev,
                    "reason": reason,
                }
                for rev, reason in out["failed"]
            ],
            "url": out.get("url") or self._url(self._ms(name)),
            "warnings": out.get("warnings", []),
        }

    def pull(self, name: str) -> dict:
        """Comments stay open: they are the owner's notes for the next update."""
        manuscript = self._ms(name)
        drive, docsvc = self._services()
        try:
            out = self._gdocs.pull_doc(
                self._db, manuscript, None, service=drive, with_comments=False, docs_service=docsvc
            )
            self._api.collect(self._db, self._ms(name), self._config, episode=self._api.NO_EPISODE)
        except Exception as error:  # noqa: BLE001
            raise DocumentUnavailable(f"the pull failed: {str(error)[:300]}") from error
        return {key: list(out.get(key) or []) for key in ("changed", "unchanged", "conflicts")}

    def withdraw(self, name: str, file: str) -> int:
        """authorlm closes this origin's own threads on the file as withdrawn
        (`api.withdraw_revisions`), and the tab is rebuilt from the local
        file, which still holds the settled text."""
        manuscript = self._ms(name)
        rows = self._api.withdraw_revisions(self._db, manuscript, file, self._origin)
        drive, docsvc = self._services()
        self._pace(REBUILD_WRITES)
        try:
            self._gdocs.push_doc(self._db, manuscript, file, service=drive, docs_service=docsvc)
        except Exception as error:  # noqa: BLE001
            raise DocumentUnavailable(f"{file} could not be rebuilt: {str(error)[:300]}") from error
        finally:
            self._wrote(REBUILD_WRITES)
        return len(rows)

    def resolve(self, name: str, file: str) -> dict:
        manuscript = self._ms(name)
        drive, docsvc = self._services()
        out = self._api.resolve_revisions(
            self._db, manuscript, file, self._origin, drive, docsvc, config=self._config
        )
        if not out["landed"] and out["state"] in ("conflict", "missing"):
            raise DocumentUnavailable(
                f"{file} could not be resolved: the tab is {out['state']}. "
                + " ".join(out.get("warnings", []))
            )
        return {
            "accepted": out["accepted"],
            "modified": out["modified"],
            "declined": out["declined"],
            "declined_revisions": [
                {"old": revision["old"], "new": revision["new"]}
                for revision in out["revisions"]
                if revision["verdict"] == "declined"
            ],
            "warnings": out.get("warnings", []),
        }
