"""Review server (corpuslm.review_server): the page's database over HTTP on loopback, answers kept
in the shared transcript database, with Bee-shaped fixtures only."""

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
def corpora(tmp_path):
    """Two corpora in a transcript database; (database path, first id, second id)."""
    path = tmp_path / "transcript.sqlite"
    database = db.Database(path)
    store = Store(database)
    first = store.add_corpus({"name": "Walks"}, created_by="test")
    second = store.add_corpus({"name": "Talks"}, created_by="test")
    database.close()
    return path, first, second


@pytest.fixture
def documents(corpora):
    return review_server.Answers(corpora[0])


@pytest.fixture
def server(documents):
    httpd = review_server.make_server(
        "127.0.0.1", 0, page=lambda: "<title>page</title>", answers=documents
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
        "127.0.0.1", 0, page=lambda: builds.append(1) or "<title>p</title>", answers=documents
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


def test_answers_round_trip_set_get_list_delete(server, corpora):
    _, first, second = corpora
    path = f"reviews/{first}/items/card-1"
    answered_at = "2026-10-09T10:00:00Z"
    assert (
        call(server, "set", {"path": path, "data": {"verdict": "Wren", "at": answered_at}})[0]
        == 200
    )
    assert (
        call(server, "set", {"path": f"reviews/{first}/items/card-2", "data": {"verdict": ""}})[0]
        == 200
    )
    assert (
        call(server, "set", {"path": f"reviews/{second}/items/card-9", "data": {"verdict": "x"}})[0]
        == 200
    )
    assert call(server, "get", {"path": path}) == (
        200,
        {"result": {"verdict": "Wren", "at": answered_at}},
    )
    status, listed = call(server, "list", {"collection": f"reviews/{first}/items"})
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
    assert call(server, "set", {"path": "reviews/cor-nope/items/c", "data": {}})[0] == 400
    assert call(server, "set", {"path": "reviews/x/items/c", "data": {"at": "yesterday"}})[0] == 400
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
        review_server.make_server("0.0.0.0", 0, page=lambda: "", answers=documents)


def test_two_taps_at_once_both_land(server, documents, corpora):
    _, first, _ = corpora

    def tap(tap_number):
        call(
            server,
            "set",
            {"path": f"reviews/{first}/items/c{tap_number}", "data": {"verdict": str(tap_number)}},
        )

    threads = [threading.Thread(target=tap, args=(tap_number,)) for tap_number in range(25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(documents.list(f"reviews/{first}/items")) == 25


def test_an_unexpected_error_names_only_its_type(tmp_path):
    class Broken(review_server.Answers):
        def call(self, method, params):
            raise RuntimeError("secret words from a transcript")

    httpd = review_server.make_server(
        "127.0.0.1", 0, page=lambda: "", answers=Broken(tmp_path / "r.sqlite")
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, data = call(httpd, "health")
        assert (status, data) == (500, {"error": "RuntimeError"})
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_answers_of_every_corpus_share_one_table(documents, corpora):
    path, first, second = corpora
    documents.set(f"reviews/{first}/items/a", {"verdict": "one"})
    documents.set(f"reviews/{second}/items/a", {"verdict": "two"})
    database = db.Database(path)
    try:
        assert Store(database).review_answer_counts() == {first: 1, second: 1}
    finally:
        database.close()
    assert documents.get(f"reviews/{first}/items/a")["verdict"] == "one"


def test_health_says_when_the_server_started(server):
    status, data = request(server, "GET", "/health")
    assert status == 200 and json.loads(data)["started_at"].endswith("Z")


def test_saved_answers_become_a_decisions_file_that_lands_as_verdicts(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    documents = review_server.Answers(tmp_path / "transcript.sqlite")
    try:
        sources = tmp_path / "sources"
        corpus_id = add_corpus_of_bee_sources(
            Store(database), "Walks", [BEE_SOURCE, SECOND_BEE_SOURCE]
        )
        write_source(sources, BEE_SOURCE, bee_transcript(), bee_speaker_map())
        write_source(sources, SECOND_BEE_SOURCE, bee_transcript())
        data = review_page.page_data(database, sources, "Walks", kind=BEE_KIND)
        speaker = next(card for card in data["cards"] if card.get("role") == "Unknown")
        documents.set(
            f"{data['collection']}/{speaker['id']}",
            {"verdict": "Wren", "at": "2026-10-09T10:00:00.123Z"},
        )
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


# ---- the concepts page and its one write (corpuslm.concepts_page) -------------------------


def test_an_extra_page_is_served_and_a_concept_verdict_lands(corpora, documents):
    path = corpora[0]
    database = db.Database(path)
    store = Store(database)
    store.add_source(BEE_SOURCE, "l", created_by="test", kind="bee")
    store.replace_segments(BEE_SOURCE, [{"idx": 0, "t0": 0.0, "t1": 1.0, "text": "a line"}])
    store.replace_claims(
        BEE_SOURCE, [{"section": 0, "segments": [0], "text": "One.", "kind": "assertion"}],
        build_key="k", created_by="t",
    )  # fmt: skip
    store.tag_claims(
        {store.claims(BEE_SOURCE)[0]["id"]: [("beat", "Beat")]}, build_key="k", created_by="t"
    )
    store.refresh_concept_counts()
    database.close()
    httpd = review_server.make_server(
        "127.0.0.1", 0, page=lambda: "<title>page</title>", answers=documents,
        pages={"/concepts": lambda: "<title>Concepts</title>"},
    )  # fmt: skip
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = request(httpd, "GET", "/concepts")
        assert status == 200 and b"<title>Concepts</title>" in body
        status, _ = request(httpd, "GET", "/nothing")
        assert status == 404
        headers = {"Content-Type": "application/json", "X-Review": "1"}
        status, body = request(
            httpd, "POST", "/api/review",
            json.dumps({"method": "concept_verdict", "params": {"key": "Beat", "verdict": "in"}}),
            headers,
        )  # fmt: skip
        assert status == 200 and json.loads(body)["result"] == {"key": "beat", "verdict": "in"}
        status, body = request(
            httpd, "POST", "/api/review",
            json.dumps(
                {"method": "concept_verdict", "params": {"key": "beat", "verdict": "maybe"}}
            ),
            headers,
        )  # fmt: skip
        assert status == 400 and "in or out" in json.loads(body)["error"]
        status, _ = request(
            httpd, "POST", "/api/review",
            json.dumps({"method": "concept_verdict", "params": {"key": "nope", "verdict": "in"}}),
            headers,
        )  # fmt: skip
        assert status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
    database = db.Database(path)
    try:
        verdicts = Store(database).current_concept_verdicts()
        assert list(verdicts.values()) == ["in"]
    finally:
        database.close()


def test_a_moment_verdict_lands_through_the_api(corpora, documents):
    path = corpora[0]
    database = db.Database(path)
    store = Store(database)
    store.add_source(BEE_SOURCE, "l", created_by="test", kind="bee")
    made = store.add_moment(BEE_SOURCE, 0, 1, "x", origin="model", created_by="t")
    database.close()
    httpd = review_server.make_server(
        "127.0.0.1", 0, page=lambda: "<title>page</title>", answers=documents
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    headers = {"Content-Type": "application/json", "X-Review": "1"}
    try:
        status, body = request(
            httpd, "POST", "/api/review",
            json.dumps({"method": "moment_verdict", "params": {"id": made, "verdict": "approved"}}),
            headers,
        )  # fmt: skip
        assert status == 200 and json.loads(body)["result"] == {"id": made, "verdict": "approved"}
        status, body = request(
            httpd, "POST", "/api/review",
            json.dumps({"method": "moment_verdict", "params": {"id": made, "verdict": "later"}}),
            headers,
        )  # fmt: skip
        assert status == 400 and "approved or declined" in json.loads(body)["error"]
        status, _ = request(
            httpd, "POST", "/api/review",
            json.dumps(
                {"method": "moment_verdict", "params": {"id": "mom-nope", "verdict": "approved"}}
            ),
            headers,
        )  # fmt: skip
        assert status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
    database = db.Database(path)
    try:
        assert list(Store(database).current_moment_verdicts(BEE_SOURCE).values()) == ["approved"]
    finally:
        database.close()


def test_several_moments_are_ruled_at_once_or_not_at_all(corpora, documents):
    path = corpora[0]
    database = db.Database(path)
    store = Store(database)
    store.add_source(BEE_SOURCE, "l", created_by="test", kind="bee")
    first = store.add_moment(BEE_SOURCE, 0, 1, "x", origin="model", created_by="t")
    second = store.add_moment(BEE_SOURCE, 2, 3, "y", origin="model", created_by="t")
    database.close()
    httpd = review_server.make_server(
        "127.0.0.1", 0, page=lambda: "<title>page</title>", answers=documents
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    headers = {"Content-Type": "application/json", "X-Review": "1"}

    def post(params):
        return request(
            httpd, "POST", "/api/review",
            json.dumps({"method": "moment_verdicts", "params": params}), headers,
        )  # fmt: skip

    try:
        status, body = post({"ids": [first, second], "verdict": "declined"})
        assert status == 200 and json.loads(body)["result"] == {"ruled": 2, "verdict": "declined"}
        status, _ = post({"ids": [first, "mom-nope"], "verdict": "approved"})
        assert status == 404  # one unknown id: nothing of the batch lands
        status, _ = post({"ids": [], "verdict": "approved"})
        assert status == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
    database = db.Database(path)
    try:
        verdicts = Store(database).current_moment_verdicts(BEE_SOURCE)
        assert sorted(verdicts.values()) == ["declined", "declined"]
    finally:
        database.close()
