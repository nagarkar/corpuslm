"""The directives page (`src/corpuslm/directives_page.py`): heard, restatement, addressee and
time per card, form cards first, everything escaped."""

from corpuslm import directives_page

SUMMARY = {
    "directives": 2,
    "states": {"confirm": 1, "decline": 0, "open": 1},
    "addressees": {"computer": 1, "self": 1},
}
ROWS = [
    {"id": "clm-1", "source_id": "bee:1", "title": "A walk <b>", "date": "2026-10-20",
     "subject": "s", "state": "open", "addressee": "computer",
     "heard": "Computer, remind me to call Wren & co", "text": "Remind the owner to call Wren.",
     "start_seconds": 61.0, "position": 1},
    {"id": "clm-2", "source_id": "bee:1", "title": "A walk <b>", "date": "2026-10-20",
     "subject": "t", "state": "confirm", "addressee": "self",
     "heard": "buy milk", "text": "Buy milk on the way home.", "start_seconds": 125.0,
     "position": 2},
]  # fmt: skip
FORM_CARDS = [
    {"source_id": "bee:2", "title": "Dinner <i>", "date": "2026-10-19", "count": 1,
     "proposed": "monologue", "jev": "media", "label": "media"},
]  # fmt: skip


def test_the_page_shows_heard_restatement_addressee_and_time():
    html = directives_page.render(SUMMARY, ROWS, FORM_CARDS)
    assert "<title>Directives</title>" in html
    assert "2 directives: 1 open, 1 confirmed, 0 declined · 1 to Computer" in html
    assert "<h2>Form · 1 to rule</h2>" in html and 'data-source="bee:2"' in html
    assert "Dinner &lt;i&gt; · 2026-10-19" in html
    assert "1 named speakers say <b>monologue</b>; Jev says <b>media</b>" in html
    assert html.count('class="buttons four"') == 1 and 'data-v="group"' in html
    assert "<h2>A walk &lt;b&gt; · 2026-10-20</h2>" in html and html.count("<h2>") == 2
    assert "Heard: <b>Computer, remind me to call Wren &amp; co</b>" in html
    assert "Remind the owner to call Wren." in html
    assert '<span class="who computer">to computer</span> · at 1:01' in html
    assert '<span class="who self">to self</span> · at 2:05' in html
    assert 'class="card directive" data-id="clm-1"' in html
    assert 'class="card directive done" data-id="clm-2"' in html
    assert html.count('data-v="confirm" aria-pressed="true"') == 1
    assert "directive_verdict" in html and "form_verdict" in html


def test_an_empty_page_says_so():
    html = directives_page.render(
        {"directives": 0, "states": {"confirm": 0, "decline": 0, "open": 0}, "addressees": {}},
        [],
    )
    assert "No directives yet." in html and "<h2>" not in html
