from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable
from urllib.parse import urlencode

import feedparser
import requests
import trafilatura


@dataclass(frozen=True)
class RssSource:
    name: str
    feed_url: str
    wp_search_base: str | None = None


@dataclass(frozen=True)
class RssResult:
    source: str
    title: str
    url: str
    published: datetime | None
    score: float
    text: str


DEFAULT_SOURCES: list[RssSource] = [
    RssSource(
        name="antiwar_news",
        feed_url="https://news.antiwar.com/feed/",
        wp_search_base="https://news.antiwar.com/",
    ),
    RssSource(
        name="caitlinjohnstone",
        feed_url="https://caitlinjohnstone.com.au/feed/",
        wp_search_base="https://caitlinjohnstone.com.au/",
    ),
]

FP_SOURCES = DEFAULT_SOURCES

ANTIWAR_ORIGINAL = RssSource(
    name="antiwar_original",
    feed_url="https://original.antiwar.com/feed/",
    wp_search_base="https://original.antiwar.com/",
)

ANTIWAR_BLOG = RssSource(
    name="antiwar_blog",
    feed_url="https://feeds.feedburner.com/AWCBlog",
    wp_search_base="https://www.antiwar.com/blog/",
)

FP_DIGEST_RSS_SOURCES: list[RssSource] = [
    *DEFAULT_SOURCES,  # antiwar_news + caitlinjohnstone
    ANTIWAR_ORIGINAL,
    ANTIWAR_BLOG,
]

AI_SOURCES: list[RssSource] = [
    RssSource(
        name="zvi_substack",
        feed_url="https://thezvi.substack.com/feed",
        wp_search_base=None,  # Substack RSS ignores ?s=, relies on fallback keyword scoring
    ),
]

SEMAFOR = RssSource(
    name="semafor",
    feed_url="https://www.semafor.com/rss.xml",
    wp_search_base=None,
)

_SEMAFOR_FP_CATEGORIES = {"africa", "gulf", "security"}
_SEMAFOR_TH_CATEGORIES = {"business", "technology", "media", "ceo", "energy"}


def categorize_semafor_article(category: str) -> str:
    """Return 'fp', 'th', or 'both' based on Semafor category tag."""
    cat = category.strip().lower()
    if cat in _SEMAFOR_FP_CATEGORIES:
        return "fp"
    if cat in _SEMAFOR_TH_CATEGORIES:
        return "th"
    return "both"


_WORD_RE = re.compile(r"[a-z0-9']+")
_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    global _SESSION  # noqa: PLW0603
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({"User-Agent": "podcast-pipeline/1.0"})
    return _SESSION


def _fetch_feed(url: str) -> feedparser.FeedParserDict:
    """Fetch RSS feed via requests (avoids feedparser SSL issues)."""
    resp = _get_session().get(url, timeout=15)
    resp.raise_for_status()
    return feedparser.parse(resp.text)


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _keyword_score(query: str, title: str, summary: str) -> float:
    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0
    t_tokens = set(_tokenize(title))
    s_tokens = set(_tokenize(summary))
    idf = 1.0 / math.sqrt(len(q_tokens))
    score = 0.0
    for tok in q_tokens:
        if tok in t_tokens:
            score += 3.0 * idf
        if tok in s_tokens:
            score += 1.0 * idf
    return score


def _parse_entry_datetime(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(time.mktime(st), tz=UTC)
    return None


def _extract_text(url: str) -> str:
    """Fetch article and extract main text via trafilatura."""
    try:
        resp = _get_session().get(url, timeout=15)
        resp.raise_for_status()
        text = trafilatura.extract(
            resp.content,
            url=url,
            include_comments=False,
            include_tables=False,
            include_formatting=False,
            favor_precision=True,
        )
        return (text or "").strip()
    except Exception:
        return ""


def search_rss_sources(
    query: str,
    *,
    sources: Iterable[RssSource] | None = None,
    days_back: int = 10,
    top_k: int = 3,
    min_score: float = 0.2,
    fetch_text: bool = True,
) -> list[RssResult]:
    """Search independent FP sources via RSS for articles matching a query.

    Strategy:
    1. Try WordPress search feed (?s=query&feed=rss2) for server-side filtering.
    2. Fall back to scanning the main RSS feed with local keyword scoring.
    3. Fetch full article text via trafilatura for top results.
    """
    if sources is None:
        sources = DEFAULT_SOURCES

    since = datetime.now(tz=UTC) - timedelta(days=days_back)
    hits: list[RssResult] = []

    for src in sources:
        entries: list[dict] = []

        # Strategy 1: WordPress search feed
        if src.wp_search_base:
            search_url = (
                src.wp_search_base.rstrip("/")
                + "/?"
                + urlencode({"s": query, "feed": "rss2"})
            )
            try:
                feed = _fetch_feed(search_url)
                entries = list(feed.entries)
            except Exception:
                entries = []

        # Strategy 2: Fall back to main feed + keyword scoring
        if not entries:
            try:
                feed = _fetch_feed(src.feed_url)
                entries = list(feed.entries)
            except Exception:
                continue

        for entry in entries:
            published = _parse_entry_datetime(entry)
            if published and published < since:
                continue

            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            summary = (entry.get("summary") or entry.get("description") or "").strip()

            if not title or not link:
                continue

            # WP search already filtered, so give a base score of 1.0;
            # for main-feed fallback, score by keyword overlap.
            score = max(1.0, _keyword_score(query, title, summary))

            hits.append(
                RssResult(
                    source=src.name,
                    title=title,
                    url=link,
                    published=published,
                    score=score,
                    text="",  # filled below if fetch_text
                )
            )

    # Sort by score descending, then recency
    hits.sort(
        key=lambda h: (h.score, h.published or datetime(1970, 1, 1, tzinfo=UTC)),
        reverse=True,
    )
    hits = hits[:top_k]

    # Fetch full text for top results
    if fetch_text:
        enriched: list[RssResult] = []
        for hit in hits:
            text = _extract_text(hit.url)
            enriched.append(
                RssResult(
                    source=hit.source,
                    title=hit.title,
                    url=hit.url,
                    published=hit.published,
                    score=hit.score,
                    text=text,
                )
            )
        hits = enriched

    return hits
