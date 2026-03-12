"""Tests for the freshness annotation module."""

from __future__ import annotations

from pipeline.freshness import (
    format_coverage_ledger,
    annotate_headlines,
    build_freshness_prompt,
    HeadlineClassification,
)


def test_format_coverage_ledger_empty():
    result = format_coverage_ledger([])
    assert result == ""


def test_format_coverage_ledger_with_data():
    summary = [
        {
            "theme": "US-Iran War",
            "days_covered": 3,
            "article_count": 6,
            "episode_dates": ["2026-03-09", "2026-03-10", "2026-03-11"],
            "was_lead": True,
        },
        {
            "theme": "THAAD Transfer",
            "days_covered": 1,
            "article_count": 2,
            "episode_dates": ["2026-03-11"],
            "was_lead": False,
        },
    ]
    result = format_coverage_ledger(summary)
    assert "US-Iran War" in result
    assert "3/3" in result or "3 of 3" in result
    assert "LEAD" in result
    assert "THAAD Transfer" in result
    assert "1/3" in result or "1 of 3" in result


def test_annotate_headlines_marks_fresh_and_recurring():
    headlines = [
        "[homepage/iran] School strike update\nContext: New details...",
        "[rss/antiwar] New Somalia airstrike\nContext: US launches...",
    ]
    classifications = [
        HeadlineClassification(
            headline_index=0,
            matched_theme="US-Iran War",
        ),
        HeadlineClassification(
            headline_index=1,
            matched_theme=None,
        ),
    ]
    coverage = [
        {
            "theme": "US-Iran War",
            "days_covered": 3,
            "article_count": 6,
            "episode_dates": ["2026-03-09", "2026-03-10", "2026-03-11"],
            "was_lead": True,
        },
    ]
    result = annotate_headlines(headlines, classifications, coverage)
    assert "[RECURRING" in result[0]
    assert "3/3" in result[0] or "3 of 3" in result[0]
    assert "[FRESH]" in result[1]


def test_annotate_headlines_no_coverage_all_fresh():
    headlines = ["[rss/antiwar] Something new\nContext: ..."]
    classifications = [
        HeadlineClassification(headline_index=0, matched_theme=None),
    ]
    result = annotate_headlines(headlines, classifications, [])
    assert "[FRESH]" in result[0]


def test_build_freshness_prompt_includes_themes_and_headlines():
    headlines = ["[homepage/iran] Strike update\nContext: ..."]
    coverage = [
        {
            "theme": "Iran War",
            "days_covered": 2,
            "article_count": 4,
            "episode_dates": ["2026-03-10", "2026-03-11"],
            "was_lead": True,
        },
    ]
    prompt = build_freshness_prompt(headlines, coverage)
    assert "Iran War" in prompt
    assert "Strike update" in prompt
    assert "headline_index" in prompt  # Schema mention


def test_freshness_integration_with_collector_args():
    """Verify annotate_headlines output format matches what editors expect."""
    headlines = [
        "[homepage/iran] School strike latest\nContext: New details on...",
        "[rss/caitlinjohnstone] New essay on media\nContext: Media coverage...",
    ]
    coverage = [
        {
            "theme": "US-Iran War",
            "days_covered": 3,
            "article_count": 6,
            "episode_dates": ["2026-03-09", "2026-03-10", "2026-03-11"],
            "was_lead": True,
        },
    ]
    classifications = [
        HeadlineClassification(headline_index=0, matched_theme="US-Iran War"),
        HeadlineClassification(headline_index=1, matched_theme=None),
    ]
    annotated = annotate_headlines(headlines, classifications, coverage)
    assert "School strike latest" in annotated[0]
    assert "New essay on media" in annotated[1]
    assert annotated[0].startswith("[RECURRING")
    assert annotated[1].startswith("[FRESH]")
