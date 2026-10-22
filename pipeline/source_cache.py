from __future__ import annotations

import hashlib
import html as html_mod
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import trafilatura
from google import genai
from google.genai import types
from pydantic import BaseModel

from pipeline.article_resolver import slugify as _slugify
from pipeline.fp_homepage_scraper import scrape_homepage
from pipeline.rss_sources import (
    FP_DIGEST_RSS_SOURCES,
    ROUTING_PENDING,
    SEMAFOR,
    SEMAFOR_ROUTINGS,
    fetch_feed,
)


_HTML_TAG_RE = re.compile(r"<[^>]+>")

logger = logging.getLogger(__name__)


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode entities."""
    return html_mod.unescape(_HTML_TAG_RE.sub("", html)).strip()


def _parse_publish_date(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime(*st[:6], tzinfo=UTC)
    return None


class _ArticleRouting(BaseModel):
    index: int
    routing: str


class _RoutingResult(BaseModel):
    articles: list[_ArticleRouting]


_ROUTING_PROMPT = """\
Classify each article for two daily podcasts.

**FP Digest** (`fp`): foreign policy, geopolitics, military conflicts, diplomacy, \
international relations, wars, sanctions, territorial disputes, international \
humanitarian crises, international organizations.

**The Rundown** (`th`): business, technology, AI, science, culture, entertainment, \
domestic US politics, media, energy markets, startups, corporate news.

**Both** (`both`): articles that clearly span both (e.g., US trade policy, sanctions \
with major business impact, arms deals).

**Skip** (`skip`): publisher self-promotion, event announcements, sponsored content, \
meta-content about the publisher itself.

For each article, return its index and routing.

