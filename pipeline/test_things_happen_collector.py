from __future__ import annotations

import json
from unittest.mock import patch

import feedparser

from pipeline.article_fetcher import Article
from pipeline.exa_client import ExaResult
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
@patch("pipeline.things_happen_collector.search_zvi_cache")
@patch("pipeline.things_happen_collector._fetch_semafor_articles")
@patch("pipeline.things_happen_collector.sync_zvi_cache")
def test_collect_all_artifacts(
    mock_zvi,
    mock_semafor,
    mock_zvi_search,
    mock_xai,
    mock_exa,
    mock_plan,
    mock_resolve,
    mock_fetch,
    tmp_path,
) -> None:
    mock_semafor.return_value = []
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
            is_foreign_policy=False,
            fp_query="",
            is_ai=True,
            ai_query="ai test",
        )
    ]

    mock_exa.return_value = [ExaResult(title="Exa", url="http://exa", text="Exa text")]
    mock_xai.return_value = XaiResult(summary="Xai summary")
    mock_zvi_search.return_value = [
        {
            "headline": "Zvi AI Take",
            "path": "/fake/path",
            "snippet": "Zvi AI text",
            "score": 5.0,
        }
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

    rss_ai_files = list((work_dir / "enrichment" / "rss").glob("*-ai.md"))
    assert len(rss_ai_files) == 1
    assert "Zvi AI text" in rss_ai_files[0].read_text()
    assert "Zvi Perspectives" in rss_ai_files[0].read_text()


def test_fp_links_routed_to_staging(tmp_path, monkeypatch):
    """FP-flagged links are written to fp-routed-links dir, not enriched."""
    # Mock all external calls
    monkeypatch.setattr(
        "pipeline.things_happen_collector._fetch_semafor_articles", lambda: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles",
        lambda *a, **kw: [
            Article(
                headline="Iran War Escalates",
                url="https://example.com/iran",
                content="war content",
            ),
            Article(
                headline="Bitcoin Rises",
                url="https://example.com/btc",
                content="crypto content",
            ),
        ],
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.search_related", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.search_twitter", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.search_zvi_cache", lambda *a, **kw: []
    )

    # Editor returns one FP link and one non-FP link
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_research_plan",
        lambda *a, **kw: [
            ResearchDirective(
                headline="Iran War Escalates",
                needs_exa=False,
                exa_query="",
                needs_xai=False,
                xai_query="",
                is_foreign_policy=True,
                fp_query="iran war",
                is_ai=False,
                ai_query="",
            ),
            ResearchDirective(
                headline="Bitcoin Rises",
                needs_exa=False,
                exa_query="",
                needs_xai=False,
                xai_query="",
                is_foreign_policy=False,
                fp_query="",
                is_ai=False,
                ai_query="",
            ),
        ],
    )

    routed_dir = tmp_path / "fp-routed"
    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    links_raw = [
        {"raw_url": "https://example.com/iran", "headline": "Iran War Escalates"},
        {"raw_url": "https://example.com/btc", "headline": "Bitcoin Rises"},
    ]

    collect_all_artifacts(
        "test-job",
        links_raw,
        work_dir,
        scripts_source_dir=scripts_dir,
        fp_routed_dir=routed_dir,
    )

    # FP link should be in the routed file
    routed_files = list(routed_dir.glob("*.json"))
    assert len(routed_files) == 1
    routed = json.loads(routed_files[0].read_text())
    assert len(routed) == 1
    assert routed[0]["headline"] == "Iran War Escalates"


