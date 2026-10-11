"""Schemas: JSON schemas and semantic checks for the files corpuslm owns.

A schema lives in `src/corpuslm/schemas/<name>.json`, every property described. `validate`
checks a document against the schema, then runs the semantic check for that kind (segments
sorted by t0, t1 >= t0, idx equals position, review keys unique, every ambiguity anchored in
its segment). A tool that adds rules of its own (ytlm pins a YouTube video id) passes its
refined schema as `refined_schema=`; the semantic check still runs.

Never imports: db, store, clikit.

Decisions:
- Invalid state is rejected where it is produced and where it is read: the error names the
  property. Enforced by:
  tests/test_transcript.py::test_a_broken_document_is_rejected_naming_the_property.
- Every property carries a description. Enforced by:
  tests/test_transcript.py::test_every_schema_property_has_a_description.

Terms:
- **semantic check**: the rules a JSON schema cannot state (order, positions, anchors).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import cache
from pathlib import Path

SCHEMAS_DIR = Path(__file__).parent / "schemas"


class SchemaError(ValueError):
    pass


def names() -> list[str]:
    return sorted(p.stem for p in SCHEMAS_DIR.glob("*.json"))


@cache
def schema(name: str) -> dict:
    path = SCHEMAS_DIR / f"{name}.json"
    if not path.exists():
        raise SchemaError(f"no schema named {name!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def undescribed(node, path: str = "") -> list[str]:
    """Property paths in a schema that carry no description."""
    out: list[str] = []
    if not isinstance(node, dict):
        return out
    for key, sub in (node.get("properties") or {}).items():
        here = f"{path}.{key}" if path else key
        if not isinstance(sub, dict) or not sub.get("description"):
            out.append(here)
        out += undescribed(sub, here)
    for key in ("items", "additionalProperties"):
        sub = node.get(key)
        if isinstance(sub, dict):
            if "type" in sub and not sub.get("description"):
                out.append(f"{path}[{key}]")
            out += undescribed(sub, f"{path}[{key}]")
    for i, sub in enumerate(node.get("prefixItems") or []):
        if isinstance(sub, dict) and not sub.get("description"):
            out.append(f"{path}[{i}]")
    return out


def validate_against(name: str, schema_document: dict, document) -> None:
    """The schema errors only; the first one, by property path, named in the message."""
    import jsonschema  # the `clean` extra

    validator = jsonschema.Draft202012Validator(schema_document)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        e = errors[0]
        where = ".".join(str(p) for p in e.absolute_path) or "(root)"
        raise SchemaError(f"{name}: {where}: {e.message}")


def validate(name: str, document, *, refined_schema: dict | None = None) -> None:
    """Schema, then the semantic checks for that kind. `refined_schema` replaces the stored
    schema with a tool's refinement of it."""
    validate_against(name, refined_schema if refined_schema is not None else schema(name), document)
    checker = _SEMANTIC.get(name)
    if checker:
        checker(document)


def _check_transcript(t: dict) -> None:
    segments = t["segments"]
    for i, segment in enumerate(segments):
        if segment["idx"] != i:
            raise SchemaError(
                f"transcript: segments[{i}].idx is {segment['idx']}, not its position"
            )
        if segment["t1"] < segment["t0"]:
            raise SchemaError(
                f"transcript: segments[{i}].t1 {segment['t1']} is before t0 {segment['t0']}"
            )
        if i and segment["t0"] < segments[i - 1]["t0"]:
            raise SchemaError(f"transcript: segments are not sorted by t0 at index {i}")
    seen: set[str] = set()
    for a in t.get("ambiguities") or []:
        if a["key"] in seen:
            raise SchemaError(f"transcript: review key {a['key']} is not unique")
        seen.add(a["key"])
        if a["idx"] >= len(segments):
            raise SchemaError(
                f"transcript: {a['key']} names segment {a['idx']} which does not exist"
            )
        text = segments[a["idx"]]["text"]
        needles = [a["span"], a["pick"], *a["candidates"], a.get("resolution", "")]
        if not any(n and n in text for n in needles):
            raise SchemaError(
                f"transcript: {a['key']} has no anchor in segment {a['idx']} "
                f"(neither the span, a candidate nor the resolution appears "
                f"in its text)"
            )


_SEMANTIC: dict[str, Callable[[dict], None]] = {"transcript": _check_transcript}
