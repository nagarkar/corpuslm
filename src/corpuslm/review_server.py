"""Review server: serves the review page on this machine and keeps its answers in the database.

The review page (corpuslm.review_page) saves every tap through a small database interface that
claude.ai once gave a published artifact. This module is the page's home now: a small
standard-library HTTP server on the loopback address that answers the same calls (get, set, list,
delete of the answers under `reviews/<corpus id>/items/<card id>`) from the `review_answers` table
of the shared transcript database, through the Store. The page script picks it when `window.claude`
is absent and a server answers; `corpuslm.review_apply` lands the saved answers and deletes them.

Routes: `GET /` the page (built per request by the function the caller passes in), `GET /health`
(with the time this server started, for `review check`), any other page the caller passes in
`pages` (`/concepts`, corpuslm.concepts_page; `/moments`, corpuslm.moments_page; built per
request too), any page under a prefix in `page_prefixes` (`/teleprompter/<monologue>`,
corpuslm.teleprompter_page), and one `POST /api/review` with `{"method": ..., "params": {...}}`
dispatched to `Answers`, whose `concept_verdict`, `moment_verdict`, `directive_verdict` and
`form_verdict` methods record a ruling (`/directives`, corpuslm.directives_page).
The pages' scripts (never a second copy of a page) are the only clients.

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

    def __init__(self, database: Path, settings=None):
        self.database = Path(database)
        self._lock = threading.Lock()
        # the review settings (corpuslm.review_settings), for the monologue calls that need a
        # model client and the document store; None leaves those calls declined
        self.settings = settings

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

    def moment_verdict(self, moment_id: object, verdict: object) -> dict:
        """Append the owner's ruling on a moment (corpuslm.moments)."""
        from . import moments  # the server knows moments only for this one call

        if not isinstance(moment_id, str) or not moment_id.strip():
            raise ValueError("id is the moment's id")
        if verdict not in ("approved", "declined"):
            raise ValueError("verdict is approved or declined")
        with self._lock:
            db, store = self._store()
            try:
                return moments.verdict(
                    store, moment_id, verdict, answered_through="phone page", created_by=CREATED_BY
                )
            finally:
                db.close()

    def moment_edit(self, moment_id: object, insight: object) -> dict:
        """The owner's wording of a moment's insight (corpuslm.moments.edit)."""
        from . import moments

        if not isinstance(moment_id, str) or not moment_id.strip():
            raise ValueError("id is the moment's id")
        if not isinstance(insight, str):
            raise ValueError("insight is text")
        with self._lock:
            db, store = self._store()
            try:
                return moments.edit(
                    store, moment_id, insight, answered_through="phone page", created_by=CREATED_BY
                )
            finally:
                db.close()

    def moment_verdicts(self, moment_ids: object, verdict: object) -> dict:
        """Append one ruling on several moments at once (a source's Approve all or Decline
        all), in one transaction: all land or none."""
        from . import moments

        if not isinstance(moment_ids, list) or not moment_ids or len(moment_ids) > 500:
            raise ValueError("ids is a list of moment ids, at most 500")
        if not all(isinstance(moment_id, str) and moment_id for moment_id in moment_ids):
            raise ValueError("ids is a list of moment ids")
        if verdict not in ("approved", "declined"):
            raise ValueError("verdict is approved or declined")
        with self._lock:
            db, store = self._store()
            try:
                with store.transaction():
                    for moment_id in moment_ids:
                        moments.verdict(
                            store, moment_id, verdict, answered_through="phone page",
                            created_by=CREATED_BY,
                        )  # fmt: skip
                return {"ruled": len(moment_ids), "verdict": verdict}
            finally:
                db.close()

    def directive_verdict(self, claim_id: object, verdict: object) -> dict:
        """Append the owner's ruling on a directive (corpuslm.directives)."""
        from . import directives  # the server knows directives only for this one call

        if not isinstance(claim_id, str) or not claim_id.strip():
            raise ValueError("id is the directive's claim id")
        if verdict not in ("confirm", "decline"):
            raise ValueError("verdict is confirm or decline")
        with self._lock:
            db, store = self._store()
            try:
                return directives.verdict(
                    store, claim_id, verdict, answered_through="phone page", created_by=CREATED_BY
                )
            finally:
                db.close()

    def form_verdict(self, source_id: object, label: object) -> dict:
        """The owner's ruling on a source's form (corpuslm.form)."""
        from . import form  # the server knows forms only for this one call

        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source is the source's id")
        if label not in form.LABELS:
            raise ValueError(f"label is one of {', '.join(form.LABELS)}")
        with self._lock:
            db, store = self._store()
            try:
                if not store.has_source(source_id):
                    raise KeyError(f"no source {source_id!r}")
                record = form.rule(store, source_id, label, created_by=CREATED_BY)
                return {"source": source_id, "label": record["label"]}
            finally:
                db.close()

    def line_speaker(self, params: dict) -> dict:
        """The owner's pick for who spoke one line, from the moments page
        (corpuslm.line_speakers)."""
        from . import line_speakers

        source, position, name = params.get("source"), params.get("position"), params.get("name")
        if not isinstance(source, str) or not source:
            raise ValueError("source is the source's id")
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("position is the line's position")
        if not isinstance(name, str) or not name:
            raise ValueError("name is who spoke the line")
        if self.settings is None or self.settings.line_speakers is None:
            raise ValueError("[line_speakers] is not in the settings; the page cannot name lines")
        found = line_speakers.settings(self.settings.client_config())
        with self._lock:
            db, store = self._store()
            try:
                return line_speakers.pick_line(
                    store, found, source, position, name, created_by=CREATED_BY
                )
            finally:
                db.close()

    # -- the monologue pages' calls (corpuslm.monologue_page) --

    def monologue_search(self, params: dict) -> dict:
        """Search all moments or all videos for the monologue page; returns the cards' HTML."""
        from . import monologue_page, monologues

        kind, query = params.get("kind"), params.get("query")
        if kind not in ("moments", "videos"):
            raise ValueError("kind is moments or videos")
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("query is text")
        name = self._monologue_name(params)
        with self._lock:
            db, store = self._store()
            try:
                found = monologues.search(store, name, kind, query)
            finally:
                db.close()
        return {
            "found": found.get("found", 0),
            "html": monologue_page.render_results(kind, found["items"], found.get("found", 0)),
        }

    def _monologue_name(self, params: dict) -> str:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is the monologue's name")
        return name

    def monologue_new(self, params: dict) -> dict:
        from . import monologues

        brief = params.get("brief", "")
        if not isinstance(brief, str) or len(brief) > MAX_VERDICT_CHARS * 10:
            raise ValueError("brief is text")
        series = params.get("series")
        if not isinstance(series, str) or not series.strip():
            raise ValueError("series is the series manuscript's name (e.g. SpiritualHindu)")
        doc, workspace = self._documents()
        with self._lock:
            db, store = self._store()
            try:
                return monologues.create(
                    store, doc, self._monologue_name(params), series=series,
                    workspace=workspace, brief=brief, created_by=CREATED_BY,
                )  # fmt: skip
            finally:
                db.close()

    def monologue_brief(self, params: dict) -> dict:
        from . import monologues

        changes = {}
        for key in ("brief", "title"):
            value = params.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > MAX_VERDICT_CHARS * 10:
                raise ValueError(f"{key} is text")
            changes[key] = value.strip()
        with self._lock:
            db, store = self._store()
            try:
                name = monologues.slug(self._monologue_name(params))
                store.update_monologue(name, changes)
                return {"name": name, "saved": sorted(changes)}
            finally:
                db.close()

    def monologue_attach(self, params: dict, *, detach: bool = False) -> dict:
        from . import monologues

        name = self._monologue_name(params)
        moment, body, source = params.get("moment"), params.get("body"), params.get("source")
        given = [value for value in (moment, body, source) if value is not None]
        if len(given) != 1 or not isinstance(given[0], str):
            raise ValueError("give one of moment, body or source")
        with self._lock:
            db, store = self._store()
            try:
                if detach:
                    if source is not None:
                        body = f"source:{source}"
                    return monologues.detach(store, name, moment_id=moment, body=body)
                if source is not None:
                    return monologues.attach_source(store, name, source, created_by=CREATED_BY)
                return monologues.attach(store, name, moment_id=moment, body=body)
            finally:
                db.close()

    def _documents(self):
        """(document store, workspace) from the settings, for a new monologue's series file."""
        from . import document

        settings = self.settings
        if settings is None or settings.claims is None:
            raise ValueError("the server was started without [claims] settings")
        config = settings.client_config()
        return (
            document.store(config, settings.claims.workspace, origin="corpuslm"),
            settings.claims.workspace,
        )

    def _monologue_tools(self):
        """(client, document store, workspace) from the settings, for estimate and run."""
        from . import document, llm, schemas

        settings = self.settings
        if settings is None or settings.claims is None or settings.monologue is None:
            raise ValueError("the server was started without [claims] and [monologue] settings")
        config = settings.client_config()
        client = llm.Client(
            settings.claims.workspace,
            config,
            verb="monologue page",
            validator=schemas.validate,
            schema_loader=schemas.schema,
        )
        doc = document.store(config, settings.claims.workspace, origin="corpuslm")
        return client, doc, settings.claims.workspace

    def monologue_estimate(self, params: dict) -> dict:
        from . import monologues

        name = self._monologue_name(params)
        client, doc, _ = self._monologue_tools()
        db, store = self._store()
        try:
            estimate = monologues.estimate(
                store, client, doc, name, fresh=bool(params.get("fresh"))
            )
        finally:
            db.close()
        return {
            "moments": estimate.moments,
            "bodies": estimate.bodies,
            "stored_run": estimate.stored,
            "tokens_in": estimate.tokens_in,
            "tokens_out_assumed": estimate.tokens_out,
            "estimated_usd": estimate.usd,
            "model": estimate.model,
        }

    def monologue_run(self, params: dict) -> dict:
        """Billed: the owner's second tap after an estimate."""
        from . import monologues

        name = self._monologue_name(params)
        client, doc, _ = self._monologue_tools()
        with self._lock:
            db, store = self._store()
            try:
                return monologues.run(
                    store, client, doc, name, fresh=bool(params.get("fresh")),
                    created_by=CREATED_BY,
                )  # fmt: skip
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
        if method == "moment_verdict":
            return self.moment_verdict(params.get("id"), params.get("verdict"))
        if method == "moment_edit":
            return self.moment_edit(params.get("id"), params.get("insight"))
        if method == "moment_verdicts":
            return self.moment_verdicts(params.get("ids"), params.get("verdict"))
        if method == "directive_verdict":
            return self.directive_verdict(params.get("id"), params.get("verdict"))
        if method == "form_verdict":
            return self.form_verdict(params.get("source"), params.get("label"))
        if method == "line_speaker":
            return self.line_speaker(params)
        if method == "monologue_search":
            return self.monologue_search(params)
        if method == "monologue_new":
            return self.monologue_new(params)
        if method == "monologue_brief":
            return self.monologue_brief(params)
        if method == "monologue_attach":
            return self.monologue_attach(params)
        if method == "monologue_detach":
            return self.monologue_attach(params, detach=True)
        if method == "monologue_estimate":
            return self.monologue_estimate(params)
        if method == "monologue_run":
            return self.monologue_run(params)
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
        raise ValueError(
            "method must be one of: health, get, set, delete, list, concept_verdict, "
            "moment_verdict, moment_verdicts, moment_edit, directive_verdict, form_verdict, "
            "line_speaker, monologue_search, "
            "monologue_new, monologue_brief, monologue_attach, monologue_detach, "
            "monologue_estimate, monologue_run"
        )


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
    page_prefixes: Mapping[str, Callable[[str], str]] | None = None,
) -> ThreadingHTTPServer:
    """A server for `page` (called for every `GET /`) and each of `pages` (path -> builder, called
    for every GET of that path) over `answers`. Loopback addresses only."""
    extra = dict(pages or {})
    prefixes = dict(page_prefixes or {})  # "/teleprompter/" -> builder(rest of the path)
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
            elif any(self.path.startswith(prefix) for prefix in prefixes):
                prefix = next(p for p in prefixes if self.path.startswith(p))
                rest = self.path[len(prefix) :]
                if not re.fullmatch(_WORD, rest):
                    self._json(404, {"error": "not found"})
                    return
                try:
                    html = prefixes[prefix](rest)
                except KeyError:
                    self._json(404, {"error": "not found"})
                    return
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
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
