from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NewsletterPreset:
    name: str
    route_tags: tuple[str, ...]
    category: str
    feed_slug: str


PRESETS: tuple[NewsletterPreset, ...] = (
    NewsletterPreset(
        name="Matt Levine - Money Stuff",
        route_tags=("levine", "money-stuff", "moneystuff", "bloomberg"),
        category="Business",
        feed_slug="levine",
    ),
    NewsletterPreset(
        name="Yglesias Substack",
        route_tags=("yglesias", "slowboring", "substack-yglesias"),
        category="News",
        feed_slug="yglesias",
    ),
    NewsletterPreset(
        name="Nate Silver - Silver Bulletin",
        route_tags=("silver", "natesilver", "silverbulletin"),
        category="News",
        feed_slug="silver",
    ),
    NewsletterPreset(
        name="The Rundown",
        route_tags=("the-rundown",),
        category="News",
        feed_slug="the-rundown",
    ),
    NewsletterPreset(
        name="Foreign Policy Digest",
        route_tags=("fp-digest",),
        category="News",
        feed_slug="fp-digest",
    ),
    NewsletterPreset(
        name="Scott Aaronson - Shtetl-Optimized",
        route_tags=("aaronson", "shtetl-optimized"),
        category="Technology",
        feed_slug="aaronson",
    ),
    NewsletterPreset(
        name="ChinaTalk",
        route_tags=("chinatalk",),
        category="News",
        feed_slug="chinatalk",
    ),
)


DEFAULT_PRESET = NewsletterPreset(
    name="General Newsletter",
    route_tags=(),
    category="News",
    feed_slug="general",
)


def resolve_preset(route_tag: str | None) -> NewsletterPreset:
    if not route_tag:
        return DEFAULT_PRESET
    normalized = route_tag.strip().lower()
    for preset in PRESETS:
        if normalized in preset.route_tags:
            return preset
    return DEFAULT_PRESET
