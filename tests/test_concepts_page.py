"""The concepts page (`src/corpuslm/concepts_page.py`): labels and counts only, escaped, with
the boundary rows, the ruled rows and the bars."""

from __future__ import annotations

from corpuslm import concepts_page

SUMMARY = {
    "concepts": 3,
    "states": {"in": 1, "out": 1, "boundary": 1},
    "verdicts": 0,
    "gate": {"min_sections": 3, "min_claims": 5},
    "boundary": [{"key": "beat", "label": "Beat <b>", "sections": 3, "claims": 6, "sources": 2}],
    "sections_histogram": {1: 1, 3: 1, 9: 1},
}


def test_the_page_shows_labels_and_counts_and_escapes_them():
    html = concepts_page.render(SUMMARY)
    assert "<title>Concepts</title>" in html
    assert "3 concepts: 1 in, 1 boundary, 1 out" in html and "gate 3 sections, 5 claims" in html
    assert (
        'data-key="beat"' in html
        and "Beat &lt;b&gt;" in html
        and "<b>"
        not in html.split("<body>")[1]
        .split("<script")[0]
        .replace("<button", "")
        .replace("<b", "", 0)
    )
    assert "3 sections · 6 claims · 2 sources" in html
    assert html.count('data-v="in"') == 1 and html.count('data-v="out"') == 1
    assert "Ruled" not in html
    assert (
        '<div class="bar gate"><span>3</span>' in html and '<div class="bar"><span>1</span>' in html
    )
    assert "Time is the counting" not in html  # the page never carries a claim's text


def test_ruled_concepts_are_offered_again_with_their_ruling_pressed():
    ruled = [
        {
            "key": "choice",
            "label": "choice",
            "sections": 4,
            "claims": 9,
            "sources": 3,
            "verdict": "out",
        }
    ]
    html = concepts_page.render(SUMMARY | {"boundary": []}, ruled)
    assert "Nothing on the boundary." in html and "<h2>Ruled</h2>" in html
    assert 'data-key="choice"' in html
    assert 'class="out" data-v="out" aria-pressed="true"' in html
    assert 'data-v="in" aria-pressed="false"' in html


def test_an_empty_lexicon_renders_without_bars():
    html = concepts_page.render(
        SUMMARY
        | {
            "concepts": 0,
            "states": {"in": 0, "out": 0, "boundary": 0},
            "boundary": [],
            "sections_histogram": {},
        }
    )
    assert "No concepts yet." in html and "0 concepts" in html
