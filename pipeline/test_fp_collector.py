from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pipeline.exa_client import ExaResult
from pipeline.fp_collector import _slugify, collect_fp_artifacts
from pipeline.fp_editor import FPResearchPlan, FPStoryDirective


_et = ZoneInfo("America/New_York")


def _make_empty_plan():
    return FPResearchPlan(themes=[], directives=[])


def _today_et() -> str:
    return datetime.now(tz=_et).strftime("%Y-%m-%d")


def _write_homepage_cache_file(
    cache_dir: Path,
    date: str,
    headline: str,
    url: str,
    region: str,
    text: str = "Full article text.",
) -> None:
    """Write a homepage cache file in the expected format."""
    from hashlib import md5

    slug = _slugify(headline)
    url_hash = md5(url.encode()).hexdigest()[:8]  # noqa: S324
    filename = f"{date}-{slug}-{url_hash}.md"
    content = (
        f"# {headline}\n\n"
        f"URL: {url}\n"
        f"Published: {date}\n"
        f"Region: {region}\n"
        f"Source: antiwar-homepage\n"
        f"Type: article\n\n"
        f"{text}"
    )
    (cache_dir / filename).write_text(content, encoding="utf-8")


def _write_rss_cache_file(
    cache_dir: Path,
    date: str,
    source: str,
    headline: str,
    url: str,
    text: str = "RSS article text.",
) -> None:
    """Write an RSS cache file in the expected format."""
    slug = _slugify(headline)
    filename = f"{date}-{source}-{slug}.md"
    content = (
        f"# {headline}\n\n"
        f"URL: {url}\n"
        f"Published: {date}\n"
        f"Source: {source}\n"
        f"Type: article\n\n"
        f"{text}"
    )
    (cache_dir / filename).write_text(content, encoding="utf-8")


def _write_semafor_cache_file(
    cache_dir: Path,
    date: str,
    headline: str,
    url: str,
    category: str,
    text: str = "Semafor article text.",
) -> None:
    """Write a Semafor cache file in the expected format."""
    slug = _slugify(headline)
    filename = f"{date}-{slug}.md"
    content = (
        f"# {headline}\n\n"
        f"URL: {url}\n"
        f"Published: {date}\n"
        f"Source: semafor\n"
        f"Category: {category}\n"
        f"Type: article\n\n"
        f"{text}"
    )
    (cache_dir / filename).write_text(content, encoding="utf-8")


def test_slugify() -> None:
    assert _slugify("Hello World!") == "hello-world"
    assert _slugify("A -- B") == "a-b"
    assert (
        _slugify("Very Long Headline That Exceeds Fifty Characters Limit")
        == "very-long-headline-that-exceeds-fifty-characters-l"
    )


@patch("pipeline.fp_collector.search_related")
@patch("pipeline.fp_collector.generate_fp_research_plan")
@patch("pipeline.fp_collector._extract_article_text")
def test_collect_fp_artifacts(
    mock_extract,
    mock_plan,
    mock_exa,
    tmp_path,
) -> None:
    today = _today_et()

    # Pre-populate homepage cache
    homepage_cache = tmp_path / "homepage-cache"
    homepage_cache.mkdir()
    _write_homepage_cache_file(
        homepage_cache,
        today,
        "Iran Escalation",
        "http://example.com/iran",
        "middle-east",
        "Full article text about Iran.",
    )
    _write_homepage_cache_file(
        homepage_cache,
        today,
        "NATO Update",
        "http://example.com/nato",
        "europe",
        "Full article text about NATO.",
    )

    # Pre-populate RSS cache
    rss_cache = tmp_path / "rss-cache"
    rss_cache.mkdir()
    _write_rss_cache_file(
        rss_cache,
        today,
        "antiwar_news",
        "Antiwar News Story",
        "http://antiwar.com/story1",
        "Full RSS article text here.",
    )
    _write_rss_cache_file(
        rss_cache,
        today,
        "caitlinjohnstone",
        "Caitlin Johnston on War",
        "http://caitlin.com/story1",
        "Caitlin article text.",
    )

    # Pre-populate Semafor cache (empty for this test)
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()

    # Fake article text extraction (for routed links only now)
    mock_extract.return_value = "Full article text about the topic."

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
    collect_fp_artifacts(
        "job123",
        work_dir,
        scripts_source_dir=scripts_dir,
        homepage_cache_dir=homepage_cache,
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=semafor_cache,
    )

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

    # Verify _extract_article_text was NOT called (cache-based reading, no routed links)
    mock_extract.assert_not_called()


