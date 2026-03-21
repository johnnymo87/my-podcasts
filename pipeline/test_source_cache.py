from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from pipeline.__main__ import cli
from pipeline.fp_homepage_scraper import HomepageLink
from pipeline.source_cache import (
    classify_semafor_articles,
    sync_antiwar_homepage_cache,
    sync_antiwar_rss_cache,
    sync_semafor_cache,
)


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


def _make_rss_entry(
    title: str, source_url: str, published_str: str = "2026-03-07"
) -> dict:
    """Create a fake feedparser entry for antiwar/CJ RSS."""
    dt = datetime.strptime(published_str, "%Y-%m-%d").replace(tzinfo=UTC)
    st = dt.timetuple()
    return {
        "title": title,
        "link": source_url,
        "published_parsed": st,
        "summary": f"Summary of {title}",
        "content": [{"value": f"<p>Full content about {title}.</p>"}],
    }


def test_sync_antiwar_rss_creates_files(tmp_path):
    """Antiwar RSS articles cached with source metadata."""
    entry = _make_rss_entry("US Troops Deploy", "https://news.antiwar.com/story1")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        new_files = sync_antiwar_rss_cache(tmp_path)

    assert len(new_files) == 4  # one file per source in FP_DIGEST_RSS_SOURCES
    content = new_files[0].read_text()
    assert "# US Troops Deploy" in content
    assert "Source: antiwar_news" in content


def test_sync_antiwar_rss_is_idempotent(tmp_path):
    """Running sync twice does not create duplicates."""
    entry = _make_rss_entry("Troops Story", "https://news.antiwar.com/story1")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        first = sync_antiwar_rss_cache(tmp_path)
        second = sync_antiwar_rss_cache(tmp_path)

    assert len(first) == 4
    assert len(second) == 0


def test_sync_antiwar_rss_handles_partial_failure(tmp_path):
    """A failing source is skipped; other sources still produce files."""
    good_entry = _make_rss_entry("Good Story", "https://news.antiwar.com/s1")
    good_feed = MagicMock()
    good_feed.entries = [good_entry]
    call_count = 0

    def _side_effect(url):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return good_feed
        raise Exception("timeout")

    with patch("pipeline.source_cache.fetch_feed", side_effect=_side_effect):
        result = sync_antiwar_rss_cache(tmp_path)

    assert len(result) == 1  # first source succeeded, others failed


def test_sync_homepage_creates_files(tmp_path):
    """Homepage links cached with region metadata."""
    links = [
        HomepageLink(
            region="Middle East",
            headline="Iran Tensions Rise",
            url="https://example.com/iran",
        ),
        HomepageLink(
            region="Europe", headline="NATO Expands", url="https://example.com/nato"
        ),
    ]

    with (
        patch("pipeline.source_cache.scrape_homepage", return_value=links),
        patch(
            "pipeline.source_cache._extract_homepage_text",
            return_value="Article body text.",
        ),
    ):
        new_files = sync_antiwar_homepage_cache(tmp_path)

    assert len(new_files) == 2
    content = new_files[0].read_text()
    assert "Region:" in content
    assert "Source: antiwar-homepage" in content
    assert "Article body text." in content


def test_sync_homepage_is_idempotent(tmp_path):
    """Running sync twice does not create duplicates."""
    links = [
        HomepageLink(region="Africa", headline="Story One", url="https://example.com/1")
    ]

    with (
        patch("pipeline.source_cache.scrape_homepage", return_value=links),
        patch("pipeline.source_cache._extract_homepage_text", return_value="Text."),
    ):
        first = sync_antiwar_homepage_cache(tmp_path)
        second = sync_antiwar_homepage_cache(tmp_path)

    assert len(first) == 1
    assert len(second) == 0


