from __future__ import annotations

import hashlib
import html as html_mod
import re
from datetime import UTC, datetime
from pathlib import Path

import trafilatura

from pipeline.fp_homepage_scraper import scrape_homepage
from pipeline.rss_sources import FP_DIGEST_RSS_SOURCES, SEMAFOR, fetch_feed


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _slugify(text: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:50]


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode entities."""
    return html_mod.unescape(_HTML_TAG_RE.sub("", html)).strip()


def _parse_publish_date(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime(*st[:6], tzinfo=UTC)
    return None


def sync_semafor_cache(cache_dir: Path) -> list[Path]:
    """Fetch Semafor RSS and cache all articles. Returns newly-written paths."""
    try:
        feed = fetch_feed(SEMAFOR.feed_url)
    except Exception as e:
        print(f"[source_cache] Failed to fetch Semafor RSS: {e}")
        return []

    cache_dir.mkdir(parents=True, exist_ok=True)
    new_files: list[Path] = []

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

        content_html = (
            entry.get("content", [{}])[0].get("value", "")
            if entry.get("content")
            else ""
        )
        text = (
            _strip_html(content_html)
            if content_html
            else (entry.get("summary") or "").strip()
        )

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
            f"Type: article\n\n"
            f"{text}"
        )
        path.write_text(md, encoding="utf-8")
        new_files.append(path)

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
            text = _strip_html(content_html) if content_html else summary

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
