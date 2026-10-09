"""Review server (corpuslm.review_server): the page's local database over HTTP on loopback, with
Bee-shaped fixtures only."""

from __future__ import annotations

import http.client
import json
import threading

import pytest
from review_kinds import (
    BEE_KIND,
    BEE_SOURCE,
    SECOND_BEE_SOURCE,
    add_corpus_of_bee_sources,
    bee_speaker_map,
    bee_transcript,
    write_source,
)

from corpuslm import db, review_page, review_server
from corpuslm.store import Store


@pytest.fixture
def documents(tmp_path):
    return review_server.Documents(tmp_path / "state" / "review.json")


@pytest.fixture
def server(documents):
    httpd = review_server.make_server(
        "127.0.0.1", 0, page=lambda: "<title>page</title>", documents=documents
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def request(server, method, path, body=None, headers=None):
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    send = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    connection.request(method, path, body=body, headers=send)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


GOOD = {"Content-Type": "application/json", "X-Review": "1"}


def call(server, method, params=None, headers=GOOD):
    body = json.dumps({"method": method, "params": params or {}})
    status, data = request(server, "POST", "/api/review", body, headers)
    return status, json.loads(data)


def test_the_page_and_health_are_served_and_the_page_is_built_for_each_request(documents):
    builds = []
    httpd = review_server.make_server(
        "127.0.0.1", 0, page=lambda: builds.append(1) or "<title>p</title>", documents=documents
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert request(httpd, "GET", "/") == (200, b"<title>p</title>")
        request(httpd, "GET", "/")
        assert len(builds) == 2
        assert request(httpd, "GET", "/health")[0] == 200
        assert request(httpd, "GET", "/elsewhere")[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_documents_round_trip_set_get_list_delete(server):
    path = "reviews/cor-1/items/card-1"
    assert call(server, "set", {"path": path, "data": {"verdict": "Wren"}})[0] == 200
    assert (
        call(server, "set", {"path": "reviews/cor-1/items/card-2", "data": {"verdict": ""}})[0]
        == 200
    )
    assert (
        call(server, "set", {"path": "reviews/cor-2/items/card-9", "data": {"verdict": "x"}})[0]
        == 200
    )
    assert call(server, "get", {"path": path}) == (200, {"result": {"verdict": "Wren"}})
    status, listed = call(server, "list", {"collection": "reviews/cor-1/items"})
    assert [row["id"] for row in listed["result"]] == ["card-1", "card-2"]
    assert call(server, "delete", {"path": path}) == (200, {"result": {"deleted": True}})
    assert call(server, "get", {"path": path})[0] == 404


def test_the_write_check_declines_each_bad_request(server):
    body = json.dumps({"method": "health", "params": {}})
    port = server.server_address[1]
    assert request(server, "POST", "/api/review", body, {"X-Review": "1"})[0] == 415
    assert (
        request(server, "POST", "/api/review", body, {"Content-Type": "application/json"})[0] == 403
    )
    foreign = {**GOOD, "Origin": "http://elsewhere.example"}
    assert request(server, "POST", "/api/review", body, foreign)[0] == 403
    own = {**GOOD, "Origin": f"http://127.0.0.1:{port}"}
    assert request(server, "POST", "/api/review", body, own)[0] == 200
    big = json.dumps({"method": "set", "params": {"path": "a/b", "data": {"x": "y" * 70_000}}})
    assert request(server, "POST", "/api/review", big, GOOD)[0] == 413
    assert request(server, "POST", "/api/review", "[1]", GOOD)[0] == 400
    assert request(server, "POST", "/api/review", "not json", GOOD)[0] == 400
    assert call(server, "set", {"path": "../escape", "data": {}})[0] == 400
    assert call(server, "set", {"path": "a/b", "data": [1]})[0] == 400
    assert call(server, "nonsense")[0] == 400
    assert request(server, "POST", "/api/other", body, GOOD)[0] == 404
    assert request(server, "OPTIONS", "/api/review")[0] == 501  # never answered


def test_a_host_that_is_not_this_machine_gets_421(server):
    status, _ = request(server, "GET", "/", headers={"Host": "evil.example"})
    assert status == 421
    assert request(server, "GET", "/health", headers={"Host": "imac.tail1234.ts.net"})[0] == 200


def test_the_server_will_not_bind_a_public_address(documents):
    with pytest.raises(ValueError, match="loopback"):
        review_server.make_server("0.0.0.0", 0, page=lambda: "", documents=documents)


def test_two_taps_at_once_both_land(server, documents):
    def tap(n):
        call(server, "set", {"path": f"reviews/cor-1/items/c{n}", "data": {"verdict": str(n)}})

    threads = [threading.Thread(target=tap, args=(n,)) for n in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(documents.list("reviews/cor-1/items")) == 25


def test_an_unexpected_error_names_only_its_type(tmp_path):
    class Broken(review_server.Documents):
        def call(self, method, params):
            raise RuntimeError("secret words from a transcript")

    httpd = review_server.make_server(
        "127.0.0.1", 0, page=lambda: "", documents=Broken(tmp_path / "r.json")
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, data = call(httpd, "health")
        assert (status, data) == (500, {"error": "RuntimeError"})
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_saved_answers_become_a_decisions_file_that_lands_as_verdicts(tmp_path, documents):
    database = db.Database(tmp_path / "transcript.sqlite")
    try:
        sources = tmp_path / "sources"
        corpus_id = add_corpus_of_bee_sources(
            Store(database), "Walks", [BEE_SOURCE, SECOND_BEE_SOURCE]
        )
        write_source(sources, BEE_SOURCE, bee_transcript(), bee_speaker_map())
        write_source(sources, SECOND_BEE_SOURCE, bee_transcript())
        data = review_page.page_data(database, sources, "Walks", kind=BEE_KIND)
        speaker = next(c for c in data["cards"] if c.get("role") == "Unknown")
        documents.set(f"{data['collection']}/{speaker['id']}", {"verdict": "Wren", "at": "t"})
        documents.set(f"{data['collection']}/blank", {"verdict": ""})
        decisions = documents.decisions(corpus_id)
        assert decisions["decisions"][speaker["id"]] == {"verdict": "Wren"}
        verdicts, unknown = review_page.decisions_to_verdicts(
            database, sources, "Walks", decisions, kind=BEE_KIND
        )
        assert unknown == []  # a blank answer is skipped, not unknown
        landed = review_page.land_page_verdicts(database, verdicts, created_by="test")
        assert landed == [f"{BEE_SOURCE}:spk-1"]
        assert Store(database).current_speaker_names(BEE_SOURCE) == {"Unknown": "Wren"}
        # the same answer again adds nothing
        review_page.land_page_verdicts(database, verdicts, created_by="test")
        assert len(Store(database).current_review_verdicts(BEE_SOURCE)) == 1
    finally:
        database.close()
