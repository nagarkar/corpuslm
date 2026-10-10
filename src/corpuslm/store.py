"""The Repository (architecture.md, "OOAD patterns"): the one module that
reads and writes the workspace database. Every other module asks it by
name (`source`, `record_artifact`, `append_review_verdict` ...) and never
writes SQL; `db.py` holds the connection, the schema and the migrations.
The tables are described in docs/design/storage-schema.md.

Rows come back as plain dicts. A source or corpus carries its lists
(`tags`, `topics`, `people`, `feeds`; `topic`, `perspective`) as Python
lists, read from their one-row-per-element tables. Decisions are
append-only: the `*_verdict` and `correction` writers add a row, and the
readers return the newest row per slot."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from .db import Database, ko_fields, loads, now_iso

# A master row's lists: list name → (table, parent column, element columns).
SOURCE_LISTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tags": ("source_tags", ("tag",)),
    "topics": ("source_topics", ("topic",)),
    "people": ("source_people", ("name", "role")),
    "feeds": ("source_feeds", ("feed_id",)),
}
CORPUS_LISTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "topic": ("corpus_topics", ("topic",)),
    "perspective": ("corpus_perspectives", ("perspective",)),
}
_LIST_PREFIX = {
    "source_tags": "vtg",
    "source_topics": "vtp",
    "source_people": "vpp",
    "source_feeds": "sfd",
    "corpus_topics": "ctp",
    "corpus_perspectives": "cps",
}


def _of_kind(kind: str | None) -> tuple[str, list[str]]:
    """WHERE clause (corpora aliased `c`) keeping the corpora a tool of source kind `kind`
    reads: those with no members yet and those with at least one member of that kind. A corpus
    made only of another kind's sources (beelm's, for ytlm) is left out."""
    if kind is None:
        return "", []
    return (
        "WHERE NOT EXISTS (SELECT 1 FROM corpus_members m WHERE m.corpus_id = c.id) "
        "OR EXISTS (SELECT 1 FROM corpus_members m JOIN sources v ON v.id = m.source_id "
        "WHERE m.corpus_id = c.id AND v.kind = ?) ",
        [kind],
    )


class Store:
    def __init__(self, db: Database):
        self.db = db

    def _row(self, sql: str, args: tuple | list = ()):
        """The one row an aggregate query always returns."""
        row = self.db.one(sql, args)
        if row is None:
            raise RuntimeError(f"no row from: {sql}")
        return row

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.db.transaction():
            yield

    # ---- lists of a master row -------------------------------------------

    def _read_list(self, table: str, parent: str, parent_id: str, columns: tuple[str, ...]):
        rows = self.db.all(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {parent} = ? ORDER BY position",
            (parent_id,),
        )
        if len(columns) == 1:
            return [r[columns[0]] for r in rows]
        return [{c: r[c] for c in columns} for r in rows]

    def _write_list(
        self,
        table: str,
        parent: str,
        parent_id: str,
        columns: tuple[str, ...],
        values: Sequence,
        created_by: str,
    ) -> None:
        """Write a list position by position: an element that stays where it
        was is left alone, so the change log records only the elements
        really removed or added (an unchanged list writes nothing)."""
        new = list(values)
        old = self._read_list(table, parent, parent_id, columns)
        if old == new:
            return

        def element(value) -> dict:
            if len(columns) == 1:
                return {columns[0]: value}
            return {c: value.get(c, "") for c in columns}

        with self.db.transaction():
            for position in range(max(len(old), len(new))):
                before = old[position] if position < len(old) else None
                after = new[position] if position < len(new) else None
                if position < len(old) and position < len(new) and before == after:
                    continue
                if position < len(old):
                    self.db.execute(
                        f"DELETE FROM {table} WHERE {parent} = ? AND position = ?",
                        (parent_id, position),
                    )
                if position < len(new):
                    self.db.insert(
                        table,
                        ko_fields(_LIST_PREFIX[table], created_by=created_by)
                        | {parent: parent_id, "position": position}
                        | element(after),
                    )

    def _with_lists(self, row, lists, parent: str) -> dict:
        out = dict(row)
        for name, (table, columns) in lists.items():
            out[name] = self._read_list(table, parent, out["id"], columns)
        return out

    def _split(self, changes: dict, lists) -> tuple[dict, dict]:
        scalar = {k: v for k, v in changes.items() if k not in lists}
        listed = {k: v for k, v in changes.items() if k in lists}
        return scalar, listed

    # ---- sources -----------------------------------------------------------

    def source(self, source_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM sources WHERE id = ?", (source_id,))
        return None if row is None else self._with_lists(row, SOURCE_LISTS, "source_id")

    def _source_row(self, source_id: str) -> dict:
        row = self.source(source_id)
        if row is None:
            raise KeyError(f"unknown source {source_id!r}")
        return row

    def _corpus_row(self, corpus_id: str) -> dict:
        corpus = self.corpus(corpus_id)
        if corpus is None:
            raise KeyError(f"unknown corpus {corpus_id!r}")
        return corpus

    def has_source(self, source_id: str) -> bool:
        return self.db.one("SELECT 1 FROM sources WHERE id = ?", (source_id,)) is not None

    def source_ids(self, kind: str | None = None) -> list[str]:
        """Every source id, oldest first; only those of `kind` (`youtube`, `bee`) when given."""
        where, args = ("WHERE kind = ? ", [kind]) if kind else ("", [])
        return [
            r["id"]
            for r in self.db.all(f"SELECT id FROM sources {where}ORDER BY created_at, rowid", args)
        ]

    def add_source(
        self, source_id: str, locator: str, *, created_by: str, kind: str = "youtube"
    ) -> bool:
        """False when the source is already in the catalog."""
        if self.has_source(source_id):
            return False
        self.db.insert(
            "sources",
            ko_fields(id=source_id, created_by=created_by) | {"kind": kind, "locator": locator},
        )
        return True

    def update_source(self, source_id: str, changes: dict[str, Any], *, created_by: str = "cli"):
        """Scalar columns update in place (logged by trigger); `tags`,
        `topics` and `people` replace their lists."""
        scalar, listed = self._split(changes, SOURCE_LISTS)
        with self.db.transaction():
            if scalar:
                self.db.update("sources", source_id, scalar)
            for name, values in listed.items():
                table, columns = SOURCE_LISTS[name]
                self._write_list(table, "source_id", source_id, columns, values, created_by)

    def source_counts_by(self, column: str, kind: str | None = None) -> dict[str, int]:
        if column not in ("status", "fetcher"):
            raise ValueError(f"no count by {column!r}")
        where, args = ("WHERE kind = ? ", [kind]) if kind else ("", [])
        return {
            r["k"]: r["n"]
            for r in self.db.all(
                f"SELECT {column} AS k, COUNT(*) AS n FROM sources {where}GROUP BY 1", args
            )
        }

    # ---- corpora and membership -----------------------------------------------

    def corpus_by_name(self, name: str, *, exact: bool = False) -> dict | None:
        """Case and outer spaces do not matter unless `exact`."""
        sql = "SELECT * FROM corpora WHERE name = ?" + ("" if exact else " COLLATE NOCASE")
        row = self.db.one(sql, (name if exact else name.strip(),))
        return None if row is None else self._with_lists(row, CORPUS_LISTS, "corpus_id")

    def corpus(self, corpus_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM corpora WHERE id = ?", (corpus_id,))
        return None if row is None else self._with_lists(row, CORPUS_LISTS, "corpus_id")

    def corpus_ids(self, kind: str | None = None) -> list[str]:
        """Every corpus id, oldest first; with `kind`, only the corpora a tool of that source
        kind reads: those with no members yet and those with a member of that kind."""
        where, args = _of_kind(kind)
        sql = f"SELECT id FROM corpora c {where}ORDER BY rowid"
        return [r["id"] for r in self.db.all(sql, args)]

    def corpus_names(self, kind: str | None = None) -> list[str]:
        where, args = _of_kind(kind)
        sql = f"SELECT name FROM corpora c {where}ORDER BY name"
        return [r["name"] for r in self.db.all(sql, args)]

    def corpus_count(self, kind: str | None = None) -> int:
        """How many corpora; with `kind`, only those a tool of that source kind reads."""
        where, args = _of_kind(kind)
        return self._row(f"SELECT COUNT(*) AS n FROM corpora c {where}".strip(), args)["n"]

    def add_corpus(self, fields: dict[str, Any], *, created_by: str) -> str:
        scalar, listed = self._split(fields, CORPUS_LISTS)
        with self.db.transaction():
            cid = self.db.insert("corpora", ko_fields("cor", created_by=created_by) | scalar)
            for name, values in listed.items():
                table, columns = CORPUS_LISTS[name]
                self._write_list(table, "corpus_id", cid, columns, values, created_by)
        return cid

    def update_corpus(self, corpus_id: str, changes: dict[str, Any], *, created_by: str = "cli"):
        scalar, listed = self._split(changes, CORPUS_LISTS)
        with self.db.transaction():
            if scalar:
                self.db.update("corpora", corpus_id, scalar)
            for name, values in listed.items():
                table, columns = CORPUS_LISTS[name]
                self._write_list(table, "corpus_id", corpus_id, columns, values, created_by)

    def ingested_member_rows(self, corpus_id: str) -> list[dict]:
        """The corpus document's members: ingested source rows (without their
        lists), by publication date then id."""
        return [
            dict(r)
            for r in self.db.all(
                "SELECT v.* FROM sources v JOIN corpus_members m ON m.source_id = v.id "
                "WHERE m.corpus_id = ? AND v.status = 'ingested' ORDER BY v.published_at, v.id",
                (corpus_id,),
            )
        ]

    def set_corpus_metadata(self, corpus_id: str, metadata: dict) -> None:
        """Replace a corpus's metadata object (logged by the change-log trigger)."""
        self.db.update("corpora", corpus_id, {"metadata": json.dumps(metadata, sort_keys=True)})

    def is_member(self, corpus_id: str, source_id: str) -> bool:
        return (
            self.db.one(
                "SELECT 1 FROM corpus_members WHERE corpus_id = ? AND source_id = ?",
                (corpus_id, source_id),
            )
            is not None
        )

    def add_member(self, corpus_id: str, source_id: str, reason: str, *, created_by: str) -> str:
        return self.db.insert(
            "corpus_members",
            ko_fields("mem", created_by=created_by)
            | {"corpus_id": corpus_id, "source_id": source_id, "reason": reason},
        )

    def member_source_ids(self, corpus_id: str) -> list[str]:
        return [
            r["source_id"]
            for r in self.db.all(
                "SELECT source_id FROM corpus_members WHERE corpus_id = ? "
                "ORDER BY created_at, rowid",
                (corpus_id,),
            )
        ]

    def member_count(self, corpus_id: str) -> int:
        return self._row(
            "SELECT COUNT(*) AS n FROM corpus_members WHERE corpus_id = ?", (corpus_id,)
        )["n"]

    def member_status_counts(self, corpus_id: str) -> dict[str, int]:
        """Membership status is derived from the source's status
        (vw_corpus_members): queued, ingested or failed."""
        return {
            r["member_status"]: r["n"]
            for r in self.db.all(
                "SELECT member_status, COUNT(*) AS n FROM vw_corpus_members "
                "WHERE corpus_id = ? GROUP BY member_status",
                (corpus_id,),
            )
        }

    def corpus_summaries(self, kind: str | None = None) -> list[dict]:
        where, args = _of_kind(kind)
        rows = self.db.all(
            "SELECT c.name, c.id, c.person, c.subject, COUNT(m.id) AS members, "
            "SUM(CASE WHEN m.member_status = 'ingested' THEN 1 ELSE 0 END) AS ingested "
            "FROM corpora c LEFT JOIN vw_corpus_members m ON m.corpus_id = c.id "
            f"{where}GROUP BY c.id ORDER BY c.name",
            args,
        )
        return [dict(r) for r in rows]

    def member_sources(self, corpus_id: str, *, ingested_only: bool = False) -> list[dict]:
        """Member source rows (with their lists) plus `member_status` and
        `member_reason`, in publication order."""
        rows = self.db.all(
            "SELECT v.id, m.member_status, m.reason AS member_reason "
            "FROM vw_corpus_members m JOIN sources v ON v.id = m.source_id "
            "WHERE m.corpus_id = ? "
            + ("AND v.status = 'ingested' " if ingested_only else "")
            + "ORDER BY v.published_at, v.title, v.id",
            (corpus_id,),
        )
        out = []
        for r in rows:
            row = self._source_row(r["id"])
            row["member_status"] = r["member_status"]
            row["member_reason"] = r["member_reason"]
            out.append(row)
        return out

    def corpora_of_source(self, source_id: str) -> list[dict]:
        rows = self.db.all(
            "SELECT c.id FROM corpora c JOIN corpus_members m ON m.corpus_id = c.id "
            "WHERE m.source_id = ? ORDER BY c.name",
            (source_id,),
        )
        return [self._corpus_row(r["id"]) for r in rows]

    def merge_corpora(self, into_id: str, from_ids: Sequence[str], *, created_by: str) -> dict:
        """Fold corpora into `into_id` in one transaction. Each membership
        moves to the target, marked `merged_from` in its metadata (a video
        already there keeps the target's row; the folded one is dropped).
        Each folded corpus, whole (row, lists, members), is appended to the
        target's `metadata.merged_from`, which the change log records; then
        its corpus-level artifacts (derived), lists and row are deleted."""
        if into_id in from_ids:
            raise ValueError("a corpus is not merged into itself")
        moved = already = 0
        removed = []
        with self.db.transaction():
            target = self._corpus_row(into_id)
            record = loads(target["metadata"], {})
            kept = list(record.get("merged_from") or [])
            for cid in from_ids:
                corpus = self._corpus_row(cid)
                members = [
                    dict(r)
                    for r in self.db.all(
                        "SELECT id, source_id, reason, created_at, created_by, metadata "
                        "FROM corpus_members WHERE corpus_id = ? ORDER BY created_at, rowid",
                        (cid,),
                    )
                ]
                snapshot = dict(corpus) | {
                    "metadata": loads(corpus["metadata"], {}),
                    "members": [m | {"metadata": loads(m["metadata"], {})} for m in members],
                    "merged_at": now_iso(),
                    "merged_by": created_by,
                }
                # The answers not yet landed are keyed by card, which hashes the corpus id: they
                # cannot follow a membership, so the folded corpus carries them in its snapshot.
                snapshot["review_answers"] = [
                    {k: a[k] for k in ("card_id", "verdict", "answered_at", "created_at")}
                    for a in self.review_answers(cid)
                ]
                kept.append(snapshot)
                for m in members:
                    if self.is_member(into_id, m["source_id"]):
                        self.db.execute("DELETE FROM corpus_members WHERE id = ?", (m["id"],))
                        already += 1
                        continue
                    meta = loads(m["metadata"], {}) | {"merged_from": cid}
                    self.db.update(
                        "corpus_members",
                        m["id"],
                        {"corpus_id": into_id, "metadata": json.dumps(meta, sort_keys=True)},
                    )
                    moved += 1
                self.db.execute("DELETE FROM artifacts WHERE corpus_id = ?", (cid,))
                self.db.execute("DELETE FROM review_answers WHERE corpus_id = ?", (cid,))
                for table, _ in CORPUS_LISTS.values():
                    self.db.execute(f"DELETE FROM {table} WHERE corpus_id = ?", (cid,))
                self.db.execute("DELETE FROM corpora WHERE id = ?", (cid,))
                removed.append(corpus["name"])
            self.set_corpus_metadata(into_id, record | {"merged_from": kept})
        return {"moved": moved, "already_members": already, "removed": removed}

    # ---- fetch attempts (evidence, append-only) ---------------------------------

    def append_fetch_attempt(
        self, source_id: str, fetcher: str, outcome: str, detail: str, *, created_by: str
    ) -> str:
        return self.db.insert(
            "fetch_attempts",
            ko_fields("fet", created_by=created_by)
            | {"source_id": source_id, "fetcher": fetcher, "outcome": outcome, "detail": detail},
        )

    def fetch_attempt_counts(self) -> list[dict]:
        """How many attempts each fetcher made per UTC day and outcome, oldest
        day first. Counts only; the detail text never leaves the table."""
        return [
            dict(r)
            for r in self.db.all(
                "SELECT substr(created_at, 1, 10) AS day, fetcher, outcome, COUNT(*) AS n, "
                "MAX(substr(detail, 1, 120)) AS last_detail FROM fetch_attempts "
                "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
            )
        ]

    # ---- artifact index (derived state) ------------------------------------------

    def _owner_column(self, owner: str) -> str:
        if self.has_source(owner):
            return "source_id"
        if self.db.one("SELECT 1 FROM corpora WHERE id = ?", (owner,)):
            return "corpus_id"
        raise KeyError(f"artifact owner {owner!r} is neither a source nor a corpus")

    def artifact(self, owner: str, node: str) -> dict | None:
        row = self.db.one("SELECT * FROM artifacts WHERE owner = ? AND node = ?", (owner, node))
        return None if row is None else dict(row)

    def record_artifact(
        self,
        owner: str,
        node: str,
        *,
        path: str,
        sha256: str,
        state: str,
        inputs: dict,
        provenance: dict,
        created_by: str,
    ) -> None:
        changes = {
            "path": path,
            "sha256": sha256,
            "state": state,
            "inputs": json.dumps(inputs, sort_keys=True),
            "provenance": json.dumps(provenance, sort_keys=True),
        }
        row = self.artifact(owner, node)
        if row:
            self.db.update("artifacts", row["id"], changes)
        else:
            self.db.insert(
                "artifacts",
                ko_fields("art", created_by=created_by)
                | {self._owner_column(owner): owner, "node": node}
                | changes,
            )

    def mark_stale(self, owner: str, node: str) -> bool:
        row = self.artifact(owner, node)
        if not row:
            return False
        self.db.update("artifacts", row["id"], {"state": "stale"})
        return True

    def artifacts_of(self, owner: str) -> list[dict]:
        """Every artifact row of one source or corpus, by node name."""
        return [
            dict(r)
            for r in self.db.all("SELECT * FROM artifacts WHERE owner = ? ORDER BY node", (owner,))
        ]

    def delete_artifacts(self, owner: str, nodes: Sequence[str]) -> int:
        """Remove the artifact rows of `nodes` for one owner (derived state, no
        history); the runner then finds them missing and builds them. Returns
        the rows removed."""
        if not nodes:
            return 0
        marks = ", ".join("?" * len(nodes))
        cur = self.db.execute(
            f"DELETE FROM artifacts WHERE owner = ? AND node IN ({marks})", (owner, *nodes)
        )
        return cur.rowcount

    def artifact_health(self) -> dict:
        """Counts for `status`: built per node, stale, pass 3 drops, and
        provisional transcripts."""
        built = {
            r["node"]: r["n"]
            for r in self.db.all(
                "SELECT node, COUNT(*) AS n FROM artifacts WHERE state = 'built' GROUP BY node"
            )
        }
        stale = self._row("SELECT COUNT(*) AS n FROM artifacts WHERE state = 'stale'")["n"]
        drops = self._row(
            "SELECT COALESCE(SUM(json_extract(provenance, '$.dropped_ambiguities')), 0) AS a, "
            "COALESCE(SUM(json_extract(provenance, '$.dropped_bindings')), 0) AS b, "
            "COALESCE(SUM(json_extract(provenance, '$.ambiguities')), 0) AS n "
            "FROM artifacts WHERE node = 'pass3' AND state IN ('built', 'stale')"
        )
        provisional = self._row(
            "SELECT COUNT(*) AS n FROM artifacts WHERE node = 'captions' AND state = 'built' "
            "AND json_extract(provenance, '$.provisional') = 1"
        )["n"]
        return {
            "built": built,
            "stale": stale,
            "dropped_ambiguities": int(drops["a"]),
            "dropped_bindings": int(drops["b"]),
            "ambiguities": int(drops["n"]),
            "provisional": provisional,
        }

    # ---- segments and search (derived index) ---------------------------------------

    def replace_segments(self, source_id: str, segments: Iterable[dict]) -> None:
        """The FTS rows of one source, rebuilt whole. Transcript segments keep
        their JSON names (idx, t0, t1); the columns are position,
        start_seconds and end_seconds."""
        with self.db.transaction():
            self.db.execute("DELETE FROM segments WHERE source_id = ?", (source_id,))
            for seg in segments:
                self.db.execute(
                    "INSERT INTO segments(source_id, position, start_seconds, end_seconds, "
                    "speaker, text) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        source_id,
                        seg["idx"],
                        seg["t0"],
                        seg["t1"],
                        seg.get("speaker", ""),
                        seg["text"],
                    ),
                )

    def segments(self, source_id: str) -> list[dict]:
        """One source's segments in order, with the JSON names `replace_segments`
        takes (idx, t0, t1, speaker, text)."""
        return [
            {
                "idx": r["position"],
                "t0": r["start_seconds"],
                "t1": r["end_seconds"],
                "speaker": r["speaker"],
                "text": r["text"],
            }
            for r in self.db.execute(
                "SELECT position, start_seconds, end_seconds, speaker, text FROM segments "
                "WHERE source_id = ? ORDER BY position",
                (source_id,),
            ).fetchall()
        ]

    def delete_segments(self, source_id: str) -> int:
        """Remove one source's segments; the segments_ad trigger removes their
        FTS5 rows. Returns the segments removed."""
        return self.db.execute("DELETE FROM segments WHERE source_id = ?", (source_id,)).rowcount

    def segment_count(self, source_id: str | None = None, kind: str | None = None) -> int:
        """Segments of one source, of every source of `kind`, or of all."""
        if source_id is None and kind:
            return self._row(
                "SELECT COUNT(*) AS n FROM segments s JOIN sources o ON o.id = s.source_id "
                "WHERE o.kind = ?",
                (kind,),
            )["n"]
        if source_id is None:
            return self._row("SELECT COUNT(*) AS n FROM segments")["n"]
        return self._row("SELECT COUNT(*) AS n FROM segments WHERE source_id = ?", (source_id,))[
            "n"
        ]

    def search(
        self, match: str, *, corpus_id: str | None, limit: int, kind: str | None = None
    ) -> list[dict]:
        """FTS5 hits, best first, through vw_search_segments; only sources of `kind` when given.
        Raises sqlite3.OperationalError when `match` is not valid FTS5 syntax."""
        sql = (
            "SELECT h.source_id, h.position, h.start_seconds, h.end_seconds, h.title, "
            "snippet(segments_fts, 0, '[', ']', '…', 14) AS snippet, "
            "bm25(segments_fts) AS rank "
            "FROM segments_fts JOIN vw_search_segments h ON h.segment_rowid = segments_fts.rowid "
            "WHERE segments_fts MATCH ? "
            + (
                "AND EXISTS (SELECT 1 FROM corpus_members m "
                "WHERE m.source_id = h.source_id AND m.corpus_id = ?) "
                if corpus_id
                else ""
            )
            + (
                "AND EXISTS (SELECT 1 FROM sources o WHERE o.id = h.source_id AND o.kind = ?) "
                if kind
                else ""
            )
            + "ORDER BY rank LIMIT ?"
        )
        args = [match] + ([corpus_id] if corpus_id else []) + ([kind] if kind else []) + [limit]
        return [dict(r) for r in self.db.all(sql, args)]

    # ---- locks (coordination) ---------------------------------------------------

    def lock_holder(self, owner: str) -> dict | None:
        row = self.db.one("SELECT holder, expires_at FROM locks WHERE owner = ?", (owner,))
        return None if row is None else dict(row)

    def take_lock(self, owner: str, holder: str, *, acquired_at: str, expires_at: str) -> None:
        self.db.execute(
            "INSERT INTO locks(owner, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner) DO UPDATE SET holder = excluded.holder, "
            "acquired_at = excluded.acquired_at, expires_at = excluded.expires_at",
            (owner, holder, acquired_at, expires_at),
        )

    def refresh_locks(self, holder: str, *, expires_at: str) -> int:
        """The heartbeat: push out the expiry of every lock `holder` holds."""
        cur = self.db.execute(
            "UPDATE locks SET expires_at = ? WHERE holder = ?", (expires_at, holder)
        )
        return cur.rowcount

    def release_lock(self, owner: str, holder: str) -> None:
        self.db.execute("DELETE FROM locks WHERE owner = ? AND holder = ?", (owner, holder))

    # ---- review verdicts, speaker verdicts, correction memory (decisions) -------------

    def append_review_verdict(
        self,
        *,
        source_id: str,
        key: str,
        kind: str,
        verdict: str,
        span: str,
        role: str,
        source: str,
        subject_sha256: str,
        metadata: dict | None = None,
        created_by: str,
    ) -> str:
        """A new verdict row; the newest row for (source_id, key) is in force.
        `previous_subject_sha256` is filled when the slot's current verdict
        judged different content (lineage: new hash → old hash)."""
        current = self.db.one(
            "SELECT subject_sha256 FROM vw_current_review_verdicts WHERE source_id = ? AND key = ?",
            (source_id, key),
        )
        previous = (
            current["subject_sha256"]
            if current and current["subject_sha256"] != subject_sha256
            else None
        )
        row = ko_fields("ver", created_by=created_by) | {
            "source_id": source_id,
            "key": key,
            "kind": kind,
            "verdict": verdict,
            "span": span,
            "role": role,
            "source": source,
            "applied_at": now_iso(),
            "subject_sha256": subject_sha256,
            "previous_subject_sha256": previous,
        }
        if metadata is not None:
            row["metadata"] = json.dumps(metadata, sort_keys=True)
        return self.db.insert("review_verdicts", row)

    def current_review_verdicts(self, source_id: str) -> dict[str, dict]:
        """key → the verdict in force (the newest row for that key)."""
        return {
            r["key"]: dict(r)
            for r in self.db.all(
                "SELECT * FROM vw_current_review_verdicts WHERE source_id = ? ORDER BY key",
                (source_id,),
            )
        }

    def review_verdict_history(self, source_id: str, key: str) -> list[dict]:
        """Every verdict ever given for one slot, oldest first."""
        return [
            dict(r)
            for r in self.db.all(
                "SELECT * FROM review_verdicts WHERE source_id = ? AND key = ? ORDER BY rowid",
                (source_id, key),
            )
        ]

    def append_speaker_verdict(
        self, *, source_id: str, role: str, name: str, created_by: str
    ) -> str:
        return self.db.insert(
            "speaker_verdicts",
            ko_fields("spk", created_by=created_by)
            | {"source_id": source_id, "role": role, "name": name},
        )

    def current_speaker_names(self, source_id: str) -> dict[str, str]:
        """role → the author's name in force for that role."""
        return {
            r["role"]: r["name"]
            for r in self.db.all(
                "SELECT role, name FROM vw_current_speaker_verdicts WHERE source_id = ? "
                "ORDER BY role",
                (source_id,),
            )
        }

    def append_correction(
        self,
        *,
        span_norm: str,
        verdict: str,
        kind: str,
        origin_source: str,
        origin_key: str,
        created_by: str,
    ) -> str:
        return self.db.insert(
            "corrections",
            ko_fields("crr", created_by=created_by)
            | {
                "span_norm": span_norm,
                "verdict": verdict,
                "kind": kind,
                "origin_source": origin_source,
                "origin_key": origin_key,
            },
        )

    def current_corrections(self, *, kind: str | None = None) -> dict[str, dict]:
        """span_norm → the memory rule in force (the newest row per span)."""
        sql = "SELECT * FROM vw_current_corrections"
        args: tuple = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            args = (kind,)
        return {r["span_norm"]: dict(r) for r in self.db.all(sql + " ORDER BY span_norm", args)}

    def correction_count(self) -> int:
        """Rules in force (distinct normalized spans)."""
        return self._row("SELECT COUNT(*) AS n FROM vw_current_corrections")["n"]

    # ---- runs ----------------------------------------------------------------

    def open_run(self, scope: str, *, created_by: str) -> str:
        return self.db.insert(
            "runs",
            ko_fields("run", created_by=created_by)
            | {"scope": scope, "status": "running", "started_at": now_iso()},
        )

    def close_run(
        self, run_id: str, *, status: str, sources: int, failed_items: int, detail: str = ""
    ) -> None:
        if status not in ("ok", "error"):
            raise ValueError(f"a run closes as ok or error, not {status!r}")
        self.db.update(
            "runs",
            run_id,
            {
                "status": status,
                "finished_at": now_iso(),
                "sources": sources,
                "failed_items": failed_items,
                "detail": detail,
            },
        )

    def run(self, run_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return None if row is None else dict(row)

    def last_successful_run(self) -> dict | None:
        row = self.db.one(
            "SELECT id, scope, started_at, finished_at, sources FROM runs "
            "WHERE status = 'ok' ORDER BY finished_at DESC, rowid DESC LIMIT 1"
        )
        return None if row is None else dict(row)

    # ---- review answers (master data: what the review page saved, not yet landed) -----

    def review_answer(self, corpus_id: str, card_id: str) -> dict | None:
        row = self.db.one(
            "SELECT * FROM review_answers WHERE corpus_id = ? AND card_id = ?",
            (corpus_id, card_id),
        )
        return None if row is None else dict(row)

    def review_answers(self, corpus_id: str) -> list[dict]:
        """The answers saved for one corpus, by card id."""
        return [
            dict(r)
            for r in self.db.all(
                "SELECT * FROM review_answers WHERE corpus_id = ? ORDER BY card_id", (corpus_id,)
            )
        ]

    def review_answer_counts(self) -> dict[str, int]:
        """corpus id -> how many answers wait to land, for the corpora that have any."""
        return {
            r["corpus_id"]: r["n"]
            for r in self.db.all(
                "SELECT corpus_id, COUNT(*) AS n FROM review_answers GROUP BY corpus_id "
                "ORDER BY corpus_id"
            )
        }

    def save_review_answer(
        self, corpus_id: str, card_id: str, verdict: str, answered_at: str, *, created_by: str
    ) -> None:
        """One answer per (corpus, card): a card answered again replaces its row (the change log
        keeps the old values). An unknown corpus is a ValueError."""
        if self.corpus(corpus_id) is None:
            raise ValueError("no such corpus")
        with self.db.transaction():
            existing = self.review_answer(corpus_id, card_id)
            if existing is None:
                self.db.insert(
                    "review_answers",
                    ko_fields("ans", created_by=created_by)
                    | {
                        "corpus_id": corpus_id,
                        "card_id": card_id,
                        "verdict": verdict,
                        "answered_at": answered_at,
                    },
                )
            else:
                self.db.update(
                    "review_answers",
                    existing["id"],
                    {"verdict": verdict, "answered_at": answered_at},
                )

    def delete_review_answer(self, corpus_id: str, card_id: str) -> bool:
        cursor = self.db.execute(
            "DELETE FROM review_answers WHERE corpus_id = ? AND card_id = ?", (corpus_id, card_id)
        )
        return cursor.rowcount > 0

    # ---- model suggestions (derived state: the clean-up model's top suggestions) -------

    def model_suggestions(self, source_id: str) -> dict[str, dict]:
        """The recorded suggestions of one source by review key (`amb-0001`): `span_norm`,
        `suggestion`, `suggestion_probability` (None when the model gave none), `model`,
        `artifact_sha256`, `candidates` and `probabilities` (one per candidate, in the model's
        order; a probability is None when the model gave none)."""
        out: dict[str, dict] = {}
        for row in self.db.all(
            "SELECT * FROM model_suggestions WHERE source_id = ? ORDER BY key", (source_id,)
        ):
            candidates = self.db.all(
                "SELECT candidate, probability FROM model_suggestion_candidates "
                "WHERE suggestion_id = ? ORDER BY position",
                (row["id"],),
            )
            out[row["key"]] = {
                "span_norm": row["span_norm"],
                "suggestion": row["suggestion"],
                "suggestion_probability": row["suggestion_probability"],
                "model": row["model"],
                "artifact_sha256": row["artifact_sha256"],
                "candidates": [c["candidate"] for c in candidates],
                "probabilities": [c["probability"] for c in candidates],
            }
        return out

    def replace_model_suggestions(
        self,
        source_id: str,
        suggestions: Sequence[dict],
        *,
        artifact_sha256: str,
        model: str,
        created_by: str,
    ) -> bool:
        """Make the source's recorded suggestions exactly `suggestions` (each: `key`,
        `span_norm`, `suggestion`, `suggestion_probability`, `candidates`, `probabilities`), read
        from the model clean-up artifact with `artifact_sha256`. A rebuilt artifact replaces the
        rows of the one before. Nothing is written, and False returned, when the rows already come
        from this artifact. An unknown source is a KeyError."""
        if not self.has_source(source_id):
            raise KeyError(f"unknown source {source_id!r}")
        keys = [item["key"] for item in suggestions]
        if len(set(keys)) != len(keys):
            raise ValueError(f"model suggestions of {source_id} repeat a review key")
        with self.db.transaction():
            held = self._row(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT artifact_sha256) AS shas, "
                "MIN(artifact_sha256) AS sha FROM model_suggestions WHERE source_id = ?",
                (source_id,),
            )
            if held["n"] == len(suggestions) and (
                held["n"] == 0 or (held["shas"] == 1 and held["sha"] == artifact_sha256)
            ):
                return False
            self.db.execute(
                "DELETE FROM model_suggestion_candidates WHERE suggestion_id IN "
                "(SELECT id FROM model_suggestions WHERE source_id = ?)",
                (source_id,),
            )
            self.db.execute("DELETE FROM model_suggestions WHERE source_id = ?", (source_id,))
            for item in suggestions:
                fields = ko_fields("sug", created_by=created_by)
                self.db.insert(
                    "model_suggestions",
                    fields
                    | {
                        "source_id": source_id,
                        "key": item["key"],
                        "span_norm": item["span_norm"],
                        "suggestion": item["suggestion"],
                        "suggestion_probability": item["suggestion_probability"],
                        "model": model,
                        "artifact_sha256": artifact_sha256,
                    },
                )
                for position, (candidate, probability) in enumerate(
                    zip(item["candidates"], item["probabilities"], strict=True)
                ):
                    self.db.insert(
                        "model_suggestion_candidates",
                        ko_fields("sgc", created_by=created_by)
                        | {
                            "suggestion_id": fields["id"],
                            "position": position,
                            "candidate": candidate,
                            "probability": probability,
                        },
                    )
        return True

    def model_suggestion_counts(self) -> dict[str, int]:
        """`sources` with a suggestion, `suggestions` recorded, and how many of them
        `with_probability`."""
        row = self._row(
            "SELECT COUNT(DISTINCT source_id) AS sources, COUNT(*) AS suggestions, "
            "COUNT(suggestion_probability) AS with_probability FROM model_suggestions"
        )
        return {k: row[k] for k in ("sources", "suggestions", "with_probability")}

    # ---- change log (history of master data) ----------------------------------------

    def changes_of(self, table: str, row_id: str) -> list[dict]:
        return [
            dict(r)
            for r in self.db.all(
                "SELECT column_name, old_value, new_value, row_version, created_at "
                "FROM change_log WHERE table_name = ? AND row_id = ? ORDER BY rowid",
                (table, row_id),
            )
        ]


def metadata_of(row: dict) -> dict:
    """A row's `metadata` JSON object (fails loudly when it does not parse)."""
    return loads(row.get("metadata"), {})