def test_sync_homepage_handles_scrape_failure(tmp_path):
    """If homepage scrape fails, return empty list."""
    with patch(
        "pipeline.source_cache.scrape_homepage", side_effect=Exception("timeout")
    ):
        result = sync_antiwar_homepage_cache(tmp_path)
    assert result == []


def test_sync_sources_cli():
    """The sync-sources command calls all four sync functions."""
    with (
        patch("pipeline.__main__.sync_zvi_cache") as mock_zvi,
        patch("pipeline.__main__.sync_semafor_cache") as mock_semafor,
        patch("pipeline.__main__.sync_antiwar_rss_cache") as mock_rss,
        patch("pipeline.__main__.sync_antiwar_homepage_cache") as mock_homepage,
    ):
        mock_zvi.return_value = []
        mock_semafor.return_value = []
        mock_rss.return_value = []
        mock_homepage.return_value = []

        runner = CliRunner()
        result = runner.invoke(cli, ["sync-sources"])

    assert result.exit_code == 0
    mock_zvi.assert_called_once()
    mock_semafor.assert_called_once()
    mock_rss.assert_called_once()
    mock_homepage.assert_called_once()


def test_classify_semafor_articles_returns_routing(monkeypatch):
    """LLM classifier returns routing for each article."""
    articles = [
        {
            "title": "Hungary blocks EU loan to Ukraine",
            "description": "Orban demands Russian oil access.",
        },
        {
            "title": "Nvidia's Claw Bar was a pricey wakeup call",
            "description": "A demo at GTC was really a sales pitch.",
        },
        {"title": "Iran war reshapes Gulf diplomacy", "description": ""},
    ]

    mock_parsed = MagicMock()
    mock_parsed.articles = [
        MagicMock(index=0, routing="fp"),
        MagicMock(index=1, routing="th"),
        MagicMock(index=2, routing="fp"),
    ]
    mock_response = MagicMock()
    mock_response.parsed = mock_parsed

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        result = classify_semafor_articles(articles)

    assert result == {0: "fp", 1: "th", 2: "fp"}
    call_args = mock_client.models.generate_content.call_args
    assert "gemini-3.1-flash-lite-preview" in str(call_args)


def test_classify_semafor_articles_fallback_no_api_key(monkeypatch):
    """Without GEMINI_API_KEY, all articles get 'both'."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    articles = [
        {"title": "Some Article", "description": "Desc."},
        {"title": "Another Article", "description": ""},
    ]
    result = classify_semafor_articles(articles)
    assert result == {0: "both", 1: "both"}


def test_classify_semafor_articles_fallback_on_error(monkeypatch):
    """On Gemini API error, all articles get 'both'."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value.models.generate_content.side_effect = Exception(
            "API down"
        )
        result = classify_semafor_articles([{"title": "Test", "description": ""}])

    assert result == {0: "both"}


def test_classify_semafor_articles_empty_list():
    """Empty input returns empty dict."""
    result = classify_semafor_articles([])
    assert result == {}


def test_classify_semafor_articles_normalises_unknown_routing(monkeypatch):
    """LLM returning an unknown routing string falls back to 'both'."""
    mock_parsed = MagicMock()
    mock_parsed.articles = [
        MagicMock(index=0, routing="UNKNOWN_VALUE"),
    ]
    mock_response = MagicMock()
    mock_response.parsed = mock_parsed

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        result = classify_semafor_articles([{"title": "Test", "description": ""}])

    assert result == {0: "both"}


def test_classify_semafor_articles_fills_missing_indices(monkeypatch):
    """LLM omitting some indices gets them filled with 'both'."""
    mock_parsed = MagicMock()
    mock_parsed.articles = [
        MagicMock(index=0, routing="fp"),
        # index 1 missing
    ]
    mock_response = MagicMock()
    mock_response.parsed = mock_parsed

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        result = classify_semafor_articles(
            [
                {"title": "Article A", "description": ""},
                {"title": "Article B", "description": ""},
            ]
        )

    assert result == {0: "fp", 1: "both"}
