from __future__ import annotations

import html as html_mod
import re
from datetime import UTC, datetime
from pathlib import Path

import trafilatura

from pipeline.rss_sources import fetch_feed


_ZVI_FEED_URL = "https://thezvi.substack.com/feed"
_ROUNDUP_RE = re.compile(r"AI\s*#\d+", re.IGNORECASE)
_H4_SPLIT_RE = re.compile(r"<h4[^>]*>(.*?)</h4>", re.DOTALL | re.IGNORECASE)
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


def _split_roundup_sections(html: str) -> list[tuple[str, str]]:
    """Split roundup HTML on <h4> tags. Returns [(section_title, section_text), ...]."""
    matches = list(_H4_SPLIT_RE.finditer(html))
    if not matches:
        return []

    sections = []
    for i, match in enumerate(matches):
        title = _strip_html(match.group(1))
        if not title or title.lower() == "table of contents":
            continue
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        section_html = html[start:end]
        text = _strip_html(section_html)
        if text:
            sections.append((title, text))
    return sections


def _extract_essay_text(html: str) -> str:
    """Extract full text from an essay post."""
    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        include_formatting=False,
        favor_precision=True,
    )
    return (text or "").strip()


def sync_zvi_cache(cache_dir: Path) -> list[Path]:
    """Fetch Zvi RSS and cache any new posts. Returns list of newly-written paths."""
    try:
        feed = fetch_feed(_ZVI_FEED_URL)
    except Exception as e:
        print(f"[zvi_cache] Failed to fetch Zvi RSS: {e}")
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
        html = (
            entry.get("content", [{}])[0].get("value", "")
            if entry.get("content")
            else ""
        )

        post_slug = _slugify(title)

        if _ROUNDUP_RE.search(title):
            sections = _split_roundup_sections(html)
            if not sections:
                print(f"[zvi_cache] roundup '{title}' yielded 0 sections, skipping")
                continue
            for section_title, section_text in sections:
                section_slug = _slugify(section_title)
                filename = f"{date_str}-{post_slug}--{section_slug}.md"
                path = cache_dir / filename
                if path.exists():
                    continue
                content = (
                    f"# {section_title}\n\n"
                    f"Post: {title}\n"
                    f"URL: {url}\n"
                    f"Published: {date_str}\n"
                    f"Type: roundup-section\n\n"
                    f"{section_text}"
                )
                path.write_text(content, encoding="utf-8")
                new_files.append(path)
        else:
            filename = f"{date_str}-{post_slug}.md"
            path = cache_dir / filename
            if path.exists():
                continue
            text = _extract_essay_text(html)
            if not text:
                print(f"[zvi_cache] no text extracted for essay '{title}', skipping")
                continue
            content = (
                f"# {title}\n\n"
                f"Post: {title}\n"
                f"URL: {url}\n"
                f"Published: {date_str}\n"
                f"Type: essay\n\n"
                f"{text}"
            )
            path.write_text(content, encoding="utf-8")
            new_files.append(path)

    return new_files
