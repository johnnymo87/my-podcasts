from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NewsletterPreset:
    name: str
    route_tags: tuple[str, ...]
    tts_model: str
    tts_voice: str
    category: str
    feed_slug: str


PRESETS: tuple[NewsletterPreset, ...] = (
    NewsletterPreset(
        name="Matt Levine - Money Stuff",
        route_tags=("levine", "money-stuff", "moneystuff", "bloomberg"),
        tts_model="tts-1-hd",
        tts_voice="ash",
        category="Business",
        feed_slug="levine",
    ),
    NewsletterPreset(
        name="Yglesias Substack",
        route_tags=("yglesias", "slowboring", "substack-yglesias"),
        tts_model="tts-1-hd",
        tts_voice="sage",
        category="News",
        feed_slug="yglesias",
    ),
    NewsletterPreset(
        name="Nate Silver - Silver Bulletin",
        route_tags=("silver", "natesilver", "silverbulletin"),
        tts_model="tts-1-hd",
        tts_voice="echo",
        category="News",
        feed_slug="silver",
    ),
)


DEFAULT_PRESET = NewsletterPreset(
    name="General Newsletter",
    route_tags=(),
    tts_model="tts-1-hd",
    tts_voice="ash",
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
