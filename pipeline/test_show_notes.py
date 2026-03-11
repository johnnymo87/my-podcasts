from __future__ import annotations

import json

from pipeline.show_notes import extract_show_notes_articles


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
                "is_ai": False,
                "ai_query": "",
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
                "is_ai": True,
                "ai_query": "AI training data workers",
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
                "is_ai": False,
                "ai_query": "",
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
                "is_ai": False,
                "ai_query": "",
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
                "is_ai": False,
                "ai_query": "",
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
                "is_ai": False,
                "ai_query": "",
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
                "is_ai": False,
                "ai_query": "",
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
