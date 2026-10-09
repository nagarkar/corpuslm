"""Review server: serves the review page on this machine and keeps its answers in a file.

The review page (corpuslm.review_page) saves every tap to the database Claude gives a published
artifact. This module is the page's other home: a small standard-library HTTP server on the
loopback address that answers the same calls (get, set, list, delete of documents under the same
collection names, `reviews/<corpus id>/items/<card id>`) from one JSON file in the tool's own state
folder. The page script picks it when `window.claude` is absent and a server answers; a tool that
reads the answers back (`documents.decisions`) lands them with `decisions_to_verdicts`.

Routes: `GET /` the page (built per request by the function the tool passes in), `GET /health`,
and one `POST /api/review` with `{"method": ..., "params": {...}}` dispatched to `Documents`. The
page's script (never a second copy of the page) is the only client.

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
- The file is loaded for every request and written atomically under one lock, so two taps at once
  both land. Enforced by: tests/test_review_server.py::test_two_taps_at_once_both_land.
- An error answers with the message for a bad request, a bare 404 for a missing document, and
  only the exception's type for anything else; no stored text is in an error. Enforced by:
  tests/test_review_server.py::test_an_unexpected_error_names_only_its_type.

Terms:
- **documents**: the page's answers, one JSON file of `path -> document`, where a path is
  `<collection>/<id>`. The page stores `{verdict, at}` for each card.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .files import write_atomic

APP = "review"
MAX_BODY_BYTES = 64 * 1024
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_PATH = re.compile(r"^[A-Za-z0-9_.:-]+(/[A-Za-z0-9_.:-]+)+$")


class Documents:
    """The page's database: documents by path in one JSON file. The file is read for every call
    and rewritten whole under a lock, so a call never sees another's half-written state."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        loaded = json.loads(self.path.read_text("utf-8"))
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _checked(path: object) -> str:
        if not isinstance(path, str) or not _PATH.match(path) or ".." in path.split("/"):
            raise ValueError("a path is words joined by slashes, like reviews/<corpus>/items/<id>")
        return path

    def get(self, path: str) -> dict | None:
        return self._load().get(self._checked(path))

    def set(self, path: str, data: object) -> None:
        if not isinstance(data, dict):
            raise ValueError("a document is a JSON object")
        key = self._checked(path)
        with self._lock:
            documents = self._load()
            documents[key] = data
            write_atomic(self.path, json.dumps(documents, ensure_ascii=False, sort_keys=True))

    def delete(self, path: str) -> bool:
        key = self._checked(path)
        with self._lock:
            documents = self._load()
            if key not in documents:
                return False
            del documents[key]
            write_atomic(self.path, json.dumps(documents, ensure_ascii=False, sort_keys=True))
            return True

    def list(self, collection: str) -> list[dict]:
        """The documents directly under `collection`: `[{"id", "data"}]`, by id."""
        prefix = self._checked(collection) + "/"
        return [
            {"id": key[len(prefix) :], "data": value}
            for key, value in sorted(self._load().items())
            if key.startswith(prefix) and "/" not in key[len(prefix) :]
        ]

    def decisions(self, corpus_id: str) -> dict:
        """The decisions file for one corpus (`decisions_to_verdicts`' input): every card's
        saved verdict under `reviews/<corpus id>/items`."""
        return {
            "corpus_id": corpus_id,
            "decisions": {
                row["id"]: {"verdict": str(row["data"].get("verdict", ""))}
                for row in self.list(f"reviews/{corpus_id}/items")
            },
        }

    def call(self, method: object, params: object) -> object:
        """Dispatch one API call. ValueError for a bad call, KeyError for a missing document."""
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        if method == "health":
            return {"status": "ok"}
        if method == "get":
            found = self.get(params.get("path"))  # type: ignore[arg-type]
            if found is None:
                raise KeyError("no such document")
            return found
        if method == "set":
            self.set(params.get("path"), params.get("data"))  # type: ignore[arg-type]
            return {"saved": True}
        if method == "delete":
            return {"deleted": self.delete(params.get("path"))}  # type: ignore[arg-type]
        if method == "list":
            return self.list(params.get("collection"))  # type: ignore[arg-type]
        raise ValueError("method must be one of: health, get, set, delete, list")


def _host_is_this_machine(host_header: str) -> bool:
    name = host_header.rsplit(":", 1)[0] if not host_header.endswith("]") else host_header
    if host_header.startswith("[") and "]" in host_header:
        name = host_header[: host_header.index("]") + 1]
    return name in LOOPBACK_HOSTS or name.endswith(".ts.net")


def make_server(
    host: str, port: int, *, page: Callable[[], str], documents: Documents
) -> ThreadingHTTPServer:
    """A server for `page` (called for every `GET /`) over `documents`. Loopback addresses only."""
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
                self._json(200, {"status": "ok"})
            elif self.path in ("/", "/index.html"):
                html = page()
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
                result = documents.call(body.get("method"), body.get("params", {}))
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


__all__ = ["APP", "Documents", "MAX_BODY_BYTES", "make_server"]
