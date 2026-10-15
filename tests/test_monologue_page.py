"""The new-monologue flow (`corpuslm.monologues.candidates`, `corpuslm.monologue_page`, and the
server's monologue calls): candidates ranked by shared concepts and the brief, a source attached
as an occasional body, the pages' rendering, and the calls' checks. Synthetic text only."""

from __future__ import annotations

import json
import threading

import pytest

from corpuslm import bodies, claims, db, monologue_page, monologues, review_server
from corpuslm.store import Store


@pytest.fixture
def world(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    store = Store(database)
    segs = [
        {"idx": i, "t0": float(i), "t1": i + 1.0, "text": f"line {i} about karma"} for i in range(4)
    ]
    for source_id, kind, title, date in (
        ("bee:1", "bee", "A walk", "2026-09-20"),
        ("bee:2", "bee", "A talk", "2026-10-01"),
        ("vid1", "youtube", "Karma explained", "2025-01-01"),
        ("vid2", "youtube", "Other video", "2025-02-01"),
    ):
        store.add_source(source_id, "l", created_by="t", kind=kind)
        store.update_source(source_id, {"title": title, "published_at": date})
        store.replace_segments(
            source_id, segs if source_id != "vid2" else [segs[0] | {"text": "nothing here"}]
        )
        store.replace_claims(
            source_id,
            [{"section": 0, "segments": [0], "text": f"Claim {source_id}.", "kind": "assertion"}],
            build_key=f"k-{source_id}", created_by="t",
        )  # fmt: skip
        store.record_artifact(
            source_id, claims.NODE, path="", sha256=f"k-{source_id}", state="built",
            inputs={}, provenance={}, created_by="t",
        )  # fmt: skip
    for source_id, keys in (
        ("bee:1", ["karma", "beat"]),
        ("bee:2", ["beat"]),
        ("vid1", ["karma"]),
        ("vid2", ["rank"]),
    ):
        store.tag_claims(
            {store.claims(source_id)[0]["id"]: [(k, k) for k in keys]},
            build_key="k",
            created_by="t",
        )
    store.refresh_concept_counts()
    ids = {}
    for source_id, insight in (
        ("bee:1", "Karma is the count of choices"),
        ("bee:2", "Beats move on"),
        ("vid1", "a video moment"),
    ):
        ids[source_id] = store.add_moment(source_id, 0, 1, insight, origin="import", created_by="t")
        store.append_moment_verdict(source_id, store.moment_subject(source_id, 0, 1), "approved",
                                    answered_through="review file", created_by="t")  # fmt: skip
    unruled = store.add_moment("bee:2", 2, 3, "Unruled", origin="model", created_by="t")
    bodies.add(store, "SMSTTD", text_source_id="bee:1", created_by="t")
    monologues.create(store, "karma", brief="karma and choosing again", created_by="t")
    yield tmp_path, store, ids, unruled
    database.close()


def test_candidates_rank_by_shared_concepts_and_the_brief_and_leave_out_the_attached(world):
    tmp_path, store, ids, unruled = world
    first = monologues.candidates(store, "karma")
    assert [m["insight"] for m in first["moments"]][:1] == [
        "Karma is the count of choices"
    ]  # the brief's word
    assert first["moments"][0]["brief_words"] == ["karma"] and first["concepts"] == []
    assert [b["name"] for b in first["bodies"]] == ["SMSTTD"] and first["bodies"][0][
        "reason"
    ] == "never used yet"
    assert [s["source_id"] for s in first["sources"]] == ["vid1"]  # full-text match on "karma"
    assert first["sources"][0]["full_text"] is True and first["sources"][0]["shared_concepts"] == []
    assert all(m["id"] != unruled for m in first["moments"])
    monologues.attach(store, "karma", moment_id=ids["bee:1"])
    again = monologues.candidates(store, "karma")
    assert [m["id"] for m in again["attached_moments"]] == [ids["bee:1"]]
    assert again["concepts"] == ["beat", "karma"] or again["concepts"] == ["karma", "beat"]
    offered = {m["source_id"]: m["shared_concepts"] for m in again["moments"]}
    assert offered == {"bee:2": ["beat"], "vid1": ["karma"]}
    vid1 = next(s for s in again["sources"] if s["source_id"] == "vid1")
    assert vid1["shared_concepts"] == ["karma"] and vid1["full_text"] is True and vid1["score"] == 3
    assert all(s["source_id"] != "vid2" for s in again["sources"])
    store.update_monologue("karma", {"brief": ""})
    bare = monologues.candidates(store, "karma")
    assert bare["sources"] and all(not s["full_text"] for s in bare["sources"])


def test_attach_source_makes_an_occasional_body_once(world):
    tmp_path, store, ids, _ = world
    out = monologues.attach_source(store, "karma", "vid1", created_by="t")
    assert out == {"monologue": "karma", "body": "source:vid1", "attached": True}
    assert store.body("source:vid1")["kind"] == "occasional"
    assert monologues.attach_source(store, "karma", "vid1", created_by="t")["attached"] is False
    assert all(s["source_id"] != "vid1" for s in monologues.candidates(store, "karma")["sources"])
    with pytest.raises(KeyError, match="unknown source"):
        monologues.attach_source(store, "karma", "nope", created_by="t")
    assert monologues.brief_words("The karma of choosing again, and the beat") == [
        "karma",
        "choosing",
        "again",
        "beat",
    ]


def test_the_pages_render_the_choices_escaped_and_without_transcript_text(world):
    tmp_path, store, ids, _ = world
    store.update_monologue("karma", {"title": "Karma <b>"})
    monologues.attach(store, "karma", moment_id=ids["bee:1"])
    html = monologue_page.render(monologues.candidates(store, "karma"))
    assert "<title>Karma &lt;b&gt;</title>" in html and 'data-name="karma"' in html
    assert 'data-act="detach" data-kind="moment" data-id="' + ids["bee:1"] in html
    assert 'data-act="attach" data-kind="moment" data-id="' + ids["bee:2"] in html
    assert 'data-act="attach" data-kind="body" data-id="SMSTTD"' in html
    assert 'data-act="attach" data-kind="source" data-id="vid1"' in html
    assert '<span class="hot">beat</span>' in html and "line 0 about karma" not in html
    assert 'data-act="estimate"' in html and 'id="run" data-act="run" hidden' in html
    # every moment, attached or offered, can be reworded or deleted in context
    assert f'data-moment="{ids["bee:1"]}" data-attached="1"' in html
    assert f'data-moment="{ids["bee:2"]}" data-attached=""' in html
    assert html.count('data-act="edit"') == html.count('data-act="delete"') >= 2
    # the brief is a tall box the owner can resize, and a laptop gets the full width
    assert '<textarea id="brief" rows="12"' in html and "resize:vertical" in html
    assert "#brief{min-height:40vh}" in html and "@media (min-width:900px)" in html
    listed = monologue_page.render_list(monologues.listing(store, None, None))
    assert '<a href="/monologues/karma">Karma &lt;b&gt;</a>' in listed and "1 moments" in listed
    assert "No monologues yet." in monologue_page.render_list([])


def test_the_server_calls_make_attach_and_decline_what_needs_settings(world):
    tmp_path, store, ids, _ = world
    answers = review_server.Answers(tmp_path / "transcript.sqlite")
    httpd = review_server.make_server(
        "127.0.0.1", 0, page=lambda: "<title>p</title>", answers=answers
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    headers = {"Content-Type": "application/json", "X-Review": "1"}

    def post(method, params):
        from test_review_server import request

        status, body = request(
            httpd, "POST", "/api/review", json.dumps({"method": method, "params": params}), headers
        )
        return status, json.loads(body)

    try:
        status, body = post("monologue_new", {"name": "Second Walk", "brief": "beats"})
        assert status == 200 and body["result"]["name"] == "second-walk"
        status, body = post("monologue_new", {"name": "second walk"})
        assert status == 400 and "exists" in body["error"]
        status, body = post(
            "monologue_brief", {"name": "second-walk", "title": "Beats", "brief": " b "}
        )
        assert status == 200 and body["result"]["saved"] == ["brief", "title"]
        status, body = post("monologue_attach", {"name": "second-walk", "moment": ids["bee:2"]})
        assert status == 200 and body["result"]["attached"] is True
        status, body = post("monologue_attach", {"name": "second-walk", "source": "vid1"})
        assert status == 200 and body["result"]["body"] == "source:vid1"
        status, body = post("monologue_detach", {"name": "second-walk", "source": "vid1"})
        assert status == 200 and body["result"]["detached"] is True
        status, body = post(
            "monologue_attach", {"name": "second-walk", "moment": ids["bee:2"], "body": "SMSTTD"}
        )
        assert status == 400 and "one of moment, body or source" in body["error"]
        status, body = post("monologue_estimate", {"name": "second-walk"})
        assert status == 400 and "without [claims] and [monologue] settings" in body["error"]
        status, body = post("monologue_run", {"name": "second-walk"})
        assert status == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
    database = db.Database(tmp_path / "transcript.sqlite")
    try:
        row = Store(database).monologue("second-walk")
        assert (row["title"], row["brief"]) == ("Beats", "b")
        assert len(Store(database).monologue_moments(row["id"])) == 1
    finally:
        database.close()


def test_search_finds_every_moment_and_every_video_not_only_the_shortlist(world):
    tmp_path, store, ids, unruled = world
    monologues.attach(store, "karma", moment_id=ids["bee:1"])
    store.update_source(
        "vid2",
        {
            "channel_title": "Essentia Foundation",
            "description": "A talk on the screen and the film",
        },
    )
    found = monologues.search(store, "karma", "moments", "unruled")
    assert [(m["id"], m["state"]) for m in found["items"]] == [(unruled, "proposed")]
    assert monologues.search(store, "karma", "moments", "karma count")["items"] == []  # attached
    videos = monologues.search(store, "karma", "videos", "essentia screen")
    assert [v["source_id"] for v in videos["items"]] == ["vid2"]
    assert videos["items"][0]["channel"] == "Essentia Foundation"
    html = monologue_page.render_results("moments", found["items"], found["found"])
    assert 'data-act="approve-attach"' in html and "Approve &amp; add" in html
    cards = monologue_page.render_results("videos", videos["items"], videos["found"])
    assert "Essentia Foundation" in cards and "https://www.youtube.com/watch?v=vid2" in cards
    labelled = monologue_page.video_card(videos["items"][0] | {"people": ["HOST", "Swami"]})
    assert "Swami" in labelled and "HOST" not in labelled
    assert monologue_page.render_results("videos", [], 0) == '<p class="empty">Nothing matches.</p>'
    with pytest.raises(ValueError):
        monologues.search(store, "karma", "bodies", "x")
