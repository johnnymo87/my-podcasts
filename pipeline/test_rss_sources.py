from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from pipeline.rss_sources import (
    SEMAFOR,
    RssResult,
    RssSource,
    _keyword_score,
    _parse_entry_datetime,
    categorize_semafor_article,
    search_rss_sources,
)


class TestKeywordScore:
    def test_matching_title_scores_higher(self) -> None:
        score = _keyword_score("Iran drone attack", "Iran drone attack", "")
        assert score > 0

    def test_no_match_returns_zero(self) -> None:
        score = _keyword_score("Iran drone attack", "cooking recipes", "baking tips")
        assert score == 0.0

    def test_title_weighted_more_than_summary(self) -> None:
        title_score = _keyword_score("Iran", "Iran conflict", "")
        summary_score = _keyword_score("Iran", "no match", "Iran conflict")
        assert title_score > summary_score


class TestParseEntryDatetime:
    def test_published_parsed(self) -> None:
        entry = {"published_parsed": (2026, 3, 3, 12, 0, 0, 0, 62, 0)}
        result = _parse_entry_datetime(entry)
        assert result is not None
        assert result.year == 2026

    def test_missing_returns_none(self) -> None:
        assert _parse_entry_datetime({}) is None


class TestSearchRssSources:
    @patch("pipeline.rss_sources._extract_text", return_value="Article body here")
    @patch("pipeline.rss_sources._fetch_feed")
    def test_wp_search_feed_used_first(
        self, mock_fetch: MagicMock, mock_extract: MagicMock
    ) -> None:
        """When WP search feed returns results, use them."""
        now = datetime.now(tz=UTC)
        mock_feed = MagicMock()
        mock_feed.entries = [
            {
                "title": "Iran War Update",
                "link": "https://example.com/iran-war",
                "summary": "Latest on the Iran conflict",
                "published_parsed": now.timetuple(),
            }
        ]
        mock_fetch.return_value = mock_feed

        sources = [
            RssSource(
                name="test",
                feed_url="https://example.com/feed/",
                wp_search_base="https://example.com/",
            )
        ]
        results = search_rss_sources("Iran war", sources=sources, top_k=5)

        assert len(results) == 1
        assert results[0].title == "Iran War Update"
        assert results[0].source == "test"
        assert results[0].text == "Article body here"
        # WP search URL should have been called
        call_url = mock_fetch.call_args_list[0][0][0]
        assert "s=Iran+war" in call_url
        assert "feed=rss2" in call_url

    @patch("pipeline.rss_sources._extract_text", return_value="")
    @patch("pipeline.rss_sources._fetch_feed")
    def test_falls_back_to_main_feed(
        self, mock_fetch: MagicMock, mock_extract: MagicMock
    ) -> None:
        """When WP search returns empty, fall back to main feed."""
        now = datetime.now(tz=UTC)
        empty_feed = MagicMock()
        empty_feed.entries = []

        main_feed = MagicMock()
        main_feed.entries = [
            {
                "title": "Iran drone strike update",
                "link": "https://example.com/drone",
                "summary": "Drone strike details",
                "published_parsed": now.timetuple(),
            },
            {
                "title": "Cooking recipes",
                "link": "https://example.com/cooking",
                "summary": "Best pasta ever",
                "published_parsed": now.timetuple(),
            },
        ]
        mock_fetch.side_effect = [empty_feed, main_feed]

        sources = [
            RssSource(
                name="test",
                feed_url="https://example.com/feed/",
                wp_search_base="https://example.com/",
            )
        ]
        results = search_rss_sources(
            "Iran drone", sources=sources, top_k=5, min_score=0.2
        )

        # Only the matching article should appear
        assert len(results) >= 1
        assert results[0].title == "Iran drone strike update"

    @patch("pipeline.rss_sources._extract_text", return_value="")
    @patch("pipeline.rss_sources._fetch_feed")
    def test_old_entries_filtered_out(
        self, mock_fetch: MagicMock, mock_extract: MagicMock
    ) -> None:
        """Entries older than days_back are excluded."""
        old = datetime.now(tz=UTC) - timedelta(days=30)
        mock_feed = MagicMock()
        mock_feed.entries = [
            {
                "title": "Iran war old article",
                "link": "https://example.com/old",
                "summary": "Old news",
                "published_parsed": old.timetuple(),
            }
        ]
        mock_fetch.return_value = mock_feed

        sources = [
            RssSource(
                name="test",
                feed_url="https://example.com/feed/",
                wp_search_base="https://example.com/",
            )
        ]
        results = search_rss_sources("Iran war", sources=sources, days_back=10, top_k=5)
        assert len(results) == 0

    @patch("pipeline.rss_sources._extract_text", return_value="Full text")
    @patch("pipeline.rss_sources._fetch_feed")
    def test_fetch_text_disabled(
        self, mock_fetch: MagicMock, mock_extract: MagicMock
    ) -> None:
        """When fetch_text=False, text should be empty."""
        now = datetime.now(tz=UTC)
        mock_feed = MagicMock()
        mock_feed.entries = [
            {
                "title": "Iran update",
                "link": "https://example.com/update",
                "summary": "Iran latest",
                "published_parsed": now.timetuple(),
            }
        ]
        mock_fetch.return_value = mock_feed

        sources = [
            RssSource(
                name="test",
                feed_url="https://example.com/feed/",
                wp_search_base="https://example.com/",
            )
        ]
        results = search_rss_sources("Iran update", sources=sources, fetch_text=False)
        assert len(results) == 1
        assert results[0].text == ""
        mock_extract.assert_not_called()


def test_semafor_source_exists():
    assert SEMAFOR.name == "semafor"
    assert SEMAFOR.feed_url == "https://www.semafor.com/rss.xml"


def test_categorize_semafor_fp():
    assert categorize_semafor_article("Africa") == "fp"
    assert categorize_semafor_article("Gulf") == "fp"
    assert categorize_semafor_article("Security") == "fp"


def test_categorize_semafor_th():
    assert categorize_semafor_article("Business") == "th"
    assert categorize_semafor_article("Technology") == "th"
    assert categorize_semafor_article("Media") == "th"
    assert categorize_semafor_article("CEO") == "th"
    assert categorize_semafor_article("Energy") == "th"


def test_categorize_semafor_both():
    assert categorize_semafor_article("Politics") == "both"


def test_categorize_semafor_unknown_defaults_to_both():
    assert categorize_semafor_article("SomeNewCategory") == "both"