def test_routed_levine_links_included(tmp_path, monkeypatch):
    """FP Digest collector picks up routed links from Things Happen."""
    from pipeline.fp_collector import collect_fp_artifacts

    # Mock only external calls still needed
    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text",
        lambda url: f"Article text for {url}",
    )
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    monkeypatch.setattr("pipeline.fp_collector.search_related", lambda *a, **kw: [])

    # Empty cache dirs
    homepage_cache = tmp_path / "homepage-cache"
    homepage_cache.mkdir()
    rss_cache = tmp_path / "rss-cache"
    rss_cache.mkdir()
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()

    # Create routed links file
    routed_dir = tmp_path / "fp-routed"
    routed_dir.mkdir()

    today = datetime.now(tz=_et).strftime("%Y-%m-%d")
    (routed_dir / f"{today}.json").write_text(
        json.dumps(
            [
                {
                    "headline": "Iran Sanctions Tighten",
                    "url": "https://example.com/iran",
                    "snippet": "sanctions content",
                },
            ]
        )
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_fp_artifacts(
        "test-job",
        work_dir,
        scripts_source_dir=scripts_dir,
        fp_routed_dir=routed_dir,
        homepage_cache_dir=homepage_cache,
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=semafor_cache,
    )

    # Routed article should appear in work_dir/articles/routed/
    routed_articles = list((work_dir / "articles" / "routed").glob("*.md"))
    assert len(routed_articles) == 1
    assert "Iran Sanctions Tighten" in routed_articles[0].read_text()


def test_semafor_fp_articles_included(tmp_path, monkeypatch):
    """FP Digest collector picks up Semafor FP-category articles."""
    from pipeline.fp_collector import collect_fp_artifacts

    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text", lambda url: "text"
    )
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    monkeypatch.setattr("pipeline.fp_collector.search_related", lambda *a, **kw: [])

    # Empty homepage and RSS caches
    homepage_cache = tmp_path / "homepage-cache"
    homepage_cache.mkdir()
    rss_cache = tmp_path / "rss-cache"
    rss_cache.mkdir()

    # Pre-populate Semafor cache with an FP article
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    today = _today_et()
    _write_semafor_cache_file(
        semafor_cache,
        today,
        "Gulf Crisis Deepens",
        "https://semafor.com/gulf",
        "Gulf",
        "Gulf article text",
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_fp_artifacts(
        "test-job",
        work_dir,
        scripts_source_dir=scripts_dir,
        homepage_cache_dir=homepage_cache,
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=semafor_cache,
    )

    semafor_dir = work_dir / "articles" / "semafor"
    assert semafor_dir.exists()
    semafor_files = list(semafor_dir.glob("*.md"))
    assert len(semafor_files) == 1
    assert "Gulf Crisis Deepens" in semafor_files[0].read_text()


def test_fp_collector_reads_from_caches(tmp_path, monkeypatch):
    """Cache-based reading with lookback window filters out old articles."""
    from datetime import timedelta

    from pipeline.fp_collector import collect_fp_artifacts

    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text", lambda url: "text"
    )
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    monkeypatch.setattr("pipeline.fp_collector.search_related", lambda *a, **kw: [])

    today = datetime.now(tz=_et).strftime("%Y-%m-%d")
    yesterday = (datetime.now(tz=_et) - timedelta(days=1)).strftime("%Y-%m-%d")
    old_date = (datetime.now(tz=_et) - timedelta(days=10)).strftime("%Y-%m-%d")

    # Homepage cache: today, yesterday, and old article
    homepage_cache = tmp_path / "homepage-cache"
    homepage_cache.mkdir()
    _write_homepage_cache_file(
        homepage_cache,
        today,
        "Today Homepage Article",
        "http://example.com/today",
        "middle-east",
    )
    _write_homepage_cache_file(
        homepage_cache,
        yesterday,
        "Yesterday Homepage Article",
        "http://example.com/yesterday",
        "europe",
    )
    _write_homepage_cache_file(
        homepage_cache,
        old_date,
        "Old Homepage Article",
        "http://example.com/old",
        "asia",
    )

    # RSS cache: today, yesterday, and old article
    rss_cache = tmp_path / "rss-cache"
    rss_cache.mkdir()
    _write_rss_cache_file(
        rss_cache,
        today,
        "antiwar_news",
        "Today RSS Article",
        "http://antiwar.com/today",
    )
    _write_rss_cache_file(
        rss_cache,
        yesterday,
        "antiwar_news",
        "Yesterday RSS Article",
        "http://antiwar.com/yesterday",
    )
    _write_rss_cache_file(
        rss_cache, old_date, "antiwar_news", "Old RSS Article", "http://antiwar.com/old"
    )

    # Semafor cache: FP article today, non-FP article today, old FP article
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    _write_semafor_cache_file(
        semafor_cache, today, "Gulf Today", "http://semafor.com/gulf", "Gulf"
    )
    _write_semafor_cache_file(
        semafor_cache,
        today,
        "Business Today",
        "http://semafor.com/business",
        "Business",
    )  # Not FP
    _write_semafor_cache_file(
        semafor_cache,
        old_date,
        "Old Gulf Article",
        "http://semafor.com/old-gulf",
        "Gulf",
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_fp_artifacts(
        "test-lookback",
        work_dir,
        scripts_source_dir=scripts_dir,
        homepage_cache_dir=homepage_cache,
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=semafor_cache,
        lookback_days=2,
    )

    # Homepage: today + yesterday = 2 articles; old should be excluded
    all_homepage_files = list((work_dir / "articles" / "homepage").rglob("*.md"))
    assert len(all_homepage_files) == 2
    contents = [f.read_text() for f in all_homepage_files]
    headlines = [c.split("\n")[0].lstrip("# ") for c in contents]
    assert "Today Homepage Article" in headlines
    assert "Yesterday Homepage Article" in headlines
    assert "Old Homepage Article" not in headlines

    # RSS: today + yesterday = 2 articles; old should be excluded
    all_rss_files = list((work_dir / "articles" / "rss").rglob("*.md"))
    assert len(all_rss_files) == 2
    rss_contents = [f.read_text() for f in all_rss_files]
    rss_headlines = [c.split("\n")[0].lstrip("# ") for c in rss_contents]
    assert "Today RSS Article" in rss_headlines
    assert "Yesterday RSS Article" in rss_headlines
    assert "Old RSS Article" not in rss_headlines

    # Semafor: only FP articles in window (Gulf Today);
    # Business Today excluded (not FP); Old Gulf excluded (out of window)
    semafor_dir = work_dir / "articles" / "semafor"
    semafor_files = list(semafor_dir.glob("*.md"))
    assert len(semafor_files) == 1
    assert "Gulf Today" in semafor_files[0].read_text()


@patch("pipeline.fp_collector.search_related")
@patch("pipeline.fp_collector.generate_fp_research_plan")
def test_multiline_homepage_title_normalized(
    mock_plan,
    mock_exa,
    tmp_path,
) -> None:
    """Homepage cache files with multi-line titles are normalized to single lines."""
    today = _today_et()

    # Write a cache file with a multi-line title (reproduces real-world bug)
    homepage_cache = tmp_path / "homepage-cache"
    homepage_cache.mkdir()
    multiline_title = (
        "Democrats \n                    Say White House Offers No Clarity"
    )
    slug = _slugify(multiline_title)
    from hashlib import md5

    url = "http://example.com/democrats"
    url_hash = md5(url.encode()).hexdigest()[:8]  # noqa: S324
    filename = f"{today}-{slug}-{url_hash}.md"
    content = (
        f"# {multiline_title}\n\n"
        f"URL: {url}\n"
        f"Published: {today}\n"
        f"Region: The War at Home\n"
        f"Source: antiwar-homepage\n"
        f"Type: article\n\n"
        f"Full article text about Democrats."
    )
    (homepage_cache / filename).write_text(content, encoding="utf-8")

    rss_cache = tmp_path / "rss-cache"
    rss_cache.mkdir()
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    mock_plan.return_value = FPResearchPlan(themes=[], directives=[])
    mock_exa.return_value = []

    work_dir = tmp_path / "work"
    collect_fp_artifacts(
        "test-multiline",
        work_dir,
        scripts_source_dir=scripts_dir,
        homepage_cache_dir=homepage_cache,
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=semafor_cache,
    )

    # The article file should have a normalized single-line title
    homepage_files = list((work_dir / "articles" / "homepage").rglob("*.md"))
    assert len(homepage_files) == 1
    written = homepage_files[0].read_text(encoding="utf-8")
    first_line = written.split("\n")[0]
    assert first_line == "# Democrats Say White House Offers No Clarity"
    # File slug should use the full normalized title
    assert "democrats-say-white-house-offers-no-clarity" in homepage_files[0].name
