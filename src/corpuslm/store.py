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
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from .db import Database, loads, now_iso, standard_fields
from .identity import canonical_sha256

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
            return [row[columns[0]] for row in rows]
        return [{column: row[column] for column in columns} for row in rows]

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
            return {column: value.get(column, "") for column in columns}

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
                        standard_fields(_LIST_PREFIX[table], created_by=created_by)
                        | {parent: parent_id, "position": position}
                        | element(after),
                    )

    def _with_lists(self, row, lists, parent: str) -> dict:
        out = dict(row)
        for name, (table, columns) in lists.items():
            out[name] = self._read_list(table, parent, out["id"], columns)
        return out

    def _split(self, changes: dict, lists) -> tuple[dict, dict]:
        scalar = {
            field_name: value for field_name, value in changes.items() if field_name not in lists
        }
        listed = {field_name: value for field_name, value in changes.items() if field_name in lists}
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

    def source_ids(self, kind: str | None = None, *, include_ignored: bool = True) -> list[str]:
        """Every source id, oldest first; only those of `kind` (`youtube`, `bee`) when given.
        `include_ignored=False` leaves out the sources whose status is `ignored`: the answer for
        anything that builds, counts or lists work for the owner; the catalog (is it known?) keeps
        the default."""
        clauses = (["kind = ?"] if kind else []) + (
            [] if include_ignored else ["status <> 'ignored'"]
        )
        args = [kind] if kind else []
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        return [
            row["id"]
            for row in self.db.all(
                f"SELECT id FROM sources {where}ORDER BY created_at, rowid", args
            )
        ]

    def add_source(
        self, source_id: str, locator: str, *, created_by: str, kind: str = "youtube"
    ) -> bool:
        """False when the source is already in the catalog."""
        if self.has_source(source_id):
            return False
        self.db.insert(
            "sources",
            standard_fields(id=source_id, created_by=created_by)
            | {"kind": kind, "locator": locator},
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
            row["k"]: row["n"]
            for row in self.db.all(
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
        return [row["id"] for row in self.db.all(sql, args)]

    def corpus_names(self, kind: str | None = None) -> list[str]:
        where, args = _of_kind(kind)
        sql = f"SELECT name FROM corpora c {where}ORDER BY name"
        return [row["name"] for row in self.db.all(sql, args)]

    def corpus_count(self, kind: str | None = None) -> int:
        """How many corpora; with `kind`, only those a tool of that source kind reads."""
        where, args = _of_kind(kind)
        return self._row(f"SELECT COUNT(*) AS n FROM corpora c {where}".strip(), args)["n"]

    def add_corpus(self, fields: dict[str, Any], *, created_by: str) -> str:
        scalar, listed = self._split(fields, CORPUS_LISTS)
        with self.db.transaction():
            corpus_id = self.db.insert(
                "corpora", standard_fields("cor", created_by=created_by) | scalar
            )
            for name, values in listed.items():
                table, columns = CORPUS_LISTS[name]
                self._write_list(table, "corpus_id", corpus_id, columns, values, created_by)
        return corpus_id

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
            dict(row)
            for row in self.db.all(
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
            standard_fields("mem", created_by=created_by)
            | {"corpus_id": corpus_id, "source_id": source_id, "reason": reason},
        )

    def member_rows(self, corpus_id: str) -> list[dict]:
        """Each membership of the corpus as stored (its id, source, reason, who added it and
        when), oldest first."""
        return [
            dict(row)
            for row in self.db.all(
                "SELECT id, source_id, reason, created_at, created_by, version "
                "FROM corpus_members WHERE corpus_id = ? ORDER BY created_at, rowid",
                (corpus_id,),
            )
        ]

    def remove_member(self, corpus_id: str, source_id: str, *, created_by: str) -> bool:
        """Take one video out of one corpus; False when it was not a member. The video, its
        transcript and its other memberships are left alone. The removal leaves its trail in
        the change log in the same transaction: one row naming the membership, with the old
        value (corpus, source, reason, who added it and when) and no new value, written by
        `created_by`: a deleted master row is never silent."""
        with self.db.transaction():
            row = self.db.one(
                "SELECT id, version, corpus_id, source_id, reason, created_at, created_by "
                "FROM corpus_members WHERE corpus_id = ? AND source_id = ?",
                (corpus_id, source_id),
            )
            if row is None:
                return False
            was = {column: row[column] for column in ("corpus_id", "source_id", "reason")} | {
                "added_at": row["created_at"],
                "added_by": row["created_by"],
            }
            self.db.insert(
                "change_log",
                standard_fields("chg", created_by=created_by)
                | {
                    "table_name": "corpus_members",
                    "row_id": row["id"],
                    "column_name": "membership",
                    "old_value": json.dumps(was, sort_keys=True),
                    "new_value": None,
                    "row_version": row["version"],
                },
            )
            self.db.execute("DELETE FROM corpus_members WHERE id = ?", (row["id"],))
        return True

    def member_source_ids(self, corpus_id: str, *, include_ignored: bool = True) -> list[str]:
        """The corpus's member ids, oldest membership first. `include_ignored=False` leaves out
        the sources whose status is `ignored` (what a build, a count or a review works from);
        the default keeps every member (a merge moves them all)."""
        return [
            row["source_id"]
            for row in self.db.all(
                "SELECT m.source_id FROM corpus_members m JOIN sources v ON v.id = m.source_id "
                "WHERE m.corpus_id = ? "
                + ("" if include_ignored else "AND v.status <> 'ignored' ")
                + "ORDER BY m.created_at, m.rowid",
                (corpus_id,),
            )
        ]

    def member_count(self, corpus_id: str) -> int:
        return self._row(
            "SELECT COUNT(*) AS n FROM corpus_members WHERE corpus_id = ?", (corpus_id,)
        )["n"]

    def member_status_counts(self, corpus_id: str) -> dict[str, int]:
        """Membership status is derived from the source's status
        (vw_corpus_members): queued, ingested, failed or ignored. An ignored member is counted
        under its own key; callers leave it out of a corpus's member total."""
        return {
            row["member_status"]: row["n"]
            for row in self.db.all(
                "SELECT member_status, COUNT(*) AS n FROM vw_corpus_members "
                "WHERE corpus_id = ? GROUP BY member_status",
                (corpus_id,),
            )
        }

    def corpus_summaries(self, kind: str | None = None) -> list[dict]:
        where, args = _of_kind(kind)
        rows = self.db.all(
            "SELECT c.name, c.id, c.person, c.subject, "
            "COUNT(m.id) - COALESCE(SUM(m.member_status = 'ignored'), 0) AS members, "
            "COALESCE(SUM(m.member_status = 'ingested'), 0) AS ingested, "
            "COALESCE(SUM(m.member_status = 'ignored'), 0) AS ignored "
            "FROM corpora c LEFT JOIN vw_corpus_members m ON m.corpus_id = c.id "
            f"{where}GROUP BY c.id ORDER BY c.name",
            args,
        )
        return [dict(row) for row in rows]

    def member_sources(self, corpus_id: str, *, ingested_only: bool = False) -> list[dict]:
        """Member source rows (with their lists) plus `member_status` and
        `member_reason`, in publication order. Ignored sources are left out: every caller lists
        or reviews what the owner works from."""
        rows = self.db.all(
            "SELECT v.id, m.member_status, m.reason AS member_reason "
            "FROM vw_corpus_members m JOIN sources v ON v.id = m.source_id "
            "WHERE m.corpus_id = ? AND v.status <> 'ignored' "
            + ("AND v.status = 'ingested' " if ingested_only else "")
            + "ORDER BY v.published_at, v.title, v.id",
            (corpus_id,),
        )
        out = []
        for membership in rows:
            row = self._source_row(membership["id"])
            row["member_status"] = membership["member_status"]
            row["member_reason"] = membership["member_reason"]
            out.append(row)
        return out

    def corpora_of_source(self, source_id: str) -> list[dict]:
        rows = self.db.all(
            "SELECT c.id FROM corpora c JOIN corpus_members m ON m.corpus_id = c.id "
            "WHERE m.source_id = ? ORDER BY c.name",
            (source_id,),
        )
        return [self._corpus_row(row["id"]) for row in rows]

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
            for corpus_id in from_ids:
                corpus = self._corpus_row(corpus_id)
                members = [
                    dict(row)
                    for row in self.db.all(
                        "SELECT id, source_id, reason, created_at, created_by, metadata "
                        "FROM corpus_members WHERE corpus_id = ? ORDER BY created_at, rowid",
                        (corpus_id,),
                    )
                ]
                snapshot = dict(corpus) | {
                    "metadata": loads(corpus["metadata"], {}),
                    "members": [
                        member | {"metadata": loads(member["metadata"], {})} for member in members
                    ],
                    "merged_at": now_iso(),
                    "merged_by": created_by,
                }
                # The answers not yet landed are keyed by card, which hashes the corpus id: they
                # cannot follow a membership, so the folded corpus carries them in its snapshot.
                snapshot["review_answers"] = [
                    {
                        column: answer[column]
                        for column in ("card_id", "verdict", "answered_at", "created_at")
                    }
                    for answer in self.review_answers(corpus_id)
                ]
                kept.append(snapshot)
                for member in members:
                    if self.is_member(into_id, member["source_id"]):
                        self.db.execute("DELETE FROM corpus_members WHERE id = ?", (member["id"],))
                        already += 1
                        continue
                    meta = loads(member["metadata"], {}) | {"merged_from": corpus_id}
                    self.db.update(
                        "corpus_members",
                        member["id"],
                        {"corpus_id": into_id, "metadata": json.dumps(meta, sort_keys=True)},
                    )
                    moved += 1
                self.db.execute("DELETE FROM artifacts WHERE corpus_id = ?", (corpus_id,))
                self.db.execute("DELETE FROM review_answers WHERE corpus_id = ?", (corpus_id,))
                for table, _ in CORPUS_LISTS.values():
                    self.db.execute(f"DELETE FROM {table} WHERE corpus_id = ?", (corpus_id,))
                self.db.execute("DELETE FROM corpora WHERE id = ?", (corpus_id,))
                removed.append(corpus["name"])
            self.set_corpus_metadata(into_id, record | {"merged_from": kept})
        return {"moved": moved, "already_members": already, "removed": removed}

    # ---- tracked feeds and skipped videos (master data) -----------------------------

    def feed(self, feed_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM feeds WHERE feed_id = ?", (feed_id,))
        return None if row is None else dict(row)

    def feeds(self) -> list[dict]:
        """Every tracked feed, oldest first."""
        return [dict(row) for row in self.db.all("SELECT * FROM feeds ORDER BY created_at, rowid")]

    def add_feed(self, feed_id: str, kind: str, title: str, *, created_by: str) -> bool:
        """Track a playlist or channel; False when it is already tracked (its title is
        refreshed when a new one is given)."""
        existing = self.feed(feed_id)
        if existing is not None:
            if title and title != existing["title"]:
                self.db.update("feeds", existing["id"], {"title": title})
            return False
        self.db.insert(
            "feeds",
            standard_fields("fed", created_by=created_by)
            | {"feed_id": feed_id, "kind": kind, "title": title},
        )
        return True

    def mark_feed_scanned(self, feed_id: str, *, when: str | None = None) -> None:
        existing = self.feed(feed_id)
        if existing is None:
            raise KeyError(f"unknown feed {feed_id!r}")
        self.db.update("feeds", existing["id"], {"last_scanned_at": when or now_iso()})

    def sources_found_through(self, feed_id: str) -> list[str]:
        """The catalog's sources whose `feeds` name this feed, oldest first."""
        return [
            row["source_id"]
            for row in self.db.all(
                "SELECT source_id FROM source_feeds WHERE feed_id = ? ORDER BY created_at, rowid",
                (feed_id,),
            )
        ]

    def remove_feed(self, feed_id: str, *, created_by: str) -> dict | None:
        """Stop tracking a feed; None when it was not tracked. In one transaction: the feed id
        leaves the `feeds` list of every source that carries it (through the same list writer
        `update_source` uses, so the change log records each removal), the skips made through
        the feed are deleted, and the feed's own row is deleted. Every source, segment and
        corpus membership is kept. The deleted rows leave their trail in the change log, one
        row each with the old value and no new value, written by `created_by`: a deleted
        master row is never silent. Returns the counts `sources_unstamped` and
        `skips_removed`."""
        with self.db.transaction():
            feed = self.feed(feed_id)
            if feed is None:
                return None
            carriers = self.sources_found_through(feed_id)
            for source_id in carriers:
                kept = [feed for feed in self._source_row(source_id)["feeds"] if feed != feed_id]
                self.update_source(source_id, {"feeds": kept}, created_by=created_by)
            skips = self.db.all("SELECT * FROM source_skips WHERE feed_id = ?", (feed_id,))
            for skip in skips:
                self._log_deleted_row("source_skips", skip, created_by)
                self.db.execute("DELETE FROM source_skips WHERE id = ?", (skip["id"],))
            self._log_deleted_row("feeds", feed, created_by)
            self.db.execute("DELETE FROM feeds WHERE id = ?", (feed["id"],))
        return {"sources_unstamped": len(carriers), "skips_removed": len(skips)}

    def _log_deleted_row(self, table: str, row, created_by: str) -> None:
        """One change-log row for a master row about to be deleted: the whole row as the old
        value (its bookkeeping columns as `added_at` and `added_by`), no new value."""
        was = {
            column: row[column]
            for column in row.keys()
            if column not in ("id", "version", "created_at", "created_by", "schema_version")
        } | {"added_at": row["created_at"], "added_by": row["created_by"]}
        self.db.insert(
            "change_log",
            standard_fields("chg", created_by=created_by)
            | {
                "table_name": table,
                "row_id": row["id"],
                "column_name": "row",
                "old_value": json.dumps(was, sort_keys=True),
                "new_value": None,
                "row_version": row["version"],
            },
        )

    def skip(self, source_id: str) -> dict | None:
        """The skip in force for a video, or None (never skipped, or lifted)."""
        row = self.db.one(
            "SELECT * FROM source_skips WHERE source_id = ? AND lifted_at IS NULL", (source_id,)
        )
        return None if row is None else dict(row)

    def skips(self, *, feed_id: str | None = None) -> list[dict]:
        """The skips in force, oldest first; only those made through `feed_id` when given."""
        where, args = (" AND feed_id = ?", [feed_id]) if feed_id else ("", [])
        return [
            dict(row)
            for row in self.db.all(
                f"SELECT * FROM source_skips WHERE lifted_at IS NULL{where} "
                "ORDER BY skipped_at, rowid",
                args,
            )
        ]

    def skip_source(
        self,
        source_id: str,
        reason: str,
        *,
        feed_id: str | None = None,
        title: str = "",
        created_by: str,
    ) -> bool:
        """Record that a video is skipped (reason required; never silent). False when it is
        already skipped. A video skipped again after `lift_skip` takes its old row back, with
        the new reason and date; the change log keeps the earlier values."""
        if self.skip(source_id) is not None:
            return False
        now = now_iso()
        row = self.db.one("SELECT id FROM source_skips WHERE source_id = ?", (source_id,))
        with self.db.transaction():
            if row is None:
                self.db.insert(
                    "source_skips",
                    standard_fields("skp", created_by=created_by)
                    | {
                        "source_id": source_id,
                        "feed_id": feed_id,
                        "title": title,
                        "reason": reason,
                        "skipped_at": now,
                    },
                )
            else:
                self.db.update(
                    "source_skips",
                    row["id"],
                    {
                        "feed_id": feed_id,
                        "title": title,
                        "reason": reason,
                        "skipped_at": now,
                        "lifted_at": None,
                    },
                )
        return True

    def lift_skip(self, source_id: str) -> bool:
        """Reverse a skip (`unskip`); False when none is in force. The row stays, dated."""
        row = self.skip(source_id)
        if row is None:
            return False
        self.db.update("source_skips", row["id"], {"lifted_at": now_iso()})
        return True

    # ---- fetch attempts (evidence, append-only) ---------------------------------

    def append_fetch_attempt(
        self, source_id: str, fetcher: str, outcome: str, detail: str, *, created_by: str
    ) -> str:
        return self.db.insert(
            "fetch_attempts",
            standard_fields("fet", created_by=created_by)
            | {"source_id": source_id, "fetcher": fetcher, "outcome": outcome, "detail": detail},
        )

    def fetch_attempt_counts(self) -> list[dict]:
        """How many attempts each fetcher made per UTC day and outcome, oldest
        day first. Counts only; the detail text never leaves the table."""
        return [
            dict(row)
            for row in self.db.all(
                "SELECT substr(created_at, 1, 10) AS day, fetcher, outcome, COUNT(*) AS n, "
                "MAX(substr(detail, 1, 120)) AS last_detail FROM fetch_attempts "
                "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
            )
        ]

    # ---- artifact index (derived state) ------------------------------------------

    def _id_column_for(self, source_or_corpus_id: str) -> str:
        if self.has_source(source_or_corpus_id):
            return "source_id"
        if self.db.one("SELECT 1 FROM corpora WHERE id = ?", (source_or_corpus_id,)):
            return "corpus_id"
        raise KeyError(f"artifact {source_or_corpus_id!r} belongs to neither a source nor a corpus")

    def artifact(self, source_or_corpus_id: str, node: str) -> dict | None:
        row = self.db.one(
            "SELECT * FROM artifacts WHERE owner = ? AND node = ?", (source_or_corpus_id, node)
        )
        return None if row is None else dict(row)

    def record_artifact(
        self,
        source_or_corpus_id: str,
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
        row = self.artifact(source_or_corpus_id, node)
        if row:
            self.db.update("artifacts", row["id"], changes)
        else:
            self.db.insert(
                "artifacts",
                standard_fields("art", created_by=created_by)
                | {self._id_column_for(source_or_corpus_id): source_or_corpus_id, "node": node}
                | changes,
            )

    def mark_stale(self, source_or_corpus_id: str, node: str) -> bool:
        row = self.artifact(source_or_corpus_id, node)
        if not row:
            return False
        self.db.update("artifacts", row["id"], {"state": "stale"})
        return True

    def artifacts_of(self, source_or_corpus_id: str) -> list[dict]:
        """Every artifact row of one source or corpus, by node name."""
        return [
            dict(row)
            for row in self.db.all(
                "SELECT * FROM artifacts WHERE owner = ? ORDER BY node", (source_or_corpus_id,)
            )
        ]

    def delete_artifacts(self, source_or_corpus_id: str, nodes: Sequence[str]) -> int:
        """Remove the artifact rows of `nodes` for one source or corpus (derived state, no
        history); the runner then finds them missing and builds them. Returns
        the rows removed."""
        if not nodes:
            return 0
        marks = ", ".join("?" * len(nodes))
        cursor = self.db.execute(
            f"DELETE FROM artifacts WHERE owner = ? AND node IN ({marks})",
            (source_or_corpus_id, *nodes),
        )
        return cursor.rowcount

    def artifact_health(self) -> dict:
        """Counts for `status`: built per node, stale, AI cleanup drops, and
        provisional transcripts."""
        built = {
            row["node"]: row["n"]
            for row in self.db.all(
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

    # ---- claims (derived state, corpuslm.claims) --------------------------------------

    def replace_claims(
        self,
        source_id: str,
        claims: Iterable[dict],
        *,
        build_key: str,
        run_id: str = "",
        created_by: str,
    ) -> int:
        """One source's claims, rebuilt whole in one transaction. Each claim is
        `{"section", "segments": [position, ...], "text", "kind", "speaker"}`; its
        position is its place in the list. Returns how many were written."""
        written = 0
        with self.db.transaction():
            self.db.execute("DELETE FROM claims WHERE source_id = ?", (source_id,))
            for position, claim in enumerate(claims):
                self.db.insert(
                    "claims",
                    standard_fields("clm", created_by=created_by)
                    | {
                        "source_id": source_id,
                        "position": position,
                        "section": int(claim["section"]),
                        "segments": json.dumps([int(index) for index in claim["segments"]]),
                        "text": claim["text"],
                        "kind": claim["kind"],
                        "speaker": claim.get("speaker", ""),
                        "build_key": build_key,
                        "run_id": run_id,
                    },
                )
                written += 1
        return written

    def claims(self, source_id: str) -> list[dict]:
        """One source's claims in order; `segments` is the list of segment positions."""
        return [
            dict(row) | {"segments": loads(row["segments"], [])}
            for row in self.db.all(
                "SELECT * FROM claims WHERE source_id = ? ORDER BY position", (source_id,)
            )
        ]

    def delete_claims(self, source_id: str) -> int:
        return self.db.execute("DELETE FROM claims WHERE source_id = ?", (source_id,)).rowcount

    def claim_count(self, source_id: str | None = None) -> int:
        if source_id is None:
            return self._row("SELECT COUNT(*) AS n FROM claims")["n"]
        return self._row("SELECT COUNT(*) AS n FROM claims WHERE source_id = ?", (source_id,))["n"]

    def claim_counts_by_source(self) -> dict[str, int]:
        """Source id -> how many claims it has, for every source with at least one."""
        return {
            row["source_id"]: row["n"]
            for row in self.db.all(
                "SELECT source_id, COUNT(*) AS n FROM claims GROUP BY source_id ORDER BY source_id"
            )
        }

    def claim_kind_counts(self) -> dict[str, int]:
        return {
            row["kind"]: row["n"]
            for row in self.db.all(
                "SELECT kind, COUNT(*) AS n FROM claims GROUP BY kind ORDER BY kind"
            )
        }

    def best_segments(self, source_id: str) -> list[dict]:
        """The segments a reader should see: the cleaned ones when the source has them (Bee),
        else the raw ones (YouTube, whose raw lines are already the best transcript). The JSON
        names (idx, t0, t1, speaker, text), in order."""
        rows = self.db.all(
            "SELECT position, start_seconds, end_seconds, speaker, text FROM cleaned_segments "
            "WHERE source_id = ? ORDER BY position",
            (source_id,),
        )
        if not rows:
            return self.segments(source_id)
        return [
            {
                "idx": row["position"],
                "t0": row["start_seconds"],
                "t1": row["end_seconds"],
                "speaker": row["speaker"],
                "text": row["text"],
            }
            for row in rows
        ]

    # ---- concepts (derived lexicon, corpuslm.concepts) ------------------------------------

    def concept(self, key: str) -> dict | None:
        row = self.db.one("SELECT * FROM concepts WHERE key = ?", (key,))
        return None if row is None else dict(row) | {"alternates": loads(row["alternates"], [])}

    def concept_by_id(self, concept_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM concepts WHERE id = ?", (concept_id,))
        return None if row is None else dict(row) | {"alternates": loads(row["alternates"], [])}

    def concepts(self) -> list[dict]:
        """Every concept, most used first."""
        return [
            dict(row) | {"alternates": loads(row["alternates"], [])}
            for row in self.db.all(
                "SELECT * FROM concepts ORDER BY claim_count DESC, section_count DESC, key"
            )
        ]

    def concept_count(self) -> int:
        return self._row("SELECT COUNT(*) AS n FROM concepts")["n"]

    def upsert_concept(self, key: str, label: str, *, created_by: str) -> str:
        """The concept id for `key`, made with `label` when new; a label that differs from the
        stored one is kept as an alternate."""
        row = self.db.one("SELECT id, label, alternates FROM concepts WHERE key = ?", (key,))
        if row is None:
            return self.db.insert(
                "concepts",
                standard_fields("cpt", created_by=created_by) | {"key": key, "label": label},
            )
        alternates = loads(row["alternates"], [])
        if label != row["label"] and label not in alternates:
            self.db.update("concepts", row["id"], {"alternates": json.dumps(alternates + [label])})
        return row["id"]

    def tag_claims(
        self, tags: Mapping[str, Sequence[tuple[str, str]]], *, build_key: str, created_by: str
    ) -> int:
        """`tags` is claim id -> [(key, label), ...]. Each claim's tags are replaced whole, in
        one transaction, with the concepts made as needed. Returns the tags written."""
        written = 0
        with self.db.transaction():
            for claim_id, pairs in tags.items():
                self.db.execute("DELETE FROM claim_concepts WHERE claim_id = ?", (claim_id,))
                seen: set[str] = set()
                for key, label in pairs:
                    if key in seen:
                        continue
                    seen.add(key)
                    concept_id = self.upsert_concept(key, label, created_by=created_by)
                    self.db.execute(
                        "INSERT INTO claim_concepts (claim_id, concept_id, build_key) "
                        "VALUES (?, ?, ?)",
                        (claim_id, concept_id, build_key),
                    )
                    written += 1
        return written

    def refresh_concept_counts(self) -> int:
        """Recount every concept's sections, claims and sources from its tags. Returns how many
        concepts have at least one tag."""
        with self.db.transaction():
            self.db.execute(
                "UPDATE concepts SET section_count = 0, claim_count = 0, source_count = 0"
            )
            rows = self.db.all(
                "SELECT t.concept_id AS concept_id, "
                "COUNT(DISTINCT c.source_id || ':' || c.section) AS sections, "
                "COUNT(DISTINCT c.id) AS claims, COUNT(DISTINCT c.source_id) AS sources "
                "FROM claim_concepts t JOIN claims c ON c.id = t.claim_id GROUP BY t.concept_id"
            )
            for row in rows:
                self.db.execute(
                    "UPDATE concepts SET section_count = ?, claim_count = ?, source_count = ? "
                    "WHERE id = ?",
                    (row["sections"], row["claims"], row["sources"], row["concept_id"]),
                )
        return len(rows)

    def claim_concept_keys(self, claim_id: str) -> list[str]:
        return [
            row["key"]
            for row in self.db.all(
                "SELECT p.key FROM claim_concepts t JOIN concepts p ON p.id = t.concept_id "
                "WHERE t.claim_id = ? ORDER BY p.key",
                (claim_id,),
            )
        ]

    def concept_edges(self, concept_id: str) -> list[dict]:
        """The concepts that share claims with `concept_id`, most shared first, each with the
        count and the ids of the shared claims: the evidence, never an assertion."""
        rows = self.db.all(
            "SELECT b.concept_id AS concept_id, p.key AS key, p.label AS label, "
            "COUNT(*) AS shared, json_group_array(a.claim_id) AS claim_ids "
            "FROM claim_concepts a JOIN claim_concepts b ON b.claim_id = a.claim_id "
            "AND b.concept_id <> a.concept_id JOIN concepts p ON p.id = b.concept_id "
            "WHERE a.concept_id = ? GROUP BY b.concept_id ORDER BY shared DESC, p.key",
            (concept_id,),
        )
        return [dict(row) | {"claim_ids": loads(row["claim_ids"], [])} for row in rows]

    def concept_section_histogram(self) -> dict[int, int]:
        """Section count -> how many concepts have it (the distribution the gate is set by)."""
        return {
            row["section_count"]: row["n"]
            for row in self.db.all(
                "SELECT section_count, COUNT(*) AS n FROM concepts GROUP BY section_count "
                "ORDER BY section_count"
            )
        }

    def append_concept_verdict(
        self, concept_id: str, verdict: str, *, answered_through: str, created_by: str
    ) -> str:
        return self.db.insert(
            "concept_verdicts",
            standard_fields("cvr", created_by=created_by)
            | {
                "concept_id": concept_id,
                "verdict": verdict,
                "source": answered_through,
                "applied_at": now_iso(),
            },
        )

    def current_concept_verdicts(self) -> dict[str, str]:
        """Concept id -> the verdict in force."""
        return {
            row["concept_id"]: row["verdict"]
            for row in self.db.all("SELECT concept_id, verdict FROM vw_current_concept_verdicts")
        }

    # ---- moments (corpuslm.moments) ------------------------------------------------------

    @staticmethod
    def moment_subject(source_id: str, first_position: int, last_position: int) -> str:
        """The hash that names a moment across rebuilds: its source and span."""
        return canonical_sha256([source_id, int(first_position), int(last_position)])

    def add_moment(
        self,
        source_id: str,
        first_position: int,
        last_position: int,
        insight: str,
        *,
        origin: str,
        build_key: str = "",
        created_by: str,
    ) -> str | None:
        """A moment row; None when the source already has one for this span."""
        subject = self.moment_subject(source_id, first_position, last_position)
        if self.db.one(
            "SELECT 1 FROM moments WHERE source_id = ? AND subject_sha256 = ?",
            (source_id, subject),
        ):
            return None
        return self.db.insert(
            "moments",
            standard_fields("mom", created_by=created_by)
            | {
                "source_id": source_id,
                "first_position": int(first_position),
                "last_position": int(last_position),
                "insight": insight,
                "origin": origin,
                "subject_sha256": subject,
                "build_key": build_key,
            },
        )

    def replace_model_moments(
        self, source_id: str, moments: Iterable[dict], *, build_key: str, created_by: str
    ) -> int:
        """The model's moments of one source, rebuilt in one transaction; the imported ones and
        every moment the owner has ruled on (approved or declined) are left alone. Each moment is
        `{"first", "last", "insight"}`. Returns how many were written (a span already held is
        left out)."""
        written = 0
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM moments WHERE source_id = ? AND origin = 'model' "
                "AND subject_sha256 NOT IN (SELECT subject_sha256 FROM moment_verdicts "
                "WHERE source_id = ?)",
                (source_id, source_id),
            )
            for moment in moments:
                made = self.add_moment(
                    source_id,
                    moment["first"],
                    moment["last"],
                    moment["insight"],
                    origin="model",
                    build_key=build_key,
                    created_by=created_by,
                )
                written += made is not None
        return written

    def moments(self, source_id: str | None = None, *, origin: str | None = None) -> list[dict]:
        """Moments in order of source and span."""
        clauses, args = [], []
        if source_id is not None:
            clauses.append("source_id = ?")
            args.append(source_id)
        if origin is not None:
            clauses.append("origin = ?")
            args.append(origin)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return [
            dict(row)
            for row in self.db.all(
                f"SELECT * FROM moments {where} ORDER BY source_id, first_position, last_position",
                args,
            )
        ]

    def moment(self, moment_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM moments WHERE id = ?", (moment_id,))
        return None if row is None else dict(row)

    def moment_count(self, source_id: str | None = None, *, origin: str | None = None) -> int:
        return len(self.moments(source_id, origin=origin))

    def moment_claims(self, moment: dict) -> list[dict]:
        """The claims whose first cited segment lies in the moment's span."""
        return [
            claim
            for claim in self.claims(moment["source_id"])
            if claim["segments"]
            and moment["first_position"] <= min(claim["segments"]) <= moment["last_position"]
        ]

    def moment_concept_keys(self, moment: dict) -> list[str]:
        """The concepts the moment's claims use, most frequent first."""
        counts: dict[str, int] = {}
        for claim in self.moment_claims(moment):
            for key in self.claim_concept_keys(claim["id"]):
                counts[key] = counts.get(key, 0) + 1
        return [key for key, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]

    def append_moment_verdict(
        self,
        source_id: str,
        subject_sha256: str,
        verdict: str,
        *,
        answered_through: str,
        created_by: str,
    ) -> str:
        return self.db.insert(
            "moment_verdicts",
            standard_fields("mvr", created_by=created_by)
            | {
                "source_id": source_id,
                "subject_sha256": subject_sha256,
                "verdict": verdict,
                "source": answered_through,
                "applied_at": now_iso(),
            },
        )

    def current_moment_verdicts(self, source_id: str | None = None) -> dict[str, str]:
        """Subject hash -> the verdict in force (one source, or every source)."""
        if source_id is None:
            rows = self.db.all("SELECT subject_sha256, verdict FROM vw_current_moment_verdicts")
        else:
            rows = self.db.all(
                "SELECT subject_sha256, verdict FROM vw_current_moment_verdicts "
                "WHERE source_id = ?",
                (source_id,),
            )
        return {row["subject_sha256"]: row["verdict"] for row in rows}

    # ---- line speakers (corpuslm.line_speakers) -------------------------------------------

    def replace_line_speakers(
        self, source_id: str, picks: Iterable[dict], *, build_key: str, created_by: str
    ) -> int:
        """The model's pick per line of one source, rebuilt whole in one transaction. Each pick
        is `{"position", "name", "confidence"}`. The owner's picks are a separate table and are
        never touched here. Returns how many were written."""
        written = 0
        with self.db.transaction():
            self.db.execute("DELETE FROM line_speakers WHERE source_id = ?", (source_id,))
            for pick in picks:
                self.db.insert(
                    "line_speakers",
                    standard_fields("lsp", created_by=created_by)
                    | {
                        "source_id": source_id,
                        "position": int(pick["position"]),
                        "name": str(pick["name"]),
                        "confidence": float(pick["confidence"]),
                        "build_key": build_key,
                    },
                )
                written += 1
        return written

    def line_speakers(self, source_id: str) -> dict[int, dict]:
        """Position -> the model's `{"name", "confidence"}` for one source."""
        return {
            row["position"]: {"name": row["name"], "confidence": row["confidence"]}
            for row in self.db.all(
                "SELECT position, name, confidence FROM line_speakers WHERE source_id = ? "
                "ORDER BY position",
                (source_id,),
            )
        }

    def append_line_speaker_verdict(
        self,
        source_id: str,
        position: int,
        name: str,
        *,
        answered_through: str,
        created_by: str,
    ) -> str:
        return self.db.insert(
            "line_speaker_verdicts",
            standard_fields("lsv", created_by=created_by)
            | {
                "source_id": source_id,
                "position": int(position),
                "name": name,
                "source": answered_through,
                "applied_at": now_iso(),
            },
        )

    def current_line_speaker_verdicts(self, source_id: str) -> dict[int, str]:
        """Position -> the owner's pick in force for one source."""
        return {
            row["position"]: row["name"]
            for row in self.db.all(
                "SELECT position, name FROM vw_current_line_speaker_verdicts WHERE source_id = ? "
                "ORDER BY position",
                (source_id,),
            )
        }

    # ---- bodies of work (corpuslm.bodies) ------------------------------------------------

    def body(self, name: str) -> dict | None:
        row = self.db.one("SELECT * FROM bodies_of_work WHERE name = ?", (name,))
        return None if row is None else dict(row)

    def bodies(self) -> list[dict]:
        """Every body of work, most recently used first, then by name."""
        return [
            dict(row)
            for row in self.db.all("SELECT * FROM bodies_of_work ORDER BY last_used_at DESC, name")
        ]

    def add_body(
        self,
        name: str,
        kind: str,
        *,
        text_source_id: str | None = None,
        corpus_id: str | None = None,
        source_id: str | None = None,
        note: str = "",
        manuscript: str = "",
        directory: str = "",
        created_by: str,
    ) -> str:
        return self.db.insert(
            "bodies_of_work",
            standard_fields("bow", created_by=created_by)
            | {
                "name": name,
                "kind": kind,
                "text_source_id": text_source_id,
                "corpus_id": corpus_id,
                "source_id": source_id,
                "note": note,
                "manuscript": manuscript,
                "directory": directory,
            },
        )

    def update_body(self, name: str, changes: dict[str, Any]) -> None:
        row = self.body(name)
        if row is None:
            raise KeyError(f"no body of work named {name!r}")
        self.db.update("bodies_of_work", row["id"], changes)

    # ---- monologues (corpuslm.monologues) ------------------------------------------------

    def monologue(self, name: str) -> dict | None:
        row = self.db.one("SELECT * FROM monologues WHERE name = ?", (name,))
        return None if row is None else dict(row)

    def monologues(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.all("SELECT * FROM monologues ORDER BY created_at DESC, name")
        ]

    def add_monologue(
        self,
        name: str,
        *,
        title: str = "",
        brief: str = "",
        manuscript: str = "",
        directory: str = "",
        created_by: str,
    ) -> str:
        return self.db.insert(
            "monologues",
            standard_fields("mon", created_by=created_by)
            | {
                "name": name,
                "title": title,
                "brief": brief,
                "manuscript": manuscript,
                "directory": directory,
            },
        )

    def update_monologue(self, name: str, changes: dict[str, Any]) -> None:
        row = self.monologue(name)
        if row is None:
            raise KeyError(f"no monologue named {name!r}")
        self.db.update("monologues", row["id"], changes)

    def attach_moment(self, monologue_id: str, source_id: str, subject_sha256: str) -> bool:
        """False when the moment is attached already."""
        if self.db.one(
            "SELECT 1 FROM monologue_moments WHERE monologue_id = ? AND source_id = ? "
            "AND subject_sha256 = ?",
            (monologue_id, source_id, subject_sha256),
        ):
            return False
        self.db.execute(
            "INSERT INTO monologue_moments (monologue_id, source_id, subject_sha256) "
            "VALUES (?, ?, ?)",
            (monologue_id, source_id, subject_sha256),
        )
        return True

    def detach_moment(self, monologue_id: str, source_id: str, subject_sha256: str) -> bool:
        return (
            self.db.execute(
                "DELETE FROM monologue_moments WHERE monologue_id = ? AND source_id = ? "
                "AND subject_sha256 = ?",
                (monologue_id, source_id, subject_sha256),
            ).rowcount
            > 0
        )

    def monologue_moments(self, monologue_id: str) -> list[dict]:
        """The attached moments' rows (by their subject), in source and span order; an attached
        subject with no moment row any more is left out."""
        rows = self.db.all(
            "SELECT m.* FROM monologue_moments a JOIN moments m ON m.source_id = a.source_id "
            "AND m.subject_sha256 = a.subject_sha256 WHERE a.monologue_id = ? "
            "ORDER BY m.source_id, m.first_position, m.last_position",
            (monologue_id,),
        )
        return [dict(row) for row in rows]

    def attach_body(self, monologue_id: str, body_id: str) -> bool:
        if self.db.one(
            "SELECT 1 FROM monologue_bodies WHERE monologue_id = ? AND body_id = ?",
            (monologue_id, body_id),
        ):
            return False
        self.db.execute(
            "INSERT INTO monologue_bodies (monologue_id, body_id) VALUES (?, ?)",
            (monologue_id, body_id),
        )
        return True

    def detach_body(self, monologue_id: str, body_id: str) -> bool:
        return (
            self.db.execute(
                "DELETE FROM monologue_bodies WHERE monologue_id = ? AND body_id = ?",
                (monologue_id, body_id),
            ).rowcount
            > 0
        )

    def monologue_bodies(self, monologue_id: str) -> list[dict]:
        rows = self.db.all(
            "SELECT b.* FROM monologue_bodies a JOIN bodies_of_work b ON b.id = a.body_id "
            "WHERE a.monologue_id = ? "
            "ORDER BY CASE b.kind WHEN 'standing' THEN 0 ELSE 1 END, b.name",
            (monologue_id,),
        )
        return [dict(row) for row in rows]

    def body_use_counts(self) -> dict[str, int]:
        """Body id -> how many monologues drew on it."""
        return {
            row["body_id"]: row["n"]
            for row in self.db.all(
                "SELECT body_id, COUNT(*) AS n FROM monologue_bodies GROUP BY body_id"
            )
        }

    def add_monologue_run(
        self, monologue_id: str, fields: dict[str, Any], *, created_by: str
    ) -> str:
        number = self._row(
            "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM monologue_runs WHERE monologue_id = ?",
            (monologue_id,),
        )["n"]
        return self.db.insert(
            "monologue_runs",
            standard_fields("run", created_by=created_by)
            | {"monologue_id": monologue_id, "number": number}
            | fields,
        )

    def monologue_runs(self, monologue_id: str) -> list[dict]:
        """Every run, oldest first."""
        return [
            dict(row)
            for row in self.db.all(
                "SELECT * FROM monologue_runs WHERE monologue_id = ? ORDER BY number",
                (monologue_id,),
            )
        ]

    def monologue_run_by_ingredients(
        self, monologue_id: str, ingredients_sha256: str
    ) -> dict | None:
        row = self.db.one(
            "SELECT * FROM monologue_runs WHERE monologue_id = ? AND ingredients_sha256 = ?",
            (monologue_id, ingredients_sha256),
        )
        return None if row is None else dict(row)

    def sources_sharing_concepts(
        self, keys: Sequence[str], *, kind: str | None = None
    ) -> list[dict]:
        """Sources (of `kind` when given) whose claims carry any of the concepts `keys`, most
        shared first: source id, title, published_at, and the keys shared."""
        if not keys:
            return []
        marks = ", ".join("?" for _ in keys)
        rows = self.db.all(
            "SELECT c.source_id AS source_id, o.title AS title, o.published_at AS published_at, "
            "json_group_array(DISTINCT p.key) AS concepts "
            "FROM claim_concepts t JOIN concepts p ON p.id = t.concept_id "
            "JOIN claims c ON c.id = t.claim_id JOIN sources o ON o.id = c.source_id "
            f"WHERE p.key IN ({marks}) AND o.status <> 'ignored' "
            + ("AND o.kind = ? " if kind else "")
            + "GROUP BY c.source_id ORDER BY COUNT(DISTINCT p.key) DESC, o.published_at DESC",
            [*keys] + ([kind] if kind else []),
        )
        return [dict(row) | {"concepts": loads(row["concepts"], [])} for row in rows]

    # ---- segments and search (derived index) ---------------------------------------

    def replace_segments(self, source_id: str, segments: Iterable[dict]) -> None:
        """The FTS rows of one source, rebuilt whole. Transcript segments keep
        their JSON names (idx, t0, t1); the columns are position,
        start_seconds and end_seconds."""
        with self.db.transaction():
            self.db.execute("DELETE FROM segments WHERE source_id = ?", (source_id,))
            for segment in segments:
                self.db.execute(
                    "INSERT INTO segments(source_id, position, start_seconds, end_seconds, "
                    "speaker, text) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        source_id,
                        segment["idx"],
                        segment["t0"],
                        segment["t1"],
                        segment.get("speaker", ""),
                        segment["text"],
                    ),
                )

    def segments(self, source_id: str) -> list[dict]:
        """One source's segments in order, with the JSON names `replace_segments`
        takes (idx, t0, t1, speaker, text)."""
        return [
            {
                "idx": row["position"],
                "t0": row["start_seconds"],
                "t1": row["end_seconds"],
                "speaker": row["speaker"],
                "text": row["text"],
            }
            for row in self.db.execute(
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
        return [dict(row) for row in self.db.all(sql, args)]

    # ---- cleaned segments and their search (derived index of a kind whose raw lines stay) ----

    def replace_cleaned_segments(self, source_id: str, segments: Iterable[dict]) -> None:
        """The cleaned rows of one source, rebuilt whole in one transaction. Same shape as
        `replace_segments` (JSON names idx, t0, t1); the raw `segments` are never touched."""
        with self.db.transaction():
            self.db.execute("DELETE FROM cleaned_segments WHERE source_id = ?", (source_id,))
            for segment in segments:
                self.db.execute(
                    "INSERT INTO cleaned_segments(source_id, position, start_seconds, "
                    "end_seconds, speaker, text) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        source_id,
                        segment["idx"],
                        segment["t0"],
                        segment["t1"],
                        segment.get("speaker", ""),
                        segment["text"],
                    ),
                )

    def delete_cleaned_segments(self, source_id: str) -> int:
        """Remove one source's cleaned rows (the triggers remove their FTS5 rows)."""
        return self.db.execute(
            "DELETE FROM cleaned_segments WHERE source_id = ?", (source_id,)
        ).rowcount

    def cleaned_segment_count(self, source_id: str | None = None) -> int:
        """Cleaned rows of one source, or of all."""
        if source_id is None:
            return self._row("SELECT COUNT(*) AS n FROM cleaned_segments")["n"]
        return self._row(
            "SELECT COUNT(*) AS n FROM cleaned_segments WHERE source_id = ?", (source_id,)
        )["n"]

    def cleaned_source_ids(self) -> list[str]:
        """The sources that have cleaned rows, in id order."""
        return [
            row["source_id"]
            for row in self.db.all(
                "SELECT DISTINCT source_id FROM cleaned_segments ORDER BY source_id"
            )
        ]

    def search_cleaned(self, match: str, *, before: str | None = None, limit: int) -> list[dict]:
        """FTS5 hits over the cleaned segments, best first, as REFERENCES only: the source's id,
        title and date, and the second the segment starts at. The segment's text is never
        returned. `before` ('YYYY-MM-DD') keeps sources recorded on an earlier day; a source with
        no date is left out. Raises sqlite3.OperationalError when `match` is not valid FTS5."""
        sql = (
            "SELECT o.id AS source_id, o.title AS title, o.published_at AS published_at, "
            "c.start_seconds AS start_seconds, bm25(cleaned_segments_fts) AS rank "
            "FROM cleaned_segments_fts JOIN cleaned_segments c "
            "ON c.rowid = cleaned_segments_fts.rowid JOIN sources o ON o.id = c.source_id "
            "WHERE cleaned_segments_fts MATCH ? AND o.status <> 'ignored' "
            + ("AND substr(o.published_at, 1, 10) < ? " if before else "")
            + "ORDER BY rank, o.id, c.position LIMIT ?"
        )
        args = [match] + ([before] if before else []) + [limit]
        return [dict(row) for row in self.db.all(sql, args)]

    # ---- locks (coordination) ---------------------------------------------------

    def lock_holder(self, locked_id: str) -> dict | None:
        row = self.db.one("SELECT holder, expires_at FROM locks WHERE owner = ?", (locked_id,))
        return None if row is None else dict(row)

    def take_lock(self, locked_id: str, holder: str, *, acquired_at: str, expires_at: str) -> None:
        self.db.execute(
            "INSERT INTO locks(owner, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner) DO UPDATE SET holder = excluded.holder, "
            "acquired_at = excluded.acquired_at, expires_at = excluded.expires_at",
            (locked_id, holder, acquired_at, expires_at),
        )

    def refresh_locks(self, holder: str, *, expires_at: str) -> int:
        """The heartbeat: push out the expiry of every lock `holder` holds."""
        cursor = self.db.execute(
            "UPDATE locks SET expires_at = ? WHERE holder = ?", (expires_at, holder)
        )
        return cursor.rowcount

    def release_lock(self, locked_id: str, holder: str) -> None:
        self.db.execute("DELETE FROM locks WHERE owner = ? AND holder = ?", (locked_id, holder))

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
        answered_through: str,
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
        row = standard_fields("ver", created_by=created_by) | {
            "source_id": source_id,
            "key": key,
            "kind": kind,
            "verdict": verdict,
            "span": span,
            "role": role,
            "source": answered_through,
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
            row["key"]: dict(row)
            for row in self.db.all(
                "SELECT * FROM vw_current_review_verdicts WHERE source_id = ? ORDER BY key",
                (source_id,),
            )
        }

    def review_verdict_history(self, source_id: str, key: str) -> list[dict]:
        """Every verdict ever given for one slot, oldest first."""
        return [
            dict(row)
            for row in self.db.all(
                "SELECT * FROM review_verdicts WHERE source_id = ? AND key = ? ORDER BY rowid",
                (source_id, key),
            )
        ]

    def append_speaker_verdict(
        self, *, source_id: str, role: str, name: str, created_by: str
    ) -> str:
        return self.db.insert(
            "speaker_verdicts",
            standard_fields("spk", created_by=created_by)
            | {"source_id": source_id, "role": role, "name": name},
        )

    def current_speaker_names(self, source_id: str) -> dict[str, str]:
        """role → the owner's name in force for that role."""
        return {
            row["role"]: row["name"]
            for row in self.db.all(
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
            standard_fields("crr", created_by=created_by)
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
        return {
            row["span_norm"]: dict(row) for row in self.db.all(sql + " ORDER BY span_norm", args)
        }

    def correction_count(self) -> int:
        """Rules in force (distinct normalized spans)."""
        return self._row("SELECT COUNT(*) AS n FROM vw_current_corrections")["n"]

    # ---- runs ----------------------------------------------------------------

    def open_run(self, scope: str, *, created_by: str) -> str:
        return self.db.insert(
            "runs",
            standard_fields("run", created_by=created_by)
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
            dict(row)
            for row in self.db.all(
                "SELECT * FROM review_answers WHERE corpus_id = ? ORDER BY card_id", (corpus_id,)
            )
        ]

    def review_answer_counts(self) -> dict[str, int]:
        """corpus id -> how many answers wait to land, for the corpora that have any."""
        return {
            row["corpus_id"]: row["n"]
            for row in self.db.all(
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
                    standard_fields("ans", created_by=created_by)
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

    # ---- model suggestions (derived state: the AI cleanup's top suggestions) -------

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
                "candidates": [candidate_row["candidate"] for candidate_row in candidates],
                "probabilities": [candidate_row["probability"] for candidate_row in candidates],
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
        from the AI cleanup artifact with `artifact_sha256`. A rebuilt artifact replaces the
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
                fields = standard_fields("sug", created_by=created_by)
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
                        standard_fields("sgc", created_by=created_by)
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
        return {column: row[column] for column in ("sources", "suggestions", "with_probability")}

    # ---- change log (history of master data) ----------------------------------------

    def changes_of(self, table: str, row_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.all(
                "SELECT column_name, old_value, new_value, row_version, created_at "
                "FROM change_log WHERE table_name = ? AND row_id = ? ORDER BY rowid",
                (table, row_id),
            )
        ]


def metadata_of(row: dict) -> dict:
    """A row's `metadata` JSON object (fails loudly when it does not parse)."""
    return loads(row.get("metadata"), {})
