"""Text sources: a manuscript, paper or book as a source of kind `text`, one segment per paragraph.

A text source enters only through an export file the owner made (for an authorlm manuscript,
`doc create-manuscript` writes `_exports/<Name>.md`); corpuslm never reads a live manuscript.
`import_text` parses the markdown, makes or refreshes the source (`text:<name>`), replaces its
segments whole, and keeps its sections (the spans under each heading of level one or two) in the
source's metadata so the claim pass reads one section per heading instead of a fixed number of
characters. A re-import replaces the segments; the source's claims then read `stale` with the
reason `text changed` until the owner rebuilds them.

A paragraph is a block separated by blank lines; a heading line is never a segment. The segment
times carry the paragraph's position (`t0` = index, `t1` = index + 1): a text has no clock, and
the columns need a number. The section headed `Perspective sheet` (corpuslm.document, the sheet
tab a body of work carries) and everything under it are left out, so the sheet is never a claim
about itself; the import names what it left out.

Never imports: cli, clikit, llm. Reads the database only through `Store`.

Decisions:
- A text source enters only through an owner-run export, never a live read of a manuscript.
  (owner)
- The perspective sheet tab is excluded from the text source on import. (owner)

Terms:
- **text source**: a source of kind `text`: a manuscript, paper or book imported from a
  markdown export, one segment per paragraph.
- **export**: the single markdown file the owner makes from a body of work and hands to
  `corpuslm text import`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .db import loads
from .store import Store

KIND = "text"
SHEET_HEADING = "perspective sheet"  # compared case-insensitively
SECTION_LEVELS = (1, 2)  # heading levels that start a section; deeper headings stay inside
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


@dataclass(frozen=True, slots=True)
class Section:
    heading: str
    level: int
    first: int  # first segment index, inclusive
    last: int  # last segment index, inclusive


@dataclass(frozen=True, slots=True)
class Parsed:
    title: str
    segments: tuple[dict, ...]
    sections: tuple[Section, ...]
    left_out: tuple[str, ...]  # headings of the sections the import left out
    words: int


def source_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError(f"the name {name!r} leaves no letters or digits for a source id")
    return f"text:{slug}"


def _strip_front_matter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[end + 4 :]
    return text


def parse(markdown: str, *, default_title: str = "") -> Parsed:
    """Paragraphs become segments; headings of level one or two open sections; the perspective
    sheet section is left out."""
    text = _strip_front_matter(markdown).replace("\r\n", "\n")
    segments: list[dict] = []
    sections: list[dict] = []
    left_out: list[str] = []
    title = ""
    skipping_level = 0  # > 0 while inside a left-out section, its heading level
    words = 0

    def open_section(heading: str, level: int) -> None:
        if sections and sections[-1]["first"] == len(segments):
            sections.pop()  # an empty section (a heading followed by a heading)
        sections.append({"heading": heading, "level": level, "first": len(segments)})

    for block in text.split("\n\n"):
        lines = [line for line in block.strip("\n").splitlines() if line.strip()]
        if not lines:
            continue
        for line in lines:
            match = _HEADING.match(line)
            if match:
                level, heading = len(match.group(1)), match.group(2)
                if not title and level == 1:
                    title = heading
                if skipping_level and level > skipping_level:
                    continue
                skipping_level = 0
                if heading.strip().lower() == SHEET_HEADING:
                    skipping_level = level
                    left_out.append(heading)
                    if sections and sections[-1]["first"] == len(segments):
                        sections.pop()
                    continue
                if level in SECTION_LEVELS:
                    open_section(heading, level)
                continue
        paragraph = "\n".join(line for line in lines if not _HEADING.match(line)).strip()
        if not paragraph or skipping_level:
            continue
        if not sections:
            open_section(title or default_title or "(untitled)", 1)
        index = len(segments)
        segments.append(
            {
                "idx": index,
                "t0": float(index),
                "t1": float(index + 1),
                "speaker": "",
                "text": paragraph,
            }
        )
        words += len(paragraph.split())
    if sections and sections[-1]["first"] == len(segments):
        sections.pop()
    made = tuple(
        Section(
            section["heading"],
            section["level"],
            section["first"],
            (sections[number + 1]["first"] if number + 1 < len(sections) else len(segments)) - 1,
        )
        for number, section in enumerate(sections)
    )
    return Parsed(title or default_title, tuple(segments), made, tuple(left_out), words)


def sections_of(source_row: dict) -> list[Section]:
    """The sections a text source's metadata records; empty for another kind."""
    recorded = (loads(source_row.get("metadata"), {}) or {}).get("text") or {}
    return [
        Section(entry["heading"], int(entry["level"]), int(entry["first"]), int(entry["last"]))
        for entry in recorded.get("sections") or []
    ]


def import_text(
    store: Store,
    path: Path,
    *,
    name: str,
    title: str = "",
    created_by: str,
    dry_run: bool,
) -> dict:
    """Make or refresh the text source `text:<name>` from the export at `path`. Returns counts
    and the headings left out; never the text."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist")
    parsed = parse(path.read_text(encoding="utf-8"), default_title=title or name)
    if not parsed.segments:
        raise ValueError(f"{path} has no paragraphs to import")
    made_id = source_id(name)
    existed = store.has_source(made_id)
    had_claims = store.claim_count(made_id) if existed else 0
    board = {
        "source": made_id,
        "created": not existed,
        "sections": len(parsed.sections),
        "paragraphs": len(parsed.segments),
        "words": parsed.words,
        "left_out": list(parsed.left_out),
        "claims_now_stale": had_claims,
    }
    if dry_run:
        return board
    with store.transaction():
        if not existed:
            store.add_source(made_id, str(path), created_by=created_by, kind=KIND)
        row = store.source(made_id) or {}
        metadata = loads(row.get("metadata"), {}) or {}
        metadata["text"] = {
            "sections": [
                {"heading": s.heading, "level": s.level, "first": s.first, "last": s.last}
                for s in parsed.sections
            ],
            "words": parsed.words,
            "left_out": list(parsed.left_out),
        }
        store.update_source(
            made_id,
            {
                "locator": str(path),
                "title": title or parsed.title or name,
                "status": "ingested",
                "transcript_state": "LLM-CLEANED",
                "metadata": json.dumps(metadata, sort_keys=True),
            },
            created_by=created_by,
        )
        store.replace_segments(made_id, parsed.segments)
    return board


__all__ = ["KIND", "Parsed", "Section", "import_text", "parse", "sections_of", "source_id"]
