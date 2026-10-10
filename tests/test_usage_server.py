"""The usage server (corpuslm.usage_server): two GET routes, loopback only."""

from __future__ import annotations

import http.client
import threading

import pytest

from corpuslm import usage_server


@pytest.fixture
def server():
    httpd = usage_server.make_server("127.0.0.1", 0, page=lambda: "<p>the page</p>")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def request(server, method, path, headers=None):
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def test_only_the_page_and_health_are_routes(server):
    assert request(server, "GET", "/") == (200, b"<p>the page</p>")
    assert request(server, "GET", "/health")[0] == 200
    assert request(server, "GET", "/api/review")[0] == 404
    assert request(server, "POST", "/")[0] == 501
    assert request(server, "DELETE", "/")[0] == 501


def test_a_host_that_is_not_this_machine_gets_421(server):
    assert request(server, "GET", "/", {"Host": "evil.example"})[0] == 421
    assert request(server, "GET", "/health", {"Host": "imac.tail1234.ts.net"})[0] == 200


def test_the_server_will_not_bind_a_public_address():
    with pytest.raises(ValueError, match="loopback"):
        usage_server.make_server("0.0.0.0", 0, page=lambda: "")
