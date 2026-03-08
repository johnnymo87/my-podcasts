from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from pipeline.source_cache import sync_semafor_cache


def _make_semafor_entry(
    title: str, category: str, published_str: str = "2026-03-07"
) -> dict:
    """Create a fake feedparser entry for Semafor."""
    import feedparser

    dt = datetime.strptime(published_str, "%Y-%m-%d").replace(tzinfo=UTC)
    st = dt.timetuple()
    return feedparser.FeedParserDict(
        {
            "title": title,
            "link": f"https://semafor.com/{title.lower().replace(' ', '-')[:30]}",
            "published_parsed": st,
            "summary": f"Summary of {title}",
            "tags": [feedparser.FeedParserDict({"term": category})],
            "content": [{"value": f"<p>Full content about {title}.</p>"}],
        }
    )


def test_sync_semafor_creates_files(tmp_path):
    """Semafor articles are cached as markdown files with category metadata."""
    entry = _make_semafor_entry("Tech Company IPO", "Technology")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        new_files = sync_semafor_cache(tmp_path)

    assert len(new_files) == 1
    content = new_files[0].read_text()
    assert "# Tech Company IPO" in content
    assert "Category: Technology" in content
    assert "Source: semafor" in content


def test_sync_semafor_is_idempotent(tmp_path):
    """Running sync twice does not create duplicates."""
    entry = _make_semafor_entry("Tech Story", "Business")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        first = sync_semafor_cache(tmp_path)
        second = sync_semafor_cache(tmp_path)

    assert len(first) == 1
    assert len(second) == 0


def test_sync_semafor_handles_fetch_failure(tmp_path):
    """If RSS fetch fails, return empty list."""
    with patch("pipeline.source_cache.fetch_feed", side_effect=Exception("timeout")):
        result = sync_semafor_cache(tmp_path)
    assert result == []
