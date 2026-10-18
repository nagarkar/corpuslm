"""Brand: the CorpusLM icon, favicons and wordmarks every served page wears.

The files in `corpuslm/brand/` are copied unchanged from metalm `brand/corpuslm/` (made by
metalm `tools/notifier/make_brand.py`; never redrawn here). `HEAD` is the head links every page
puts in its `<head>`, `ICON` the small icon a page header shows beside its title (it removes
itself where no server answers `/brand/`, as on a page published in a chat), and `asset` answers
a server's GET of `/favicon.ico` or `/brand/<file>`.

Never imports: cli, clikit, db, store, any model transport.

Decisions:
- Every served page links `favicon.ico`, `favicon-32.png` and `apple-touch-icon.png` in its head
  (metalm guidelines/ui.md#brand). Enforced by:
  tests/test_brand.py::test_every_served_page_links_the_favicons,
  tests/test_usage.py::test_the_page_shows_spend_caps_fetches_and_the_note,
  tests/test_monologue_page.py::test_the_pages_render_the_choices_escaped_and_without_transcript_text.
- Both servers answer `/favicon.ico` and `/brand/<file>` with the image and nothing else under
  `/brand/`. Enforced by: tests/test_brand.py::test_the_usage_server_serves_the_favicon,
  tests/test_brand.py::test_the_review_server_serves_the_brand_files.
"""

from __future__ import annotations

from pathlib import Path

DIRECTORY = Path(__file__).parent / "brand"

TYPES = {
    "favicon.ico": "image/x-icon",
    "favicon-32.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "logo.png": "image/png",
    "logo-wordmark-light.png": "image/png",
    "logo-wordmark-dark.png": "image/png",
}

HEAD = (
    '<link rel="icon" href="/favicon.ico" sizes="any">'
    '<link rel="icon" type="image/png" sizes="32x32" href="/brand/favicon-32.png">'
    '<link rel="apple-touch-icon" href="/brand/apple-touch-icon.png">'
)

ICON = (
    '<img src="/brand/icon-192.png" alt="" width="22" height="22" '
    'style="vertical-align:-4px;margin-right:8px" onerror="this.remove()">'
)


def asset(path: str) -> tuple[bytes, str] | None:
    """The bytes and content type for `/favicon.ico` or `/brand/<file>`, else None."""
    path = path.split("?", 1)[0]
    if path == "/favicon.ico":
        name = "favicon.ico"
    elif path.startswith("/brand/"):
        name = path[len("/brand/") :]
    else:
        return None
    if name not in TYPES:
        return None
    return (DIRECTORY / name).read_bytes(), TYPES[name]


__all__ = ["DIRECTORY", "HEAD", "ICON", "TYPES", "asset"]
