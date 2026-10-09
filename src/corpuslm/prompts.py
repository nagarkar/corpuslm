"""Prompts: versioned prompt files, one file per version, loaded by id.

A prompt is a file, `<directory>/<id>_v<N>.md`: TOML front matter above a markdown body with
`{placeholders}`. A change is a copy to the next version, never an edit in place;
`load(id)` takes the newest version, `load(id, version=N)` an exact one, so every prompt id and
version a ledger recorded still resolves. A prompt's sha256 travels in every artifact it
produced.

Prompts are searched in a list of directories: the package's own built-in directory
(`BUILT_IN_DIRECTORY`, `src/corpuslm/prompts/`) first, then each directory a consumer added with
`register_directory`. A consumer may instead pass `directories=` to one call. The same id and
version in two directories is an error naming both files, so a released version is never
shadowed.

Never imports: db, store, llm, clikit.

Terms:
- **prompt**: one versioned file with front matter (id, version, stage, output schema,
  configuration section) and a body.
"""

from __future__ import annotations

import hashlib
import re
import string
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

BUILT_IN_DIRECTORY = Path(__file__).parent / "prompts"
_REQUIRED = ("id", "version", "stage", "output_schema", "config_section")
_registered: list[Path] = []


class PromptError(ValueError):
    pass


def register_directory(directory: Path) -> None:
    """Add `directory` to the prompts every call searches (once; a repeat is ignored)."""
    directory = Path(directory)
    if directory not in _registered:
        _registered.append(directory)


def search_directories() -> list[Path]:
    """The built-in directory, then each registered one, in the order they are searched."""
    return [BUILT_IN_DIRECTORY, *_registered]


@dataclass(frozen=True, slots=True)
class Prompt:
    id: str
    version: int
    stage: str
    output_schema: str
    config_section: str
    body: str
    path: Path
    sha256: str

    def placeholders(self) -> list[str]:
        return [f for _, f, _, _ in string.Formatter().parse(self.body) if f]

    def render(self, variables: dict[str, str]) -> str:
        missing = [p for p in self.placeholders() if p not in variables]
        if missing:
            raise PromptError(f"prompt {self.id}: missing variables {missing}")
        return self.body.format(**{k: str(v) for k, v in variables.items()})


def _parse(path: Path) -> Prompt:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise PromptError(f"{path}: no TOML front matter")
    _, front, body = raw.split("---\n", 2)
    meta = tomllib.loads(front)
    missing = [k for k in _REQUIRED if k not in meta]
    if missing:
        raise PromptError(f"{path}: front matter missing {missing}")
    unknown = sorted(set(meta) - set(_REQUIRED))
    if unknown:
        raise PromptError(f"{path}: unknown front matter keys {unknown}")
    return Prompt(
        id=str(meta["id"]),
        version=int(meta["version"]),
        stage=str(meta["stage"]),
        output_schema=str(meta["output_schema"]),
        config_section=str(meta["config_section"]),
        body=body.lstrip("\n"),
        path=path,
        sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


_FILENAME = re.compile(r"^(?P<id>.+)_v(?P<version>\d+)\.md$")


def _checked(path: Path) -> Prompt:
    """Parse `path` and decline it unless its name is `<id>_v<version>.md`
    as its front matter says."""
    m = _FILENAME.match(path.name)
    p = _parse(path)
    if m is None or m["id"] != p.id or int(m["version"]) != p.version:
        raise PromptError(
            f"{path.name}: the file name must be {p.id}_v{p.version}.md (id and version "
            "from its front matter); copy to a new version, never rename or edit one"
        )
    return p


def _searched(directories: Iterable[Path] | None) -> list[Path]:
    return search_directories() if directories is None else [Path(d) for d in directories]


def versions(prompt_id: str, directories: Iterable[Path] | None = None) -> dict[int, Path]:
    """Every version file of `prompt_id`, by version number."""
    out: dict[int, Path] = {}
    for directory in _searched(directories):
        for path in directory.glob(f"{prompt_id}_v*.md"):
            m = _FILENAME.match(path.name)
            if not (m and m["id"] == prompt_id):
                continue
            number = int(m["version"])
            if number in out and out[number] != path:
                raise PromptError(
                    f"prompt {prompt_id!r} version {number} is in two places: "
                    f"{out[number]} and {path}"
                )
            out[number] = path
    return out


def load(
    prompt_id: str, version: int | None = None, *, directories: Iterable[Path] | None = None
) -> Prompt:
    """The newest version of `prompt_id`, or exactly `version`. A file stem
    (`cleanup_v2`) is accepted as the id with its version."""
    searched = _searched(directories)
    m = _FILENAME.match(f"{prompt_id}.md")
    if m and version is None and not versions(prompt_id, searched):
        prompt_id, version = m["id"], int(m["version"])
    found = versions(prompt_id, searched)
    if not found:
        raise PromptError(
            f"no prompt named {prompt_id!r} under {', '.join(str(d) for d in searched)}"
        )
    if version is None:
        version = max(found)
    if version not in found:
        raise PromptError(f"prompt {prompt_id!r} has no version {version} (has {sorted(found)})")
    return _checked(found[version])


def list_prompts(directories: Iterable[Path] | None = None) -> list[Prompt]:
    paths = [p for d in _searched(directories) for p in d.glob("*.md")]
    return [_checked(p) for p in sorted(paths, key=lambda p: (p.name, str(p)))]
