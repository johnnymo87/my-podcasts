"""Podcast-audio sources whose episodes become "Report:" briefings.

The transcript for these shows is not available to us (The Argument's is
paid-only, see docs/plans/2026-10-18-argument-audio-report-design.md), so the
pipeline transcribes the free public audio itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class PodcastSource:
    key: str
    name: str
    rss_url: str
    feed_slug: str
    title_label: str
    hosts: tuple[str, ...]
    # Automatic discovery ignores items published before this instant. Explicit
    # enqueue (backfill) bypasses it.
    since: datetime


PODCAST_SOURCES: tuple[PodcastSource, ...] = (
    PodcastSource(
        key="argument",
        name="The Argument",
        rss_url="https://audioboom.com/channels/5173020.rss",
        feed_slug="yglesias",
        title_label="The Argument",
        hosts=("Jerusalem Demsas", "Matt Yglesias"),
        since=datetime(2026, 10, 16, tzinfo=UTC),
    ),
)


def get_podcast_source(key: str) -> PodcastSource:
    for source in PODCAST_SOURCES:
        if source.key == key:
            return source
    raise KeyError(key)
