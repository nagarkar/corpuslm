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
    assert 'data-id="mom-1"' in html and 'class="card done approved" data-id="mom-2"' in html
    assert html.count('<div class="all">') == 1  # one pair of Approve all / Decline all per source
    assert 'data-v="approved">Approve all<' in html and 'data-v="declined">Decline all<' in html
    assert html.count('data-v="approved"') == 2  # the proposed card and Approve all
    assert (
        ">Delete</button>" in html and html.count('class="edit"') == 2
    )  # approved: Delete; all: Edit
    assert "line 2" not in html and "<details" not in html  # no lines unless the caller adds them


def test_a_moment_folds_its_lines_and_offers_one_shared_picker_list():
    rows = [dict(ROWS[0]) | {"lines": [
        {"idx": 4, "t0": 25.0, "speaker": "Teo", "label": "Unknown", "text": "a <b> line",
         "unidentified": True, "pick": ""},  # named by a confident model pick
        {"idx": 5, "t0": 30.0, "speaker": "Unknown", "label": "Unknown", "text": "who said it",
         "unidentified": True, "pick": ""},  # still unsure
        {"idx": 6, "t0": 35.0, "speaker": "Ana", "label": "Unknown", "text": "picked",
         "unidentified": True, "pick": "Ana"},
        {"idx": 7, "t0": 40.0, "speaker": "Ana", "label": "Ana", "text": "hers",
         "unidentified": False, "pick": ""},
    ]}]  # fmt: skip
    choices = [("Ana", "Ana"), ("Teo", "Teo"), ("media", "Media</script>")]
    html = moments_page.render(SUMMARY, rows, choices)
    assert "<summary>Lines (4, 1 unsure) · who spoke</summary>" in html
    assert "a &lt;b&gt; line" in html
    assert html.count('class="line unknown"') == 1
    assert 'data-source="bee:1" data-position="5" data-pick="" data-shown="Unknown"' in html
    assert 'data-pick="Ana"' in html
    assert html.count("<select") == 0  # built on the phone when a fold opens
    assert '"Media\\u003c/script>"' in html  # the shared list cannot close its script tag


def test_an_empty_list_says_so():
    html = moments_page.render(SUMMARY | {"moments": 0}, [])
    assert "Nothing proposed." in html
