"""The moments page (`src/corpuslm/moments_page.py`): insights, spans and concepts only, escaped,
grouped by source, with the buttons."""

from corpuslm import moments_page

SUMMARY = {
    "moments": 3,
    "states": {"approved": 1, "declined": 0, "proposed": 2},
    "origins": {"import": 1, "model": 2},
}
ROWS = [
    {"id": "mom-1", "source_id": "bee:1", "title": "A walk <b>", "date": "2026-09-20",
     "subject": "s",
     "first_position": 2, "last_position": 6, "start_seconds": 25.0, "end_seconds": 70.0,
     "insight": "A choice is right only as of now & then.", "origin": "model", "state": "proposed",
     "concepts": ["choice", "beat"]},
    {"id": "mom-2", "source_id": "bee:1", "title": "A walk <b>", "date": "2026-09-20",
     "subject": "t",
     "first_position": 9, "last_position": 11, "start_seconds": 90.0, "end_seconds": 120.0,
     "insight": "Ruled already.", "origin": "import", "state": "approved", "concepts": []},
]  # fmt: skip


def test_the_page_shows_insights_spans_and_concepts_and_escapes():
    html = moments_page.render(SUMMARY, ROWS)
    assert "<title>Moments</title>" in html
    assert "3 moments: 1 approved, 2 proposed, 0 declined" in html
    assert "<h2>A walk &lt;b&gt; · 2026-09-20</h2>" in html and html.count("<h2>") == 1
    assert "A choice is right only as of now &amp; then." in html
    assert "0:25 to 1:10 · model" in html and "1:30 to 2:00 · import" in html
    assert "<span>choice</span><span>beat</span>" in html
    assert 'data-id="mom-1"' in html and 'class="card done" data-id="mom-2"' in html
    assert html.count('data-v="approved"') == 2 and 'data-v="approved" aria-pressed="true"' in html
    assert "line 2" not in html  # never a segment's text


def test_an_empty_list_says_so():
    html = moments_page.render(SUMMARY | {"moments": 0}, [])
    assert "Nothing proposed." in html
