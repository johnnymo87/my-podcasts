"""Parse a podcast RSS feed into items keyed by GUID (never enclosure URL:
audioboom's carries mutable tracking query strings)."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


_ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


@dataclass(frozen=True)
class PodcastItem:
    guid: str
    title: str
    link: str
    pub_date: datetime
    duration_s: int | None
    enclosure_url: str
    description: str


def _duration(text: str | None) -> int | None:
    if not text:
        return None
    text = text.strip()
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    if all(p.isdigit() for p in parts) and 1 < len(parts) <= 3:
        total = 0
        for p in parts:
            total = total * 60 + int(p)
        return total
    return None


def _plain(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def parse_podcast_feed(rss_xml: str) -> list[PodcastItem]:
    root = ET.fromstring(rss_xml)
    items: list[PodcastItem] = []
    for it in root.iter("item"):
        guid = (it.findtext("guid") or "").strip()
        enclosure = it.find("enclosure")
        url = enclosure.get("url", "").strip() if enclosure is not None else ""
        raw_date = it.findtext("pubDate")
        if not guid or not url or not raw_date:
            continue
        try:
            pub = parsedate_to_datetime(raw_date).astimezone(UTC)
        except (TypeError, ValueError):
            continue
        items.append(
            PodcastItem(
                guid=guid,
                title=(it.findtext("title") or "").strip(),
                link=(it.findtext("link") or "").strip(),
                pub_date=pub,
                duration_s=_duration(it.findtext(f"{_ITUNES}duration")),
                enclosure_url=url,
                description=_plain(it.findtext("description") or ""),
            )
        )
    return items


def eligible_items(
    items: list[PodcastItem], *, since: datetime, now: datetime
) -> list[PodcastItem]:
    """Items with since <= pub_date <= now, oldest first."""
    return sorted(
        (i for i in items if since <= i.pub_date <= now), key=lambda i: i.pub_date
    )