def test_semafor_articles_added_to_work_dir(tmp_path, monkeypatch):
    """Semafor articles categorized as 'th' or 'both' appear in work dir."""
    # Mock all external calls
    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_research_plan", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )

    # Mock Semafor RSS fetch to return articles with categories
    fake_entries = [
        feedparser.FeedParserDict(
            {
                "title": "Tech Company IPO",
                "link": "https://semafor.com/tech-ipo",
                "summary": "A tech company goes public",
                "tags": [feedparser.FeedParserDict({"term": "Technology"})],
            }
        ),
        feedparser.FeedParserDict(
            {
                "title": "Gulf Tensions Rise",
                "link": "https://semafor.com/gulf-tensions",
                "summary": "Tensions in the Gulf",
                "tags": [feedparser.FeedParserDict({"term": "Gulf"})],
            }
        ),
    ]
    monkeypatch.setattr(
        "pipeline.things_happen_collector._fetch_semafor_articles", lambda: fake_entries
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_all_artifacts("test-job", [], work_dir, scripts_source_dir=scripts_dir)

    # Tech article should appear (category=Technology -> "th")
    semafor_dir = work_dir / "articles" / "semafor"
    assert semafor_dir.exists()
    semafor_files = list(semafor_dir.glob("*.md"))
    # Only the Tech article, not the Gulf one (FP-only)
    assert len(semafor_files) == 1
    assert "Tech Company IPO" in semafor_files[0].read_text()


def test_zvi_articles_added_to_work_dir(tmp_path, monkeypatch):
    """Zvi posts published today appear in work_dir/articles/zvi/."""
    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_research_plan", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector._fetch_semafor_articles", lambda: []
    )

    from datetime import datetime
    from zoneinfo import ZoneInfo

    zvi_cache = tmp_path / "zvi-cache"
    zvi_cache.mkdir()
    today = datetime.now(tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    (zvi_cache / f"{today}-fresh-essay.md").write_text(
        f"# Fresh Essay\n\nPost: Fresh Essay\nURL: https://zvi.com/fresh\nPublished: {today}\nType: essay\n\n"
        "Content about fresh AI topics."
    )
    (zvi_cache / "2026-01-01-old-essay.md").write_text(
        "# Old Essay\n\nPost: Old Essay\nURL: https://zvi.com/old\nPublished: 2026-01-01\nType: essay\n\n"
        "Content about old AI topics."
    )

    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_all_artifacts(
        "test-job",
        [],
        work_dir,
        scripts_source_dir=scripts_dir,
        zvi_cache_dir=zvi_cache,
    )

    zvi_dir = work_dir / "articles" / "zvi"
    assert zvi_dir.exists()
    zvi_files = list(zvi_dir.glob("*.md"))
    assert len(zvi_files) == 1
    assert "Fresh Essay" in zvi_files[0].read_text()


def test_ai_enrichment_uses_zvi_cache(tmp_path, monkeypatch):
    """When a directive has is_ai=True, enrichment searches the Zvi cache."""
    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles",
        lambda *a, **kw: [
            Article(
                headline="New AI Model",
                url="https://example.com/ai",
                content="AI model content",
            ),
        ],
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.search_related", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.search_twitter", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector._fetch_semafor_articles", lambda: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )

    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_research_plan",
        lambda *a, **kw: [
            ResearchDirective(
                headline="New AI Model",
                needs_exa=False,
                exa_query="",
                needs_xai=False,
                xai_query="",
                is_foreign_policy=False,
                fp_query="",
                is_ai=True,
                ai_query="new AI model release",
            ),
        ],
    )

    # Create a Zvi cache with a matching article
    zvi_cache = tmp_path / "zvi-cache"
    zvi_cache.mkdir()
    (zvi_cache / "2026-03-07-ai-models.md").write_text(
        "# AI Models Overview\n\nPost: AI Models\nURL: https://zvi.com\nPublished: 2026-03-07\nType: essay\n\n"
        "A new AI model was released this week, achieving state of the art results."
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_all_artifacts(
        "test-job",
        [{"raw_url": "https://example.com/ai", "headline": "New AI Model"}],
        work_dir,
        scripts_source_dir=scripts_dir,
        zvi_cache_dir=zvi_cache,
    )

    # Enrichment should have written a Zvi match file
    rss_dir = work_dir / "enrichment" / "rss"
    ai_files = list(rss_dir.glob("*-ai.md"))
    assert len(ai_files) == 1
    content = ai_files[0].read_text()
    assert "AI Models Overview" in content
    assert "Zvi Perspectives" in content
