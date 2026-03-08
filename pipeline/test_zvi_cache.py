from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from pipeline.zvi_cache import sync_zvi_cache


def _make_entry(title: str, html: str, published_str: str = "2026-03-07") -> dict:
    """Create a fake feedparser entry."""
    dt = datetime.strptime(published_str, "%Y-%m-%d").replace(tzinfo=UTC)
    st = dt.timetuple()
    return {
        "title": title,
        "link": f"https://thezvi.substack.com/p/{title.lower().replace(' ', '-')[:30]}",
        "published_parsed": st,
        "content": [{"value": html}],
    }


def test_sync_essay_creates_single_file(tmp_path):
    """Non-roundup posts are cached as a single file."""
    entry = _make_entry(
        "A Tale of Three Contracts",
        "<p>Essay about contracts.</p><h4>Section One</h4><p>Details here.</p>",
    )
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.zvi_cache.fetch_feed", return_value=feed):
        new_files = sync_zvi_cache(tmp_path)

    assert len(new_files) == 1
    assert "a-tale-of-three-contracts" in new_files[0].name
    content = new_files[0].read_text()
    assert "# A Tale of Three Contracts" in content
    assert "Type: essay" in content
    assert "Essay about contracts" in content


def test_sync_roundup_splits_on_h4(tmp_path):
    """AI #N roundup posts are split into one file per <h4> section."""
    html = (
        "<p>Intro paragraph.</p>"
        "<h4>Deepfakes</h4><p>Deepfake news here.</p>"
        "<h4>Show Me The Money</h4><p>Funding round details.</p>"
    )
    entry = _make_entry("AI #158: The Department of War", html)
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.zvi_cache.fetch_feed", return_value=feed):
        new_files = sync_zvi_cache(tmp_path)

    assert len(new_files) == 2
    names = {f.name for f in new_files}
    assert any("deepfakes" in n for n in names)
    assert any("show-me-the-money" in n for n in names)

    deepfake_file = [f for f in new_files if "deepfakes" in f.name][0]
    content = deepfake_file.read_text()
    assert "# Deepfakes" in content
    assert "Post: AI #158: The Department of War" in content
    assert "Type: roundup-section" in content
    assert "Deepfake news here" in content


def test_sync_is_idempotent(tmp_path):
    """Running sync twice with the same feed does not create duplicates."""
    entry = _make_entry("Some Essay", "<p>Content.</p>")
    feed = MagicMock()
    feed.entries = [entry]

    with (
        patch("pipeline.zvi_cache.fetch_feed", return_value=feed),
        patch(
            "pipeline.zvi_cache.trafilatura.extract", return_value="Extracted content."
        ),
    ):
        first = sync_zvi_cache(tmp_path)
        second = sync_zvi_cache(tmp_path)

    assert len(first) == 1
    assert len(second) == 0


def test_sync_handles_fetch_failure(tmp_path):
    """If the RSS fetch fails, return empty list and don't crash."""
    with patch("pipeline.zvi_cache.fetch_feed", side_effect=Exception("timeout")):
        result = sync_zvi_cache(tmp_path)

    assert result == []
