from __future__ import annotations

import json
from unittest.mock import patch

from pipeline.exa_client import ExaResult
from pipeline.fp_collector import _slugify, collect_fp_artifacts
from pipeline.fp_editor import FPResearchPlan, FPStoryDirective
from pipeline.fp_homepage_scraper import HomepageLink


def test_slugify() -> None:
    assert _slugify("Hello World!") == "hello-world"
    assert _slugify("A -- B") == "a-b"
    assert (
        _slugify("Very Long Headline That Exceeds Fifty Characters Limit")
        == "very-long-headline-that-exceeds-fifty-characters-l"
    )


@patch("pipeline.fp_collector.search_related")
@patch("pipeline.fp_collector.generate_fp_research_plan")
@patch("pipeline.fp_collector._fetch_rss_articles")
@patch("pipeline.fp_collector._extract_article_text")
@patch("pipeline.fp_collector.scrape_homepage")
def test_collect_fp_artifacts(
    mock_scrape,
    mock_extract,
    mock_rss,
    mock_plan,
    mock_exa,
    tmp_path,
) -> None:
    # Fake homepage links
    mock_scrape.return_value = [
        HomepageLink(
            region="middle-east",
            headline="Iran Escalation",
            url="http://example.com/iran",
        ),
        HomepageLink(
            region="europe", headline="NATO Update", url="http://example.com/nato"
        ),
    ]

    # Fake article text extraction
    mock_extract.return_value = "Full article text about the topic."

    # Fake RSS articles
    mock_rss.return_value = [
        {
            "source": "antiwar_news",
            "headline": "Antiwar News Story",
            "url": "http://antiwar.com/story1",
            "summary": "Short summary",
            "text": "Full RSS article text here.",
        },
        {
            "source": "caitlinjohnstone",
            "headline": "Caitlin Johnston on War",
            "url": "http://caitlin.com/story1",
            "summary": "Caitlin summary",
            "text": "",  # short text to test full-text fetch
        },
    ]

    # Fake research plan
    mock_plan.return_value = FPResearchPlan(
        themes=["War in Middle East", "NATO Expansion"],
        directives=[
            FPStoryDirective(
                headline="Iran Escalation",
                source="homepage/middle-east",
                priority=1,
                theme="War in Middle East",
                needs_exa=True,
                exa_query="iran escalation latest news",
                include_in_episode=True,
            ),
            FPStoryDirective(
                headline="NATO Update",
                source="homepage/europe",
                priority=2,
                theme="NATO Expansion",
                needs_exa=False,
                exa_query="",
                include_in_episode=False,
            ),
        ],
    )

    # Fake Exa results
    mock_exa.return_value = [
        ExaResult(
            title="Exa Article", url="http://exa.com/article", text="Exa article text"
        )
    ]

    # Setup fake context scripts directory
    scripts_dir = tmp_path / "scripts" / "fp-digest"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "2026-03-01.txt").write_text("old episode script 1")
    (scripts_dir / "2026-03-02.txt").write_text("old episode script 2")
    (scripts_dir / "2026-03-03.txt").write_text("old episode script 3")

    # Run collector
    work_dir = tmp_path / "work"
    collect_fp_artifacts("job123", work_dir, scripts_source_dir=scripts_dir)

    # Verify directory structure created
    assert (work_dir / "articles" / "homepage").exists()
    assert (work_dir / "articles" / "rss").exists()
    assert (work_dir / "enrichment" / "exa").exists()
    assert (work_dir / "context").exists()

    # Verify homepage articles written by region
    homepage_middle_east = work_dir / "articles" / "homepage" / "middle-east"
    assert homepage_middle_east.exists()
    hp_files = list(homepage_middle_east.glob("*.md"))
    assert len(hp_files) == 1
    content = hp_files[0].read_text()
    assert "Iran Escalation" in content
    assert "http://example.com/iran" in content

    # Verify RSS articles written by source
    rss_antiwar = work_dir / "articles" / "rss" / "antiwar_news"
    assert rss_antiwar.exists()
    rss_files = list(rss_antiwar.glob("*.md"))
    assert len(rss_files) == 1
    assert "Antiwar News Story" in rss_files[0].read_text()

    # Verify context scripts copied
    ctx_files = list((work_dir / "context").glob("*.txt"))
    assert len(ctx_files) == 3

    # Verify plan.json written
    plan_file = work_dir / "plan.json"
    assert plan_file.exists()
    plan_data = json.loads(plan_file.read_text())
    assert "themes" in plan_data
    assert "directives" in plan_data
    assert len(plan_data["themes"]) == 2

    # Verify Exa enrichment for selected story (needs_exa=True, include_in_episode=True)
    exa_files = list((work_dir / "enrichment" / "exa").glob("*.md"))
    assert len(exa_files) == 1
    assert "Exa article text" in exa_files[0].read_text()

    # Verify search_related called only for needs_exa+include_in_episode directive
    mock_exa.assert_called_once()

    # Verify _extract_article_text was called for homepage links
    assert mock_extract.call_count >= 2  # at least for the 2 homepage links
