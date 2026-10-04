"""Content identity (architecture.md, "History and identity"): the hash a
record carries of what it describes or judged (storage-schema.md, "A
verdict carries the content hash of what it judged")."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def canonical_sha256(obj: Any) -> str:
    """SHA-256 of canonical JSON: keys sorted, no whitespace, UTF-8."""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_span(span: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", span.lower()).strip()


def subject_sha256(*, kind: str, video_id: str, span: str = "", role: str = "") -> str:
    """Identity of what a verdict judged: SHA-256 of canonical JSON with the
    type included and the review key left out (a key only names the slot).
    An ambiguity is its normalized span in its video, so a cosmetic change
    to the span keeps the identity; a speaker question is its role in its
    video."""
    if kind == "ambiguity":
        subject = {"type": "ambiguity", "video_id": video_id, "span": normalize_span(span)}
    elif kind == "speaker":
        subject = {"type": "speaker", "video_id": video_id, "role": role}
    else:
        raise ValueError(f"no subject for a verdict of kind {kind!r}")
    return canonical_sha256(subject)