Articles:
"""


def classify_semafor_articles(
    articles: list[dict],
) -> dict[int, str]:
    """Classify Semafor articles into fp/th/both/skip using Gemini.

    Args:
        articles: list of dicts with 'title' and 'description' keys.

    Returns:
        dict mapping article index to routing string, containing ONLY the
        valid routings (fp/th/both/skip) the model actually gave. An index the
        model skipped, answered with an invalid value, or that is out of range
        is omitted, and the whole dict is ``{}`` on a missing API key, an
        exception, or an unparseable reply. Nothing is invented: a caller leaves
        an unanswered article ``__PENDING__`` and retries it on the next sync,
        instead of stamping 'both' on it for the cache's 180-day retention.
    """
    if not articles:
        return {}

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set; Semafor routing stays pending")
        return {}

    lines = []
    for i, art in enumerate(articles):
        desc = art.get("description", "")
        entry = f"{i}. {art['title']}"
        if desc:
            entry += f" — {desc}"
        lines.append(entry)

    prompt = _ROUTING_PROMPT + "\n".join(lines)

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_RoutingResult,
                temperature=0.1,
            ),
        )
        parsed = response.parsed
        if parsed and parsed.articles:
            result: dict[int, str] = {}
            for art in parsed.articles:
                routing = art.routing.strip().lower()
                if routing in SEMAFOR_ROUTINGS and 0 <= art.index < len(articles):
                    result[art.index] = routing
            return result
    except Exception:
        logger.exception("Gemini Semafor classification failed; routing stays pending")

    return {}


# A file still pending after this long is left to the category fallback (which
# readers already apply): by then the article is older than any lookback window
# (max 14 days), so classifying it would only spend tokens.
PENDING_RECLASSIFY_DAYS = 14
_FILE_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_PENDING_LINE = f"Routing: {ROUTING_PENDING}"
_DESCRIPTION_CHARS = 300


def _pending_semafor_articles(
    cache_dir: Path, exclude: set[Path]
) -> list[tuple[Path, dict]]:
    """Existing cache files still at ``Routing: __PENDING__``, newest window only.

    Only files whose name starts with a ``YYYY-MM-DD`` date within the last
    ``PENDING_RECLASSIFY_DAYS`` days qualify; ``unknown-...`` files are skipped.
    The description is the first 300 chars of the body (the RSS description is
    not stored in the file).
    """
    cutoff = (datetime.now(UTC) - timedelta(days=PENDING_RECLASSIFY_DAYS)).strftime(
        "%Y-%m-%d"
    )
    found: list[tuple[Path, dict]] = []
    for path in sorted(cache_dir.glob("*.md")):
        if path in exclude:
            continue
        match = _FILE_DATE_RE.match(path.name)
        if match is None or match.group(1) < cutoff:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.split("\n", 10)
        if _PENDING_LINE not in lines[:10]:
            continue
        title = " ".join(lines[0].lstrip("# ").split())
        if not title:
            continue
        parts = text.split("\n\n", 2)
        body = parts[2].strip() if len(parts) > 2 else ""
        found.append((path, {"title": title, "description": body[:_DESCRIPTION_CHARS]}))
    return found


def sync_semafor_cache(cache_dir: Path) -> list[Path]:
    """Fetch Semafor RSS and cache all articles. Returns newly-written paths.

    New files are written with ``Routing: __PENDING__`` and classified together
    with older files still pending (see ``_pending_semafor_articles``), so a
    classification that failed once (Gemini outage, 402) is retried on the next
    sync instead of sticking. Only files the model answered are patched; the
    rest stay pending, and readers route those by category meanwhile.
    """
    try:
        feed = fetch_feed(SEMAFOR.feed_url)
    except Exception as e:
        print(f"[source_cache] Failed to fetch Semafor RSS: {e}")
        return []

    cache_dir.mkdir(parents=True, exist_ok=True)
    new_files: list[Path] = []
    new_articles: list[dict] = []

    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue

        pub_date = _parse_publish_date(entry)
        date_str = pub_date.strftime("%Y-%m-%d") if pub_date else "unknown"
        url = (entry.get("link") or "").strip()

        category = ""
        if entry.get("tags"):
            category = entry["tags"][0].get("term", "")

        description = (entry.get("summary") or "").strip()

        content_html = (
            entry.get("content", [{}])[0].get("value", "")
            if entry.get("content")
            else ""
        )
        # Semafor entries normally carry <content>, but when they don't, the
        # description is the body — and it arrives HTML-encoded. Strip and
        # unescape it the same way, or entities reach the writer prompt
        # verbatim (the identical defect fixed below for antiwar RSS).
        text = _strip_html(content_html or description)

        slug = _slugify(title)
        filename = f"{date_str}-{slug}.md"
        path = cache_dir / filename
        if path.exists():
            continue

        md = (
            f"# {title}\n\n"
            f"URL: {url}\n"
            f"Published: {date_str}\n"
            f"Source: semafor\n"
            f"Category: {category}\n"
            f"{_PENDING_LINE}\n"
            f"Type: article\n\n"
            f"{text}"
        )
        path.write_text(md, encoding="utf-8")
        new_files.append(path)
        new_articles.append({"title": title, "description": description})

    # Classify the new articles and any recent ones still pending, and patch
    # routing into the files the model answered.
    to_classify = list(zip(new_files, new_articles, strict=True))
    to_classify += _pending_semafor_articles(cache_dir, exclude=set(new_files))
    if to_classify:
        routings = classify_semafor_articles([art for _, art in to_classify])
        for i, (path, _) in enumerate(to_classify):
            routing = routings.get(i)
            if routing is None:
                continue
            try:
                content = path.read_text(encoding="utf-8")
                content = content.replace(_PENDING_LINE, f"Routing: {routing}", 1)
                path.write_text(content, encoding="utf-8")
            except OSError:
                logger.exception("could not patch Semafor routing into %s", path)

    return new_files


def sync_antiwar_rss_cache(cache_dir: Path) -> list[Path]:
    """Fetch all FP Digest RSS feeds and cache articles. Returns newly-written paths."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    new_files: list[Path] = []

    for source in FP_DIGEST_RSS_SOURCES:
        try:
            feed = fetch_feed(source.feed_url)
        except Exception as e:
            print(f"[source_cache] Failed to fetch {source.name} RSS: {e}")
            continue

        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            if not title:
                continue

            pub_date = _parse_publish_date(entry)
            date_str = pub_date.strftime("%Y-%m-%d") if pub_date else "unknown"
            url = (entry.get("link") or "").strip()

            content_html = (
                entry.get("content", [{}])[0].get("value", "")
                if entry.get("content")
                else ""
            )
            summary = (entry.get("summary") or "").strip()
            # Antiwar's feeds ship no <content>, so the summary is the body —
            # and it arrives HTML-encoded. Strip and unescape it the same way,
            # or entities reach the writer prompt verbatim.
            text = _strip_html(content_html or summary)

            slug = _slugify(title)
            filename = f"{date_str}-{source.name}-{slug}.md"
            path = cache_dir / filename
            if path.exists():
                continue

            md = (
                f"# {title}\n\n"
                f"URL: {url}\n"
                f"Published: {date_str}\n"
                f"Source: {source.name}\n"
                f"Type: article\n\n"
                f"{text}"
            )
            path.write_text(md, encoding="utf-8")
            new_files.append(path)

    return new_files


def _extract_homepage_text(url: str) -> str:
    """Fetch and extract article text from a URL."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(
                downloaded, include_comments=False, include_tables=False
            )
            return (text or "").strip()
    except Exception as e:
        print(f"[source_cache] Failed to extract text from {url}: {e}")
    return ""


def sync_antiwar_homepage_cache(cache_dir: Path) -> list[Path]:
    """Scrape antiwar.com homepage and cache all linked articles.

    Returns newly-written paths.
    """
    try:
        links = scrape_homepage()
    except Exception as e:
        print(f"[source_cache] Failed to scrape antiwar homepage: {e}")
        return []

    cache_dir.mkdir(parents=True, exist_ok=True)
    new_files: list[Path] = []
    # Homepage links carry no publication date; use scrape date as proxy
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    for link in links:
        # Normalize whitespace in headline (scraper may return multi-line text)
        headline = " ".join(link.headline.split())
        slug = _slugify(headline)
        url_hash = hashlib.md5(link.url.encode()).hexdigest()[:8]  # noqa: S324
        filename = f"{today}-{slug}-{url_hash}.md"
        path = cache_dir / filename
        if path.exists():
            continue

        text = _extract_homepage_text(link.url)

        md = (
            f"# {headline}\n\n"
            f"URL: {link.url}\n"
            f"Published: {today}\n"
            f"Region: {link.region}\n"
            f"Source: antiwar-homepage\n"
            f"Type: article\n\n"
            f"{text}"
        )
        path.write_text(md, encoding="utf-8")
        new_files.append(path)

    return new_files
