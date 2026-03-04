from __future__ import annotations

from unittest.mock import patch

from pipeline.article_fetcher import Article
from pipeline.exa_client import ExaResult
from pipeline.rss_sources import RssResult
from pipeline.things_happen_collector import _slugify, collect_all_artifacts
from pipeline.things_happen_editor import ResearchDirective
from pipeline.xai_client import XaiResult


def test_slugify() -> None:
    assert _slugify("Hello World!") == "hello-world"
    assert _slugify("A -- B") == "a-b"
    assert (
        _slugify("Very Long Headline That Exceeds Fifty Characters Limit")
        == "very-long-headline-that-exceeds-fifty-characters-l"
    )


@patch("pipeline.things_happen_collector.fetch_all_articles")
@patch("pipeline.things_happen_collector.resolve_redirect_url")
@patch("pipeline.things_happen_collector.generate_research_plan")
@patch("pipeline.things_happen_collector.search_related")
@patch("pipeline.things_happen_collector.search_twitter")
@patch("pipeline.things_happen_collector.search_rss_sources")
def test_collect_all_artifacts(
    mock_rss, mock_xai, mock_exa, mock_plan, mock_resolve, mock_fetch, tmp_path
) -> None:
    # Setup mocks
    mock_resolve.return_value = "http://resolved.com"

    mock_fetch.return_value = [
        Article(
            headline="Test Article", url="http://resolved.com", content="Full text here"
        )
    ]

    mock_plan.return_value = [
        ResearchDirective(
            headline="Test Article",
            needs_exa=True,
            exa_query="exa test",
            needs_xai=True,
            xai_query="xai test",
            is_foreign_policy=True,
            fp_query="rss test",
            is_ai=True,
            ai_query="ai test",
        )
    ]

    mock_exa.return_value = [ExaResult(title="Exa", url="http://exa", text="Exa text")]
    mock_xai.return_value = XaiResult(summary="Xai summary")
    mock_rss.return_value = [
        RssResult(
            source="test",
            title="Rss",
            url="http://rss",
            published=None,
            score=1.0,
            text="Rss text",
        )
    ]

    # Setup fake context scripts
    scripts_dir = tmp_path / "scripts" / "things-happen"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "2026-03-01.txt").write_text("old script")

    # Run collector
    links = [{"raw_url": "http://raw.com", "headline_context": "context"}]
    work_dir = tmp_path / "work"

    collect_all_artifacts("job123", links, work_dir, scripts_source_dir=scripts_dir)

    # Verify directories created
    assert (work_dir / "articles").exists()
    assert (work_dir / "enrichment" / "exa").exists()
    assert (work_dir / "enrichment" / "xai").exists()
    assert (work_dir / "enrichment" / "rss").exists()
    assert (work_dir / "context").exists()

    # Verify article written
    art_file = list((work_dir / "articles").glob("*.md"))[0]
    assert "Test Article" in art_file.read_text()
    assert "Full text here" in art_file.read_text()

    # Verify context copied
    ctx_file = work_dir / "context" / "2026-03-01.txt"
    assert ctx_file.exists()
    assert ctx_file.read_text() == "old script"

    # Verify enrichment written
    exa_file = list((work_dir / "enrichment" / "exa").glob("*.md"))[0]
    assert "Exa text" in exa_file.read_text()

    xai_file = list((work_dir / "enrichment" / "xai").glob("*.md"))[0]
    assert "Xai summary" in xai_file.read_text()

    rss_fp_files = list((work_dir / "enrichment" / "rss").glob("*-fp.md"))
    assert len(rss_fp_files) == 1
    assert "Rss text" in rss_fp_files[0].read_text()
    assert "Alternative Perspectives" in rss_fp_files[0].read_text()

    rss_ai_files = list((work_dir / "enrichment" / "rss").glob("*-ai.md"))
    assert len(rss_ai_files) == 1
    assert "Rss text" in rss_ai_files[0].read_text()
    assert "AI Perspectives" in rss_ai_files[0].read_text()
