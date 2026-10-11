"""Vault: the generic parts of an Obsidian vault export, for any source kind.

A vault is a folder opened in Obsidian as its own vault; the desk review surface. One note per
source and one per corpus. Every exported note carries its kind's marker line (ytlm's is
`ytlm: exported`) and is regenerated on change; a note without the marker is the owner's own
and is never touched.

This module holds what every kind shares: safe note names, the marker rules for writing and
removing notes, the corpus note, the speaker properties and the timestamped transcript lines.
What a source's own note says about its source (title, channel, recording date) stays in the
kind's repo. A transcript line links to the moment only when the kind's `anchor_url` gives a
link (corpuslm.review.ReviewKind); without one the line shows the time alone.

Never imports: cli, clikit, store, any model transport.

Decisions:
- A note without the kind's marker is never overwritten or removed. Enforced by:
  tests/test_vault.py::test_write_note_leaves_an_unmarked_note_at_the_target_alone.
- A source with no link gets transcript lines with the time alone. Enforced by:
  tests/test_vault.py::test_transcript_lines_without_a_link_show_the_time_alone.

Terms:
- **marker**: the frontmatter line (`<kind>: exported`) that says a note is exported and may be
  regenerated.
- **note**: a markdown file exported into the vault, one per source and one per corpus.
- **vault**: the folder opened in Obsidian; the desk review surface.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from .files import write_atomic
from .transcript import timestamp

_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]')
_HEAD_CHARACTERS = 600  # how much of a note is read to find its marker


def note_name(title: str) -> str:
    return _UNSAFE.sub("-", title).strip().rstrip(".") or "untitled"


def yaml_string(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def speaker_properties(speaker_map: dict | None) -> list[str]:
    """One flat frontmatter key per speaker label, so Obsidian's Properties panel edits it
    (the panel does not edit nested objects); a changed value lands through the review
    apply verb."""
    if not speaker_map or not speaker_map.get("roles"):
        return []
    return [
        f"speaker_{role}: {yaml_string(entry.get('name') or '')}"
        for role, entry in speaker_map["roles"].items()
    ]


def transcript_lines(
    transcript: dict, source: dict, anchor_url: Callable[[dict, float], str | None]
) -> list[str]:
    """One bullet per segment: the time (linked when `anchor_url` gives a link), the speaker
    in bold, the text."""
    lines = []
    for segment in transcript["segments"]:
        link = anchor_url(source, segment["t0"])
        moment = f"[{timestamp(segment['t0'])}]({link})" if link else timestamp(segment["t0"])
        speaker = f"**{segment['speaker']}**: " if segment.get("speaker") else ""
        lines.append(f"- {moment} {speaker}{segment['text']}")
    return lines


def render_corpus_note(
    corpus: dict,
    members: list[dict],
    *,
    marker: str,
    source_label: Callable[[dict], str],
    member_details: Callable[[dict], str],
) -> str:
    """`members` are source rows joined with membership status. A member line is a wikilink
    to the source's note, named `<note name> (<id>)` after its label, then its details."""
    lines = ["---", marker, f"corpus_id: {corpus['id']}", f"name: {yaml_string(corpus['name'])}"]
    for key in ("person", "subject"):
        if corpus.get(key):
            lines.append(f"{key}: {yaml_string(corpus[key])}")
    for key in ("topic", "perspective"):
        values = corpus.get(key) or []
        if values:
            lines.append(f"{key}:")
            lines += [f"  - {yaml_string(v)}" for v in values]
    lines += [
        "---",
        "",
        f"# {corpus['name']}",
        "",
        "## Corpus glossary",
        "",
        "_The corpus glossary arrives in build step 3._",
        "",
        "## Members",
        "",
    ]
    for member in members:
        title = source_label(member) or member["id"]
        link = f"[[{note_name(title)} ({member['id']})|{title}]]"
        lines.append(f"- {link} — {member_details(member)}")
    lines.append("")
    return "\n".join(lines)


def remove_exported(directory: Path, key: str, value: str, *, marker: str) -> int:
    """Delete exported notes whose frontmatter has `key: value` (rename safety)."""
    removed = 0
    if not directory.exists():
        return 0
    for existing in directory.glob("*.md"):
        try:
            head = existing.read_text(encoding="utf-8", errors="ignore")[:_HEAD_CHARACTERS]
        except OSError:
            continue
        if marker in head and f"{key}: {value}" in head:
            existing.unlink()
            removed += 1
    return removed


class UnmarkedNoteInTheWay(RuntimeError):
    """An owner's own note (no marker) sits at the export path; left alone."""


def write_note(
    directory: Path, filename: str, text: str, key: str, value: str, *, marker: str
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if (
        target.exists()
        and marker not in target.read_text(encoding="utf-8", errors="ignore")[:_HEAD_CHARACTERS]
    ):
        raise UnmarkedNoteInTheWay(f"{target} exists without the '{marker}' marker; not touched")
    for existing in directory.glob("*.md"):
        if existing != target:
            head = existing.read_text(encoding="utf-8", errors="ignore")[:_HEAD_CHARACTERS]
            if marker in head and f"{key}: {value}" in head:
                existing.unlink()
    write_atomic(target, text)
    return target
