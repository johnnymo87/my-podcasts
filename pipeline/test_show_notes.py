from __future__ import annotations

import json

from pipeline.show_notes import (
    _find_article_file,
    extract_show_notes_articles,
    filter_show_notes_by_coverage,
)


def test_extract_rundown_articles(tmp_path) -> None:
    """Extract articles from a Rundown work directory."""
    plan = {
        "themes": ["Markets", "AI & Labor"],
        "directives": [
            {
                "headline": "Oil's Wild Monday",
                "source": "levine",
                "priority": 1,
                "theme": "Markets",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "include_in_episode": True,
            },
            {
                "headline": "AI Training Workers",
                "source": "levine",
                "priority": 2,
                "theme": "AI & Labor",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "include_in_episode": True,
            },
            {
                "headline": "Skipped Story",
                "source": "levine",
                "priority": 3,
                "theme": "Markets",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": True,
                "fp_query": "",
                "include_in_episode": False,
            },
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    (articles_dir / "00-oil-s-wild-monday.md").write_text(
        "# Oil's Wild Monday\n\nURL: https://example.com/oil\n\nArticle text."
    )
    (articles_dir / "01-ai-training-workers.md").write_text(
        "# AI Training Workers\n\nURL: https://nymag.com/ai\n\nArticle text."
    )

    result = extract_show_notes_articles(tmp_path)

    assert len(result) == 2
    assert result[0]["title"] == "Oil's Wild Monday"
    assert result[0]["url"] == "https://example.com/oil"
    assert result[0]["theme"] == "Markets"
    assert result[1]["title"] == "AI Training Workers"
    assert result[1]["url"] == "https://nymag.com/ai"
    assert result[1]["theme"] == "AI & Labor"


def test_extract_fp_articles(tmp_path) -> None:
    """Extract articles from an FP Digest work directory."""
    plan = {
        "themes": ["Iran War"],
        "directives": [
            {
                "headline": "US Strikes Iran Base",
                "source": "homepage/iran",
                "priority": 1,
                "theme": "Iran War",
                "needs_exa": False,
                "exa_query": "",
                "include_in_episode": True,
            },
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    homepage_dir = tmp_path / "articles" / "homepage" / "iran"
    homepage_dir.mkdir(parents=True)
    (homepage_dir / "us-strikes-iran-base.md").write_text(
        "# US Strikes Iran Base\n\nURL: https://antiwar.com/iran\nRegion: Iran\n\nText."
    )

    result = extract_show_notes_articles(tmp_path)

    assert len(result) == 1
    assert result[0]["title"] == "US Strikes Iran Base"
    assert result[0]["url"] == "https://antiwar.com/iran"
    assert result[0]["theme"] == "Iran War"


def test_extract_article_missing_url(tmp_path) -> None:
    """Articles with no URL line get url=None."""
    plan = {
        "themes": ["Tech"],
        "directives": [
            {
                "headline": "Some Story",
                "source": "levine",
                "priority": 1,
                "theme": "Tech",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "include_in_episode": True,
            },
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    (articles_dir / "00-some-story.md").write_text(
        "# Some Story\n\nNo URL line here.\n\nArticle text."
    )

    result = extract_show_notes_articles(tmp_path)

    assert len(result) == 1
    assert result[0]["title"] == "Some Story"
    assert result[0]["url"] is None


def test_extract_no_plan_returns_empty(tmp_path) -> None:
    """If plan.json is missing, return empty list."""
    result = extract_show_notes_articles(tmp_path)
    assert result == []


def test_extract_orders_by_theme_then_priority(tmp_path) -> None:
    """Articles are ordered by theme order from plan, then by priority."""
    plan = {
        "themes": ["B Theme", "A Theme"],
        "directives": [
            {
                "headline": "B2",
                "source": "levine",
                "priority": 2,
                "theme": "B Theme",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "include_in_episode": True,
            },
            {
                "headline": "A1",
                "source": "levine",
                "priority": 1,
                "theme": "A Theme",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "include_in_episode": True,
            },
            {
                "headline": "B1",
                "source": "levine",
                "priority": 1,
                "theme": "B Theme",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "include_in_episode": True,
            },
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    (articles_dir / "00-b2.md").write_text("# B2\n\nURL: https://b2.com\n\nText.")
    (articles_dir / "01-a1.md").write_text("# A1\n\nURL: https://a1.com\n\nText.")
    (articles_dir / "02-b1.md").write_text("# B1\n\nURL: https://b1.com\n\nText.")

    result = extract_show_notes_articles(tmp_path)

    titles = [r["title"] for r in result]
    # B Theme comes first (index 0 in themes), then A Theme
    # Within B Theme, priority 1 (B1) before priority 2 (B2)
    assert titles == ["B1", "B2", "A1"]


def test_extract_articles_exa_empty_result_not_used_as_source(tmp_path) -> None:
    """A `Result: empty` Exa stub must not be surfaced as a show-note source."""
    plan = {
        "themes": ["Markets"],
        "directives": [
            {
                "headline": "No Results Story",
                "source": "levine",
                "priority": 1,
                "theme": "Markets",
                "needs_exa": True,
                "exa_query": "no results story",
                "is_foreign_policy": False,
                "fp_query": "",
                "include_in_episode": True,
            },
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    exa_dir = tmp_path / "enrichment" / "exa"
    exa_dir.mkdir(parents=True)
    (exa_dir / "no-results-story.md").write_text(
        "Result: empty\n\nNo results found for query."
    )

    result = extract_show_notes_articles(tmp_path)

    assert len(result) == 1
    assert result[0]["title"] == "No Results Story"
    assert result[0]["url"] is None


def test_extract_articles_exa_headerless_still_used_as_source(tmp_path) -> None:
    """Headerless Exa files (FP format) are still trusted as a show-note source."""
    plan = {
        "themes": ["World"],
        "directives": [
            {
                "headline": "Legacy Format Story",
                "source": "homepage",
                "priority": 1,
                "theme": "World",
                "needs_exa": True,
                "exa_query": "legacy format story",
                "is_foreign_policy": True,
                "fp_query": "",
                "include_in_episode": True,
            },
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    exa_dir = tmp_path / "enrichment" / "exa"
    exa_dir.mkdir(parents=True)
    (exa_dir / "legacy-format-story.md").write_text(
        "# Legacy Format Story\n\nURL: https://example.com/legacy\n\nArticle text."
    )

    result = extract_show_notes_articles(tmp_path)

    assert len(result) == 1
    assert result[0]["url"] == "https://example.com/legacy"


# --- _find_article_file: resolver-backed lookup (bead 3yb follow-up) ---


def test_find_article_file_resolves_via_index_exact_match(tmp_path) -> None:
    """An exact headline_index.json hit is used directly, without touching
    the filesystem search below it."""
    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    article = articles_dir / "00-widget-story.md"
    article.write_text("# Widget Story\n\nURL: https://example.com/widget\n\nText.")
    (tmp_path / "headline_index.json").write_text(
        json.dumps({"Widget Story": "articles/00-widget-story.md"})
    )

    result = _find_article_file("Widget Story", "levine", tmp_path)
    assert result == article


def test_find_article_file_agrees_with_writer_resolver_on_whitespace_reformulation(
    tmp_path,
) -> None:
    """A directive headline differing from the index key only by whitespace
    must resolve to the SAME file via both the writer's article resolver
    (__main__.find_rundown_article_source) and show notes' _find_article_file
    -- otherwise trigger/delivery agree while show notes silently disagree,
    the same bug class as bead 3yb, one layer down.

    The article lives one level deeper than any of _find_article_file's
    filesystem tiers checks (not flat-top-level, not semafor/zvi/homepage/
    rss/routed). `_slugify` already collapses whitespace on its own, so a
    same-directory placement would let the filesystem fallback rescue this
    case too, and the "break the index path" mutation check would not bite.
    This placement means the index is the ONLY route to the file, proving
    the resolver path is actually exercised rather than incidentally
    redundant with the filesystem search underneath it.
    """
    nested_dir = tmp_path / "articles" / "custom-source"
    nested_dir.mkdir(parents=True)
    article = nested_dir / "00-widget-maker-struggles.md"
    article.write_text(
        "# Widget  Maker Struggles\n\nURL: https://example.com/widget\n\nText."
    )
    rel_path = "articles/custom-source/00-widget-maker-struggles.md"
    (tmp_path / "headline_index.json").write_text(
        json.dumps({"Widget  Maker Struggles": rel_path})
    )
    directive_headline = "Widget Maker Struggles"  # single space, Gemini-normalized

    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        headline = directive_headline
        source = "levine"

    _text, path, _reason = find_rundown_article_source(FakeDirective(), tmp_path)
    assert path == rel_path

    show_notes_result = _find_article_file(directive_headline, "levine", tmp_path)
    assert show_notes_result == article


def test_find_article_file_falls_back_to_filesystem_on_index_miss(tmp_path) -> None:
    """The index existing is not enough to stop the cascade -- a headline the
    index does not cover must still fall through to the filesystem search,
    matching find_rundown_article_source. Falling back only on index-
    *absence* would disagree with delivery on exactly the reformulated-
    headline cases this unification exists to reconcile."""
    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    article = articles_dir / "00-unindexed-story.md"
    article.write_text("# Unindexed Story\n\nURL: https://example.com/u\n\nText.")
    # Index exists but knows nothing about this headline.
    (tmp_path / "headline_index.json").write_text(
        json.dumps({"Some Other Story": "articles/01-other.md"})
    )

    result = _find_article_file("Unindexed Story", "levine", tmp_path)
    assert result == article


def test_find_article_file_no_index_resolves_via_filesystem(tmp_path) -> None:
    """fp_collector writes no headline_index.json at all, so the filesystem
    search is the ONLY path for FP Digest show notes -- this is the FP
    shape, not a legacy fallback. See exa_client.py:136 for the sibling
    comment pinning this same permanence for the Exa reader."""
    homepage_dir = tmp_path / "articles" / "homepage" / "iran"
    homepage_dir.mkdir(parents=True)
    article = homepage_dir / "us-strikes-iran-base.md"
    article.write_text(
        "# US Strikes Iran Base\n\nURL: https://antiwar.com/iran\n\nText."
    )
    assert not (tmp_path / "headline_index.json").exists()

    result = _find_article_file("US Strikes Iran Base", "homepage/iran", tmp_path)
    assert result == article


# --- filter_show_notes_by_coverage tests ---


def test_filter_exact_match() -> None:
    """Exact headline matches are kept."""
    articles = [
        {"title": "Deutsche Bank Exposure", "url": "https://a.com", "theme": "Finance"},
        {"title": "Sunday Robotics", "url": "https://b.com", "theme": "Tech"},
        {"title": "Chen Zhi Scam", "url": "https://c.com", "theme": "Crime"},
    ]
    covered = ["Deutsche Bank Exposure", "Sunday Robotics"]
    result = filter_show_notes_by_coverage(articles, covered)
    assert [r["title"] for r in result] == ["Deutsche Bank Exposure", "Sunday Robotics"]


def test_filter_case_insensitive() -> None:
    """Matching is case-insensitive."""
    articles = [
        {"title": "Oil Markets Wild Ride", "url": "https://a.com", "theme": "Energy"},
    ]
    covered = ["oil markets wild ride"]
    result = filter_show_notes_by_coverage(articles, covered)
    assert len(result) == 1
    assert result[0]["title"] == "Oil Markets Wild Ride"


def test_filter_empty_covered_returns_all() -> None:
    """When covered_headlines is empty, all articles are returned (no filtering)."""
    articles = [
        {"title": "Story A", "url": "https://a.com", "theme": "T1"},
        {"title": "Story B", "url": "https://b.com", "theme": "T2"},
    ]
    result = filter_show_notes_by_coverage(articles, [])
    assert len(result) == 2


def test_filter_no_match_returns_empty() -> None:
    """When nothing matches, nothing is returned."""
    articles = [
        {"title": "Completely Different", "url": "https://a.com", "theme": "T1"},
    ]
    covered = ["Some Other Story"]
    result = filter_show_notes_by_coverage(articles, covered)
    assert result == []


def test_filter_substring_match() -> None:
    """Covered headline that is a substring of the article title matches."""
    articles = [
        {
            "title": "Gene-Tweaked Banana Startup Tropic Secures $105 Million",
            "url": "https://a.com",
            "theme": "Tech",
        },
    ]
    covered = ["Tropic gene-edited banana startup"]
    result = filter_show_notes_by_coverage(articles, covered)
    # Substring match: "tropic" appears in the article title
    assert len(result) == 1


def test_filter_preserves_order() -> None:
    """Filtered list preserves the original article order."""
    articles = [
        {"title": "First Story", "url": "https://a.com", "theme": "T1"},
        {"title": "Second Story", "url": "https://b.com", "theme": "T2"},
        {"title": "Third Story", "url": "https://c.com", "theme": "T3"},
    ]
    covered = ["Third Story", "First Story"]
    result = filter_show_notes_by_coverage(articles, covered)
    # Original order preserved, not covered order
    assert [r["title"] for r in result] == ["First Story", "Third Story"]
