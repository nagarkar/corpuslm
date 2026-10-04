"""The value types that cross the adapter boundary (`src/corpuslm/model.py`;
docs/design/fetching-and-metadata.md (was D2, D4); docs/design/
pipeline-and-surfaces.md, "ytlm owns service adapters…" (was D9)). The
runner decides retry or verdict from `permanent` alone, so each named error
carries the right one."""

import dataclasses

import pytest

from corpuslm.model import (
    AdapterError,
    CaptionTrack,
    NoCaptions,
    NotConfigured,
    Segment,
    TransientError,
    VideoMetadata,
    VideoUnavailable,
)


@pytest.mark.parametrize(
    "error, permanent",
    [
        (NotConfigured, True),
        (NoCaptions, True),
        (VideoUnavailable, True),
        (TransientError, False),
        (AdapterError, False),
    ],
)
def test_each_named_error_says_whether_retrying_is_pointless(error, permanent):
    assert error("x").permanent is permanent
    assert issubclass(error, AdapterError)


def test_a_track_is_frozen_and_its_raw_body_does_not_count_for_equality():
    a = CaptionTrack("v", "en", True, "ytapi", (Segment(0.0, 1.0, "hi"),), raw={"a": 1})
    b = CaptionTrack("v", "en", True, "ytapi", (Segment(0.0, 1.0, "hi"),), raw={"b": 2})
    assert a == b
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.language = "fr"  # type: ignore[misc]


def test_metadata_defaults_to_an_unknown_date():
    """was D4: oEmbed has no date; UNKNOWN is said, never guessed."""
    assert VideoMetadata("v", "t").published_at == "UNKNOWN"
