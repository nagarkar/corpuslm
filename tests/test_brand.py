"""The brand (corpuslm.brand): every served page links the favicons and both servers serve them."""

from __future__ import annotations

import http.client
import threading

import pytest
from test_concepts_page import SUMMARY as CONCEPTS
from test_moments_page import ROWS as MOMENT_ROWS
from test_moments_page import SUMMARY as MOMENTS

from corpuslm import (
    brand,
    concepts_page,
    moments_page,
    monologue_page,
    review_page,
    review_server,
    teleprompter_page,
    usage_server,
)

HEAD_LINKS = (
    '<link rel="icon" href="/favicon.ico"',
    'href="/brand/favicon-32.png"',
    '<link rel="apple-touch-icon" href="/brand/apple-touch-icon.png">',
)

PAGES = {
    "concepts": lambda: concepts_page.render(CONCEPTS),
    "moments": lambda: moments_page.render(MOMENTS, MOMENT_ROWS),
    "monologues": lambda: monologue_page.render_list([]),
    "teleprompter": lambda: teleprompter_page.render("karma", "Karma", "A line."),
    "review": lambda: review_page.render(
        {"title": "Corpus review", "corpora": [], "open_items": 0, "cards": 0}
    ),
}


@pytest.mark.parametrize("name", sorted(PAGES))
def test_every_served_page_links_the_favicons(name):
    page = PAGES[name]()
    head = page.split("<body", 1)[0]
    assert all(link in head for link in HEAD_LINKS), name
    if name != "teleprompter":  # the reading page keeps its header to the controls
        assert brand.ICON in page, name


def test_every_brand_file_ships_with_the_package():
    assert sorted(path.name for path in brand.DIRECTORY.iterdir()) == sorted(brand.TYPES)


def _get(httpd, path):
    port = httpd.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, response.getheader("Content-Type"), data


def _serving(httpd):
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _assert_serves_the_brand(httpd):
    status, content_type, data = _get(httpd, "/favicon.ico")
    assert (status, content_type) == (200, "image/x-icon")
    assert data == (brand.DIRECTORY / "favicon.ico").read_bytes()
    for name in ("favicon-32.png", "apple-touch-icon.png", "icon-192.png"):
        status, content_type, data = _get(httpd, f"/brand/{name}")
        assert (status, content_type) == (200, "image/png")
        assert data.startswith(b"\x89PNG")
    assert _get(httpd, "/brand/../brand.py")[0] == 404
    assert _get(httpd, "/brand/nothing.png")[0] == 404


def test_the_usage_server_serves_the_favicon():
    httpd = _serving(usage_server.make_server("127.0.0.1", 0, page=lambda: ""))
    try:
        _assert_serves_the_brand(httpd)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_review_server_serves_the_brand_files(tmp_path):
    answers = review_server.Answers(tmp_path / "corpus.sqlite")
    httpd = _serving(review_server.make_server("127.0.0.1", 0, page=lambda: "", answers=answers))
    try:
        _assert_serves_the_brand(httpd)
    finally:
        httpd.shutdown()
        httpd.server_close()
