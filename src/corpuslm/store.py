"""The Repository (architecture.md, "OOAD patterns"): the one module that
reads and writes the workspace database. Every other module asks it by
name (`video`, `record_artifact`, `append_review_verdict` ...) and never
writes SQL; `db.py` holds the connection, the schema and the migrations.
The tables are described in docs/design/storage-schema.md.

Rows come back as plain dicts. A video or corpus carries its lists
(`tags`, `topics`, `people`, `sources`; `topic`, `perspective`, `sources`) as Python
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
VIDEO_LISTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tags": ("video_tags", ("tag",)),
    "topics": ("video_topics", ("topic",)),
    "people": ("video_people", ("name", "role")),
    "sources": ("video_sources", ("source_id",)),
}
CORPUS_LISTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "topic": ("corpus_topics", ("topic",)),
    "perspective": ("corpus_perspectives", ("perspective",)),
    "sources": ("corpus_sources", ("source_id",)),
}
_LIST_PREFIX = {
    "video_tags": "vtg",
    "video_topics": "vtp",
    "video_people": "vpp",
    "video_sources": "vsr",
    "corpus_topics": "ctp",
    "corpus_perspectives": "cps",
    "corpus_sources": "csr",
}


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

    # ---- videos ------------------------------------------------------------

    def video(self, video_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM videos WHERE id = ?", (video_id,))
        return None if row is None else self._with_lists(row, VIDEO_LISTS, "video_id")

    def _video_row(self, video_id: str) -> dict:
        video = self.video(video_id)
        if video is None:
            raise KeyError(f"unknown video {video_id!r}")
        return video

    def _corpus_row(self, corpus_id: str) -> dict:
        corpus = self.corpus(corpus_id)
        if corpus is None:
            raise KeyError(f"unknown corpus {corpus_id!r}")
        return corpus

    def has_video(self, video_id: str) -> bool:
        return self.db.one("SELECT 1 FROM videos WHERE id = ?", (video_id,)) is not None

    def video_ids(self) -> list[str]:
        return [r["id"] for r in self.db.all("SELECT id FROM videos ORDER BY created_at, rowid")]

    def add_video(self, video_id: str, url: str, *, created_by: str) -> bool:
        """False when the video is already in the catalog."""
        if self.has_video(video_id):
            return False
        self.db.insert("videos", ko_fields(id=video_id, created_by=created_by) | {"url": url})
        return True

    def update_video(self, video_id: str, changes: dict[str, Any], *, created_by: str = "cli"):
        """Scalar columns update in place (logged by trigger); `tags`,
        `topics` and `people` replace their lists."""
        scalar, listed = self._split(changes, VIDEO_LISTS)
        with self.db.transaction():
            if scalar:
                self.db.update("videos", video_id, scalar)
            for name, values in listed.items():
                table, columns = VIDEO_LISTS[name]
                self._write_list(table, "video_id", video_id, columns, values, created_by)

    def video_counts_by(self, column: str) -> dict[str, int]:
        if column not in ("status", "fetcher"):
            raise ValueError(f"no count by {column!r}")
        return {
            r["k"]: r["n"]
            for r in self.db.all(f"SELECT {column} AS k, COUNT(*) AS n FROM videos GROUP BY 1")
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

    def corpus_ids(self) -> list[str]:
        return [r["id"] for r in self.db.all("SELECT id FROM corpora ORDER BY rowid")]

    def corpus_names(self) -> list[str]:
        return [r["name"] for r in self.db.all("SELECT name FROM corpora ORDER BY name")]

    def corpus_count(self) -> int:
        return self._row("SELECT COUNT(*) AS n FROM corpora")["n"]

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
        """The corpus document's members: ingested video rows (without their
        lists), by publication date then id."""
        return [
            dict(r)
            for r in self.db.all(
                "SELECT v.* FROM videos v JOIN corpus_members m ON m.video_id = v.id "
                "WHERE m.corpus_id = ? AND v.status = 'ingested' ORDER BY v.published_at, v.id",
                (corpus_id,),
            )
        ]

    def set_corpus_metadata(self, corpus_id: str, metadata: dict) -> None:
        """Replace a corpus's metadata object (logged by the change-log trigger)."""
        self.db.update("corpora", corpus_id, {"metadata": json.dumps(metadata, sort_keys=True)})

    def is_member(self, corpus_id: str, video_id: str) -> bool:
        return (
            self.db.one(
                "SELECT 1 FROM corpus_members WHERE corpus_id = ? AND video_id = ?",
                (corpus_id, video_id),
            )
            is not None
        )

    def add_member(self, corpus_id: str, video_id: str, reason: str, *, created_by: str) -> str:
        return self.db.insert(
            "corpus_members",
            ko_fields("mem", created_by=created_by)
            | {"corpus_id": corpus_id, "video_id": video_id, "reason": reason},
        )

    def member_video_ids(self, corpus_id: str) -> list[str]:
        return [
            r["video_id"]
            for r in self.db.all(
                "SELECT video_id FROM corpus_members WHERE corpus_id = ? "
                "ORDER BY created_at, rowid",
                (corpus_id,),
            )
        ]

    def member_count(self, corpus_id: str) -> int:
        return self._row(
            "SELECT COUNT(*) AS n FROM corpus_members WHERE corpus_id = ?", (corpus_id,)
        )["n"]

    def member_status_counts(self, corpus_id: str) -> dict[str, int]:
        """Membership status is derived from the video's status
        (vw_corpus_members): queued, ingested or failed."""
        return {
            r["member_status"]: r["n"]
            for r in self.db.all(
                "SELECT member_status, COUNT(*) AS n FROM vw_corpus_members "
                "WHERE corpus_id = ? GROUP BY member_status",
                (corpus_id,),
            )
        }

    def corpus_summaries(self) -> list[dict]:
        rows = self.db.all(
            "SELECT c.name, c.id, c.person, c.subject, COUNT(m.id) AS members, "
            "SUM(CASE WHEN m.member_status = 'ingested' THEN 1 ELSE 0 END) AS ingested "
            "FROM corpora c LEFT JOIN vw_corpus_members m ON m.corpus_id = c.id "
            "GROUP BY c.id ORDER BY c.name"
        )
        return [dict(r) for r in rows]

    def member_videos(self, corpus_id: str, *, ingested_only: bool = False) -> list[dict]:
        """Member video rows (with their lists) plus `member_status` and
        `member_reason`, in publication order."""
        rows = self.db.all(
            "SELECT v.id, m.member_status, m.reason AS member_reason "
            "FROM vw_corpus_members m JOIN videos v ON v.id = m.video_id "
            "WHERE m.corpus_id = ? "
            + ("AND v.status = 'ingested' " if ingested_only else "")
            + "ORDER BY v.published_at, v.title, v.id",
            (corpus_id,),
        )
        out = []
        for r in rows:
            video = self._video_row(r["id"])
            video["member_status"] = r["member_status"]
            video["member_reason"] = r["member_reason"]
            out.append(video)
        return out

    def corpora_of_video(self, video_id: str) -> list[dict]:
        rows = self.db.all(
            "SELECT c.id FROM corpora c JOIN corpus_members m ON m.corpus_id = c.id "
            "WHERE m.video_id = ? ORDER BY c.name",
            (video_id,),
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
                        "SELECT id, video_id, reason, created_at, created_by, metadata "
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
                kept.append(snapshot)
                for m in members:
                    if self.is_member(into_id, m["video_id"]):
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
                for table, _ in CORPUS_LISTS.values():
                    self.db.execute(f"DELETE FROM {table} WHERE corpus_id = ?", (cid,))
                self.db.execute("DELETE FROM corpora WHERE id = ?", (cid,))
                removed.append(corpus["name"])
            self.set_corpus_metadata(into_id, record | {"merged_from": kept})
        return {"moved": moved, "already_members": already, "removed": removed}

    # ---- fetch attempts (evidence, append-only) ---------------------------------

    def append_fetch_attempt(
        self, video_id: str, fetcher: str, outcome: str, detail: str, *, created_by: str
    ) -> str:
        return self.db.insert(
            "fetch_attempts",
            ko_fields("fet", created_by=created_by)
            | {"video_id": video_id, "fetcher": fetcher, "outcome": outcome, "detail": detail},
        )

    # ---- artifact index (derived state) ------------------------------------------

    def _owner_column(self, owner: str) -> str:
        if self.has_video(owner):
            return "video_id"
        if self.db.one("SELECT 1 FROM corpora WHERE id = ?", (owner,)):
            return "corpus_id"
        raise KeyError(f"artifact owner {owner!r} is neither a video nor a corpus")

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
        """Every artifact row of one video or corpus, by node name."""
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

    def replace_segments(self, video_id: str, segments: Iterable[dict]) -> None:
        """The FTS rows of one video, rebuilt whole. Transcript segments keep
        their JSON names (idx, t0, t1); the columns are position,
        start_seconds and end_seconds."""
        with self.db.transaction():
            self.db.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
            for seg in segments:
                self.db.execute(
                    "INSERT INTO segments(video_id, position, start_seconds, end_seconds, "
                    "speaker, text) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        video_id,
                        seg["idx"],
                        seg["t0"],
                        seg["t1"],
                        seg.get("speaker", ""),
                        seg["text"],
                    ),
                )

    def delete_segments(self, video_id: str) -> int:
        """Remove one video's segments; the segments_ad trigger removes their
        FTS5 rows. Returns the segments removed."""
        return self.db.execute("DELETE FROM segments WHERE video_id = ?", (video_id,)).rowcount

    def segment_count(self, video_id: str | None = None) -> int:
        if video_id is None:
            return self._row("SELECT COUNT(*) AS n FROM segments")["n"]
        return self._row("SELECT COUNT(*) AS n FROM segments WHERE video_id = ?", (video_id,))["n"]

    def search(self, match: str, *, corpus_id: str | None, limit: int) -> list[dict]:
        """FTS5 hits, best first, through vw_search_segments. Raises
        sqlite3.OperationalError when `match` is not valid FTS5 syntax."""
        sql = (
            "SELECT h.video_id, h.position, h.start_seconds, h.end_seconds, h.title, "
            "snippet(segments_fts, 0, '[', ']', '…', 14) AS snippet, "
            "bm25(segments_fts) AS rank "
            "FROM segments_fts JOIN vw_search_segments h ON h.segment_rowid = segments_fts.rowid "
            "WHERE segments_fts MATCH ? "
            + (
                "AND EXISTS (SELECT 1 FROM corpus_members m "
                "WHERE m.video_id = h.video_id AND m.corpus_id = ?) "
                if corpus_id
                else ""
            )
            + "ORDER BY rank LIMIT ?"
        )
        args = [match] + ([corpus_id] if corpus_id else []) + [limit]
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
        video_id: str,
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
        """A new verdict row; the newest row for (video_id, key) is in force.
        `previous_subject_sha256` is filled when the slot's current verdict
        judged different content (lineage: new hash → old hash)."""
        current = self.db.one(
            "SELECT subject_sha256 FROM vw_current_review_verdicts WHERE video_id = ? AND key = ?",
            (video_id, key),
        )
        previous = (
            current["subject_sha256"]
            if current and current["subject_sha256"] != subject_sha256
            else None
        )
        row = ko_fields("ver", created_by=created_by) | {
            "video_id": video_id,
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

    def current_review_verdicts(self, video_id: str) -> dict[str, dict]:
        """key → the verdict in force (the newest row for that key)."""
        return {
            r["key"]: dict(r)
            for r in self.db.all(
                "SELECT * FROM vw_current_review_verdicts WHERE video_id = ? ORDER BY key",
                (video_id,),
            )
        }

    def review_verdict_history(self, video_id: str, key: str) -> list[dict]:
        """Every verdict ever given for one slot, oldest first."""
        return [
            dict(r)
            for r in self.db.all(
                "SELECT * FROM review_verdicts WHERE video_id = ? AND key = ? ORDER BY rowid",
                (video_id, key),
            )
        ]

    def append_speaker_verdict(
        self, *, video_id: str, role: str, name: str, created_by: str
    ) -> str:
        return self.db.insert(
            "speaker_verdicts",
            ko_fields("spk", created_by=created_by)
            | {"video_id": video_id, "role": role, "name": name},
        )

    def current_speaker_names(self, video_id: str) -> dict[str, str]:
        """role → the author's name in force for that role."""
        return {
            r["role"]: r["name"]
            for r in self.db.all(
                "SELECT role, name FROM vw_current_speaker_verdicts WHERE video_id = ? "
                "ORDER BY role",
                (video_id,),
            )
        }

    def append_correction(
        self,
        *,
        span_norm: str,
        verdict: str,
        kind: str,
        origin_video: str,
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
                "origin_video": origin_video,
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
        self, run_id: str, *, status: str, videos: int, failed_items: int, detail: str = ""
    ) -> None:
        if status not in ("ok", "error"):
            raise ValueError(f"a run closes as ok or error, not {status!r}")
        self.db.update(
            "runs",
            run_id,
            {
                "status": status,
                "finished_at": now_iso(),
                "videos": videos,
                "failed_items": failed_items,
                "detail": detail,
            },
        )

    def run(self, run_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return None if row is None else dict(row)

    def last_successful_run(self) -> dict | None:
        row = self.db.one(
            "SELECT id, scope, started_at, finished_at, videos FROM runs "
            "WHERE status = 'ok' ORDER BY finished_at DESC, rowid DESC LIMIT 1"
        )
        return None if row is None else dict(row)

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
