"""Shared value types crossing the adapter boundary (D2, D4, D9)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Segment:
    """One caption cue. Times in seconds."""

    t0: float
    t1: float
    text: str


@dataclass(frozen=True, slots=True)
class CaptionTrack:
    video_id: str
    language: str  # ISO 639-1 of the track fetched
    generated: bool | None  # auto vs creator captions; None when the fetcher cannot say
    fetcher: str  # provenance: supadata | ytapi
    segments: tuple[Segment, ...]
    raw: dict = field(default_factory=dict, compare=False)  # as fetched, for the file


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    video_id: str
    title: str
    channel_id: str = ""
    channel_title: str = ""
    published_at: str = "UNKNOWN"  # ISO 8601 or UNKNOWN (D4)
    duration_seconds: int | None = None
    description: str = ""
    tags: tuple[str, ...] = ()
    thumbnail_url: str = ""
    language: str = ""
    has_caption_track: bool | None = None
    source: str = ""  # dataapi | oembed
    raw: dict = field(default_factory=dict, compare=False)


class AdapterError(RuntimeError):
    """Base for adapter failures. `permanent` says whether retrying is pointless."""

    permanent = False


class NotConfigured(AdapterError):
    """The adapter's key is missing; skip it (recorded, never fatal)."""

    permanent = True


class NoCaptions(AdapterError):
    """The video has no caption track this fetcher can see."""

    permanent = True


class VideoUnavailable(AdapterError):
    permanent = True


class TransientError(AdapterError):
    permanent = False
