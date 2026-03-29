from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BlogSource:
    name: str
    feed_url: str
    feed_slug: str
    tts_voice: str
    category: str


BLOG_SOURCES: tuple[BlogSource, ...] = (
    BlogSource(
        name="Scott Aaronson - Shtetl-Optimized",
        feed_url="https://scottaaronson.blog/?feed=rss2",
        feed_slug="aaronson",
        tts_voice="fable",
        category="Technology",
    ),
)
