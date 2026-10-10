"""Usage server: serves the usage page on this machine, read-only.

A small standard-library HTTP server on the loopback address. Two routes, both GET: `/` the page,
built per request by the function the caller passes in, and `/health` with the time the server
started. Anything else is 404, any other method 501. It writes nothing and shows numbers and money,
never content.

Never imports: cli, clikit, db, store, any model transport.

Decisions:
- The server binds the loopback address only and answers only Host headers that name this machine
  (loopback or `*.ts.net`), else 421. Enforced by:
  tests/test_usage_server.py::test_the_server_will_not_bind_a_public_address,
  tests/test_usage_server.py::test_a_host_that_is_not_this_machine_gets_421.
- Only `GET /` and `GET /health` exist. Enforced by:
  tests/test_usage_server.py::test_only_the_page_and_health_are_routes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import clock

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _host_is_this_machine(header: str) -> bool:
    name = header[1 : header.index("]")] if header.startswith("[") else header.rsplit(":", 1)[0]
    return name in LOOPBACK or name.endswith(".ts.net")


def make_server(host: str, port: int, *, page: Callable[[], str]) -> ThreadingHTTPServer:
    """A server for `page` (called for every `GET /`). Loopback addresses only."""
    if host not in LOOPBACK:
        raise ValueError(f"the usage server binds the loopback address only, not {host!r}")
    started_at = clock.iso(clock.utc_now())

    class Handler(BaseHTTPRequestHandler):
        server_version = "corpuslm-usage"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # no request line is written anywhere

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: object) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            if not _host_is_this_machine(self.headers.get("Host", "")):
                self._json(421, {"error": "this server answers requests for this machine only"})
            elif self.path == "/health":
                self._json(200, {"status": "ok", "started_at": started_at})
            elif self.path.split("?")[0] == "/":
                try:
                    body = page().encode("utf-8")
                except Exception as error:  # noqa: BLE001 (the page names only the type)
                    self._json(500, {"error": type(error).__name__})
                else:
                    self._send(200, body, "text/html; charset=utf-8")
            else:
                self._json(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), Handler)
