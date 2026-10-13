"""Review server: serves the review page on this machine and keeps its answers in the database.

The review page (corpuslm.review_page) saves every tap through a small database interface that
claude.ai once gave a published artifact. This module is the page's home now: a small
standard-library HTTP server on the loopback address that answers the same calls (get, set, list,
delete of the answers under `reviews/<corpus id>/items/<card id>`) from the `review_answers` table
of the shared transcript database, through the Store. The page script picks it when `window.claude`
is absent and a server answers; `corpuslm.review_apply` lands the saved answers and deletes them.

Routes: `GET /` the page (built per request by the function the caller passes in), `GET /health`
(with the time this server started, for `review check`), any other page the caller passes in
`pages` (`/concepts`, corpuslm.concepts_page, built per request too), and one `POST /api/review`
with `{"method": ..., "params": {...}}` dispatched to `Answers`, whose `concept_verdict` method
appends a concept ruling. The pages' scripts (never a second copy of a page) are the only clients.

Never imports: cli, clikit, any model transport.

Decisions:
- The server binds the loopback address only and answers only Host headers that name this machine
  (loopback or `*.ts.net`), else 421. Enforced by:
  tests/test_review_server.py::test_a_host_that_is_not_this_machine_gets_421,
  tests/test_review_server.py::test_the_server_will_not_bind_a_public_address.
- Every POST passes one write check: Content-Type application/json (415), header `X-Review`
  (403), Origin, when sent, equal to Host (403), body at most 64 KB (413), a JSON object (400).
  There is no CORS, no cookie and no answer to OPTIONS. Enforced by:
  tests/test_review_server.py::test_the_write_check_declines_each_bad_request.
- Answers are stored through the Store, one short transaction per call on a connection of its
  own, under one lock, so two taps at once both land. Enforced by:
  tests/test_review_server.py::test_two_taps_at_once_both_land.
- An error answers with the message for a bad request, a bare 404 for a missing document, and
  only the exception's type for anything else; no stored text is in an error. Enforced by:
  tests/test_review_server.py::test_an_unexpected_error_names_only_its_type.
- One server and one answers table serve every source kind's corpora; the page it builds names
  them all. Enforced by: tests/test_review_server.py::test_answers_of_every_corpus_share_one_table.

Terms:
- **answers**: what the page saved and `review apply` has not landed: one `review_answers` row per
  (corpus, card), shown to the page as the document `{verdict, at}` at
  `reviews/<corpus id>/items/<card id>`.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import clock
from .db import Database, now_iso
from .review_page import decisions_document
from .store import Store

APP = "review"
CREATED_BY = "review page"
MAX_BODY_BYTES = 64 * 1024
MAX_VERDICT_CHARS = 2000
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_WORD = r"[A-Za-z0-9_.:-]+"
_ANSWER_PATH = re.compile(rf"^reviews/(?P<corpus>{_WORD})/items/(?P<card>{_WORD})$")
_COLLECTION = re.compile(rf"^reviews/(?P<corpus>{_WORD})/items$")
_BAD_PATH = "a path is reviews/<corpus id>/items/<card id>"


class Answers:
    """The page's database: the saved answers of every corpus, in the shared transcript database.
    Each call opens its own connection and writes under a lock, so a call never sees another's
    half-written state."""

    def __init__(self, database: Path):
        self.database = Path(database)
        self._lock = threading.Lock()

    def _store(self) -> tuple[Database, Store]:
        db = Database(self.database)
        return db, Store(db)

    @staticmethod
    def _answer_path(path: object) -> tuple[str, str]:
        match = _ANSWER_PATH.match(path) if isinstance(path, str) else None
        if match is None:
            raise ValueError(_BAD_PATH)
        return match["corpus"], match["card"]

    @staticmethod
    def _collection(collection: object) -> str:
        match = _COLLECTION.match(collection) if isinstance(collection, str) else None
        if match is None:
            raise ValueError("a collection is reviews/<corpus id>/items")
        return match["corpus"]

    @staticmethod
    def _document(row: dict) -> dict:
        return {"verdict": row["verdict"], "at": row["answered_at"]}

    def get(self, path: object) -> dict | None:
        corpus_id, card_id = self._answer_path(path)
        db, store = self._store()
        try:
            row = store.review_answer(corpus_id, card_id)
        finally:
            db.close()
        return None if row is None else self._document(row)

    def set(self, path: object, data: object) -> None:
        corpus_id, card_id = self._answer_path(path)
        if not isinstance(data, dict):
            raise ValueError("a document is a JSON object")
        verdict = data.get("verdict", "")
        stamp = data.get("at") or now_iso()
        if not isinstance(verdict, str) or len(verdict) > MAX_VERDICT_CHARS:
            raise ValueError(f"verdict is text of at most {MAX_VERDICT_CHARS} characters")
        if not isinstance(stamp, str) or not _is_time(stamp):
            raise ValueError("at is a time like 2026-10-09T10:00:00Z")
        with self._lock:
            db, store = self._store()
            try:
                store.save_review_answer(corpus_id, card_id, verdict, stamp, created_by=CREATED_BY)
            finally:
                db.close()

    def delete(self, path: object) -> bool:
        corpus_id, card_id = self._answer_path(path)
        with self._lock:
            db, store = self._store()
            try:
                return store.delete_review_answer(corpus_id, card_id)
            finally:
                db.close()

    def list(self, collection: object) -> list[dict]:
        """The documents under `collection`: `[{"id", "data"}]`, by card id."""
        corpus_id = self._collection(collection)
        db, store = self._store()
        try:
            rows = store.review_answers(corpus_id)
        finally:
            db.close()
        return [{"id": row["card_id"], "data": self._document(row)} for row in rows]

    def decisions(self, corpus_id: str) -> dict:
        """The decisions document of one corpus (`decisions_to_verdicts`' input)."""
        db, store = self._store()
        try:
            return decisions_document(store, corpus_id)
        finally:
            db.close()

    def concept_verdict(self, key: object, verdict: object) -> dict:
        """Append the owner's ruling on a boundary concept (corpuslm.concepts)."""
        from . import concepts  # the server knows the lexicon only for this one call

        if not isinstance(key, str) or not key.strip():
            raise ValueError("key is the concept's label or key")
        if verdict not in ("in", "out"):
            raise ValueError("verdict is in or out")
        with self._lock:
            db, store = self._store()
            try:
                return concepts.verdict(
                    store, key, verdict, answered_through="phone page", created_by=CREATED_BY
                )
            finally:
                db.close()

    def call(self, method: object, params: object) -> object:
        """Dispatch one API call. ValueError for a bad call, KeyError for a missing document."""
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        if method == "health":
            return {"status": "ok"}
        if method == "concept_verdict":
            return self.concept_verdict(params.get("key"), params.get("verdict"))
        if method == "get":
            found = self.get(params.get("path"))
            if found is None:
                raise KeyError("no such document")
            return found
        if method == "set":
            self.set(params.get("path"), params.get("data"))
            return {"saved": True}
        if method == "delete":
            return {"deleted": self.delete(params.get("path"))}
        if method == "list":
            return self.list(params.get("collection"))
        raise ValueError("method must be one of: health, get, set, delete, list, concept_verdict")


def _is_time(text: str) -> bool:
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _host_is_this_machine(host_header: str) -> bool:
    name = host_header.rsplit(":", 1)[0] if not host_header.endswith("]") else host_header
    if host_header.startswith("[") and "]" in host_header:
        name = host_header[: host_header.index("]") + 1]
    return name in LOOPBACK_HOSTS or name.endswith(".ts.net")


def make_server(
    host: str,
    port: int,
    *,
    page: Callable[[], str],
    answers: Answers,
    pages: Mapping[str, Callable[[], str]] | None = None,
) -> ThreadingHTTPServer:
    """A server for `page` (called for every `GET /`) and each of `pages` (path -> builder, called
    for every GET of that path) over `answers`. Loopback addresses only."""
    extra = dict(pages or {})
    started_at = clock.iso(clock.utc_now())
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(f"the review server binds the loopback address only, not {host!r}")

    class Handler(BaseHTTPRequestHandler):
        server_version = "corpuslm-review"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # no request line (it could carry a path) is written anywhere

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: object) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

        def _this_machine(self) -> bool:
            if _host_is_this_machine(self.headers.get("Host", "")):
                return True
            self._json(421, {"error": "this server answers requests for this machine only"})
            return False

        def do_GET(self) -> None:  # noqa: N802
            if not self._this_machine():
                return
            if self.path == "/health":
                self._json(200, {"status": "ok", "started_at": started_at})
            elif self.path in ("/", "/index.html"):
                html = page()
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path in extra:
                self._send(200, extra[self.path]().encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._this_machine():
                return
            if self.path != f"/api/{APP}":
                self._json(404, {"error": "not found"})
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._json(415, {"error": "Content-Type must be application/json"})
                return
            if self.headers.get(f"X-{APP.capitalize()}") is None:
                self._json(403, {"error": f"the X-{APP.capitalize()} header is missing"})
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin.split("://", 1)[-1] != self.headers.get("Host"):
                self._json(403, {"error": "Origin does not match Host"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0:
                self._json(400, {"error": "Content-Length is required"})
                return
            if length > MAX_BODY_BYTES:
                self._json(413, {"error": "the body is over 64 KB"})
                return
            try:
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("the body must be a JSON object")
                result = answers.call(body.get("method"), body.get("params", {}))
            except (ValueError, UnicodeDecodeError) as error:
                self._json(400, {"error": str(error)})
            except KeyError:
                self._json(404, {"error": "no such document"})
            except Exception as error:  # noqa: BLE001 - the type alone is answered
                self._json(500, {"error": type(error).__name__})
            else:
                self._json(200, {"result": result})

    class Server(ThreadingHTTPServer):
        request_queue_size = 64  # a burst of taps queues instead of being reset
        daemon_threads = True

    return Server((host, port), Handler)


__all__ = ["APP", "Answers", "MAX_BODY_BYTES", "make_server"]
