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
    monkeypatch,
) -> None:
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
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

    # Verify _extract_article_text was called for the two short RSS bodies
    # (both under the teaser gate) but never for homepage articles, which are
    # written directly from the cache and never routed through the fetch
    # path.
    assert mock_extract.call_count == 2
    fetched_urls = {call.args[0] for call in mock_extract.call_args_list}
    assert fetched_urls == {"http://antiwar.com/story1", "http://caitlin.com/story1"}
    assert "http://example.com/iran" not in fetched_urls
    assert "http://example.com/nato" not in fetched_urls


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
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)

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


def _write_semafor_cache_file_with_routing(
    cache_dir: Path,
    date: str,
    headline: str,
    url: str,
    category: str,
    routing: str,
    text: str = "Semafor article text.",
) -> None:
    """Write a Semafor cache file with an explicit Routing: header."""
    slug = _slugify(headline)
    filename = f"{date}-{slug}.md"
    content = (
        f"# {headline}\n\n"
        f"URL: {url}\n"
        f"Published: {date}\n"
        f"Source: semafor\n"
        f"Category: {category}\n"
        f"Routing: {routing}\n"
        f"Type: article\n\n"
        f"{text}"
    )
    (cache_dir / filename).write_text(content, encoding="utf-8")


def test_semafor_routing_header_preferred_over_category(tmp_path, monkeypatch):
    """Routing: header overrides Category:-based routing for Semafor articles."""
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

    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    today = _today_et()

    # Article 1: Category=Business (normally excluded) but Routing=fp → INCLUDED
    _write_semafor_cache_file_with_routing(
        semafor_cache,
        today,
        "Business Article With FP Routing",
        "https://semafor.com/business-fp",
        "Business",
        "fp",
        "Business but FP routed article text",
    )

    # Article 2: Category=Technology (normally excluded) and Routing=th → EXCLUDED
    _write_semafor_cache_file_with_routing(
        semafor_cache,
        today,
        "Tech Article With TH Routing",
        "https://semafor.com/tech-th",
        "Technology",
        "th",
        "Tech article text",
    )

    # Article 3: Routing=skip → EXCLUDED
    _write_semafor_cache_file_with_routing(
        semafor_cache,
        today,
        "Article With Skip Routing",
        "https://semafor.com/skip",
        "Gulf",
        "skip",
        "This Gulf article is explicitly skipped",
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_fp_artifacts(
        "test-routing",
        work_dir,
        scripts_source_dir=scripts_dir,
        homepage_cache_dir=homepage_cache,
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=semafor_cache,
    )

    semafor_dir = work_dir / "articles" / "semafor"
    assert semafor_dir.exists()
    semafor_files = list(semafor_dir.glob("*.md"))

    # Only the Business article with Routing: fp should be included
    assert len(semafor_files) == 1
    contents = semafor_files[0].read_text()
    assert "Business Article With FP Routing" in contents
    assert "Tech Article With TH Routing" not in contents
    assert "Article With Skip Routing" not in contents


def test_should_fetch_full_text_gate_matches_measured_corpus():
    """The gate separates antiwar teasers (max 454c) from Johnstone (min 767c).

    Threshold sits in the measured gap; see
    docs/plans/2026-08-18-fp-rss-full-text.md.
    """
    from pipeline.fp_collector import _should_fetch_full_text

    # Longest antiwar teaser measured across 1633 cache files.
    assert _should_fetch_full_text("x" * 454, "https://news.antiwar.com/a/") is True
    # Shortest caitlinjohnstone body measured across 147 cache files.
    assert _should_fetch_full_text("x" * 767, "https://x.substack.com/p/a") is False


def test_should_fetch_full_text_requires_a_url():
    from pipeline.fp_collector import _should_fetch_full_text

    assert _should_fetch_full_text("short", "") is False
    assert _should_fetch_full_text("short", "   ") is False


def test_should_fetch_full_text_boundary_is_exclusive():
    from pipeline.fp_collector import _should_fetch_full_text

    assert _should_fetch_full_text("x" * 599, "https://a/") is True
    assert _should_fetch_full_text("x" * 600, "https://a/") is False


def test_collector_cannot_reach_the_network_in_tests():
    """conftest severs fp_collector's HTTP transport structurally.

    Mirrors _block_real_telegram_posts: a test that grows a new outbound fetch
    must fail loudly rather than silently hit a real host.
    """
    import pytest

    # _extract_article_text swallows exceptions and returns "", which is the
    # degrade-to-excerpt path; assert the transport itself is blocked.
    from pipeline import fp_collector
    from pipeline.fp_collector import _extract_article_text

    with pytest.raises(AssertionError, match="real HTTP"):
        fp_collector.requests.get("https://example.invalid/")

    assert _extract_article_text("https://example.invalid/") == ""


def test_rss_teaser_is_replaced_with_fetched_full_text(tmp_path, monkeypatch):
    """A short RSS body is upgraded to the fetched article text."""
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()
    _write_rss_cache_file(
        rss_cache,
        today,
        "antiwar_news",
        "Strikes Kill Eleven",
        "https://news.antiwar.com/strikes/",
        text="Teaser body [&#8230;]",
    )
    full = "Full article text. " * 100
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: full)
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    work_dir = tmp_path / "work"

    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    art = (
        work_dir
        / "articles"
        / "rss"
        / "antiwar_news"
        / f"{_slugify('Strikes Kill Eleven')}.md"
    ).read_text()
    assert "Full article text." in art
    assert "Teaser body" not in art


def test_failed_fetch_degrades_to_the_excerpt(tmp_path, monkeypatch):
    """A fetch failure must never drop or shorten the story."""
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()
    _write_rss_cache_file(
        rss_cache,
        today,
        "antiwar_news",
        "Strikes Kill Eleven",
        "https://news.antiwar.com/strikes/",
        text="Teaser body [&#8230;]",
    )
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: "")
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    work_dir = tmp_path / "work"

    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    art = (
        work_dir
        / "articles"
        / "rss"
        / "antiwar_news"
        / f"{_slugify('Strikes Kill Eleven')}.md"
    ).read_text()
    assert "Teaser body" in art


def test_shorter_fetch_result_never_replaces_the_excerpt(tmp_path, monkeypatch):
    """The excerpt is the floor: a shorter extraction is discarded."""
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()
    _write_rss_cache_file(
        rss_cache,
        today,
        "antiwar_news",
        "Strikes Kill Eleven",
        "https://news.antiwar.com/strikes/",
        text="Teaser body [&#8230;]",
    )
    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text", lambda url: "Tiny."
    )
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    work_dir = tmp_path / "work"

    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    art = (
        work_dir
        / "articles"
        / "rss"
        / "antiwar_news"
        / f"{_slugify('Strikes Kill Eleven')}.md"
    ).read_text()
    assert "Teaser body" in art
    assert "Tiny." not in art


def test_full_text_rss_article_is_not_refetched(tmp_path, monkeypatch):
    """caitlinjohnstone-shaped bodies are already whole; leave them alone."""
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()
    _write_rss_cache_file(
        rss_cache,
        today,
        "caitlinjohnstone",
        "A Whole Essay",
        "https://caitlinjohnstone.substack.com/p/a-whole-essay",
        text="x" * 800,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text",
        lambda url: calls.append(url) or "SHOULD NOT BE USED",
    )
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    work_dir = tmp_path / "work"

    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    assert calls == []
    art = (
        work_dir
        / "articles"
        / "rss"
        / "caitlinjohnstone"
        / f"{_slugify('A Whole Essay')}.md"
    ).read_text()
    assert "x" * 800 in art


def test_fetched_text_reaches_the_editor_snippet(tmp_path, monkeypatch):
    """The editor sees the fetched text, not the teaser.

    Derived from the editor's own input rather than from the article file, so
    this cannot pass by construction alongside the file assertion above.
    """
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()
    _write_rss_cache_file(
        rss_cache,
        today,
        "antiwar_news",
        "Strikes Kill Eleven",
        "https://news.antiwar.com/strikes/",
        text="Teaser body [&#8230;]",
    )
    full = "Distinctive opening sentence. " + ("Filler text. " * 100)
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: full)
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)

    captured = {}

    def _fake_plan(headlines, **kwargs):
        captured["headlines"] = headlines
        return _make_empty_plan()

    monkeypatch.setattr("pipeline.fp_collector.generate_fp_research_plan", _fake_plan)
    work_dir = tmp_path / "work"

    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    assert any("Distinctive opening sentence." in h for h in captured["headlines"])


def test_fetches_are_capped_and_take_the_newest(tmp_path, monkeypatch):
    """Bounded work under a 14-day lookback; the cap trims the oldest.

    The newer date must carry at least _MAX_RSS_FETCHES files on its own, or
    the "all fetched urls are from the newer date" assertion is not entailed
    by the cap and would fail for a legitimate implementation.
    """
    from datetime import timedelta

    from pipeline.fp_collector import _MAX_RSS_FETCHES

    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()
    yesterday = (datetime.now(tz=_et) - timedelta(days=1)).strftime("%Y-%m-%d")

    today_urls: set[str] = set()
    for i in range(_MAX_RSS_FETCHES + 5):
        url = f"https://news.antiwar.com/today-{i}/"
        today_urls.add(url)
        _write_rss_cache_file(rss_cache, today, "antiwar_news", f"Today Story {i}", url)
    for i in range(5):
        _write_rss_cache_file(
            rss_cache,
            yesterday,
            "antiwar_news",
            f"Yesterday Story {i}",
            f"https://news.antiwar.com/yesterday-{i}/",
        )

    calls: list[str] = []
    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text",
        lambda url: calls.append(url) or "fetched " * 50,
    )
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    work_dir = tmp_path / "work"

    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
        lookback_days=3,
    )

    assert len(calls) == _MAX_RSS_FETCHES
    assert all(url in today_urls for url in calls)


def test_fetch_delay_matches_the_levine_path(tmp_path, monkeypatch):
    """One 1.0s sleep between fetches, none before the first."""
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()
    for i in range(3):
        _write_rss_cache_file(
            rss_cache,
            today,
            "antiwar_news",
            f"Teaser Story {i}",
            f"https://news.antiwar.com/story-{i}/",
        )

    sleeps: list[float] = []
    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text",
        lambda url: "fetched text " * 50,
    )
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    work_dir = tmp_path / "work"

    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    assert sleeps == [1.0, 1.0]


def test_cached_entities_are_decoded_on_read(tmp_path, monkeypatch):
    """Bodies already on disk are entity-encoded; decode when reading them.

    Deliberately arranged so the body is NOT upgraded (the fetch fails),
    because that is the only state in which the on-read decode is observable
    — a successful fetch would replace the body and hide it. This is the
    test whose absence made an earlier draft's mutation list dishonest.
    """
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()
    _write_rss_cache_file(
        rss_cache,
        today,
        "antiwar_news",
        "Ansar Allah Announces Attacks",
        "https://news.antiwar.com/a/",
        text="he called it a &#8220;landing ship&#8221; [&#8230;]",
    )
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: "")
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    work_dir = tmp_path / "work"

    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    art = (
        work_dir
        / "articles"
        / "rss"
        / "antiwar_news"
        / f"{_slugify('Ansar Allah Announces Attacks')}.md"
    ).read_text()
    assert "&#8220;" not in art
    assert "&#8230;" not in art
    assert "\u201clanding ship\u201d" in art
    assert "\u2026" in art


def test_retry_reuses_the_prior_attempt_and_makes_no_request(tmp_path, monkeypatch):
    """A re-run in the same work dir must not re-fetch what it already has.

    Collection re-runs from the top on every retry (collection_done.json is
    written last), and MAX_RETRY_FAILURES is 51 — so without this, a failing
    editor turns ~17 requests/day into ~850 against a small nonprofit's site.
    """
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()
    _write_rss_cache_file(
        rss_cache,
        today,
        "antiwar_news",
        "Strikes Kill Eleven",
        "https://news.antiwar.com/strikes/",
        text="Teaser body [&#8230;]",
    )
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    work_dir = tmp_path / "work"

    full = "Full article text. " * 100
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: full)
    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    art_path = (
        work_dir
        / "articles"
        / "rss"
        / "antiwar_news"
        / f"{_slugify('Strikes Kill Eleven')}.md"
    )
    assert full.strip() in art_path.read_text()

    calls: list[str] = []
    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text",
        lambda url: calls.append(url) or "SHOULD NOT BE USED",
    )
    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    assert calls == []
    assert full.strip() in art_path.read_text()
    assert "SHOULD NOT BE USED" not in art_path.read_text()


def test_semafor_cached_entities_are_decoded_on_read(tmp_path, monkeypatch):
    """Semafor cache bodies already on disk may carry undecoded HTML entities.

    Mirrors the antiwar RSS on-read decode (test_cached_entities_are_decoded_on_read):
    Task 4 fixes source_cache's write side, but files already cached during the
    180-day retention window keep the old encoding for as long as they remain
    in a lookback window.
    """
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    monkeypatch.setattr("pipeline.fp_collector.search_related", lambda *a, **kw: [])

    homepage_cache = tmp_path / "homepage-cache"
    homepage_cache.mkdir()
    rss_cache = tmp_path / "rss-cache"
    rss_cache.mkdir()
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    today = _today_et()

    _write_semafor_cache_file(
        semafor_cache,
        today,
        "Ansar Allah Announces Attacks",
        "https://semafor.com/a",
        "Gulf",
        text="he called it a &#8220;landing ship&#8221; [&#8230;]",
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_fp_artifacts(
        "test-semafor-entities",
        work_dir,
        scripts_source_dir=scripts_dir,
        homepage_cache_dir=homepage_cache,
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=semafor_cache,
    )

    semafor_dir = work_dir / "articles" / "semafor"
    semafor_files = list(semafor_dir.glob("*.md"))
    assert len(semafor_files) == 1
    art = semafor_files[0].read_text(encoding="utf-8")
    assert "&#8220;" not in art
    assert "&#8230;" not in art
    assert "\u201clanding ship\u201d" in art
    assert "\u2026" in art


def test_rss_entry_deduped_against_homepage_by_url(tmp_path, monkeypatch):
    """An RSS cache entry whose URL already arrived via homepage is dropped.

    Guards the `if url in homepage_urls: continue` guard in fp_collector.py
    Phase 2 — previously asserted only by code inspection, not by a test.
    """
    captured = {}

    def _fake_plan(headlines, **kwargs):
        captured["headlines"] = headlines
        return _make_empty_plan()

    monkeypatch.setattr("pipeline.fp_collector.generate_fp_research_plan", _fake_plan)
    monkeypatch.setattr("pipeline.fp_collector.search_related", lambda *a, **kw: [])
    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text",
        lambda url: "SHOULD NOT BE USED",
    )
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)

    homepage_cache = tmp_path / "homepage-cache"
    homepage_cache.mkdir()
    rss_cache = tmp_path / "rss-cache"
    rss_cache.mkdir()
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    today = _today_et()

    shared_url = "https://news.antiwar.com/shared-story/"
    _write_homepage_cache_file(
        homepage_cache,
        today,
        "Homepage Headline For Shared Story",
        shared_url,
        "middle-east",
    )
    _write_rss_cache_file(
        rss_cache,
        today,
        "antiwar_news",
        "RSS Headline For Shared Story",
        shared_url,
        text="RSS teaser body.",
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_fp_artifacts(
        "test-dedup",
        work_dir,
        scripts_source_dir=scripts_dir,
        homepage_cache_dir=homepage_cache,
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=semafor_cache,
    )

    rss_source_dir = work_dir / "articles" / "rss" / "antiwar_news"
    rss_files = list(rss_source_dir.glob("*.md")) if rss_source_dir.exists() else []
    assert rss_files == []

    headlines = captured["headlines"]
    matches = [h for h in headlines if "Shared Story" in h]
    assert len(matches) == 1
    assert "Homepage Headline For Shared Story" in matches[0]
    assert "RSS Headline For Shared Story" not in matches[0]


def test_retry_does_refetch_a_previously_failed_article(tmp_path, monkeypatch):
    """The reuse must not cache a failure. Derived from the opposite
    direction to the test above so the two cannot both pass on a stuck
    implementation."""
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()
    _write_rss_cache_file(
        rss_cache,
        today,
        "antiwar_news",
        "Strikes Kill Eleven",
        "https://news.antiwar.com/strikes/",
        text="Teaser body [&#8230;]",
    )
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    work_dir = tmp_path / "work"

    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: "")
    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    art_path = (
        work_dir
        / "articles"
        / "rss"
        / "antiwar_news"
        / f"{_slugify('Strikes Kill Eleven')}.md"
    )
    assert "Teaser body" in art_path.read_text()

    full = "Full article text. " * 100
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: full)
    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    assert full.strip() in art_path.read_text()
    assert "Teaser body" not in art_path.read_text()


def test_retry_reuse_does_not_cross_a_slug_collision(tmp_path, monkeypatch):
    """Two different in-window articles can share a work-dir path.

    ``slugify`` truncates at 50 chars, so two distinct headlines from the
    same source that agree on their first 50 slug chars write to the same
    ``art_path``. A retry must not let the reuse check hand one article's
    text to the other under its own headline — that is a wrong body under a
    true headline, the worst outcome for an unread-publish pipeline. Keying
    reuse to the file's ``URL:`` header (not just the path) is what prevents
    it.
    """
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()

    # Both headlines slugify to the same 50 'y' chars; they differ only
    # after the truncation point.
    headline_alpha = "Y" * 55 + " Report Alpha"
    headline_beta = "Y" * 55 + " Report Beta"
    assert _slugify(headline_alpha) == _slugify(headline_beta)
    url_alpha = "https://news.antiwar.com/story-alpha/"
    url_beta = "https://news.antiwar.com/story-beta/"

    # Explicit filenames control cache glob order: alpha sorts before beta,
    # so pending = [alpha, beta] and the end-of-run write loop (which walks
    # `pending` in that order) leaves beta's body as the final file content.
    (rss_cache / f"{today}-antiwar_news-000-alpha.md").write_text(
        f"# {headline_alpha}\n\nURL: {url_alpha}\nPublished: {today}\n"
        f"Source: antiwar_news\nType: article\n\nRSS article text.",
        encoding="utf-8",
    )
    (rss_cache / f"{today}-antiwar_news-001-beta.md").write_text(
        f"# {headline_beta}\n\nURL: {url_beta}\nPublished: {today}\n"
        f"Source: antiwar_news\nType: article\n\nRSS article text.",
        encoding="utf-8",
    )

    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )
    work_dir = tmp_path / "work"

    def _fake_extract_run1(url: str) -> str:
        if url == url_alpha:
            return "TARGET-ALPHA " * 60
        if url == url_beta:
            return "TARGET-BETA " * 60
        return ""

    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text", _fake_extract_run1
    )
    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    shared_path = (
        work_dir
        / "articles"
        / "rss"
        / "antiwar_news"
        / f"{_slugify(headline_alpha)}.md"
    )
    # Last-write-wins on the shared path is a pre-existing, accepted
    # collision (it never used to reach rss_articles_data). Confirm the
    # setup produced it, as the premise for what follows.
    assert "TARGET-BETA" in shared_path.read_text()

    calls: list[str] = []

    def _fake_extract_run2(url: str) -> str:
        calls.append(url)
        return f"REFETCHED for {url} " * 60

    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text", _fake_extract_run2
    )
    captured: dict = {}

    def _fake_plan(headlines, **kwargs):
        captured["headlines"] = headlines
        return _make_empty_plan()

    monkeypatch.setattr("pipeline.fp_collector.generate_fp_research_plan", _fake_plan)

    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    headlines = captured["headlines"]
    alpha_snippet = next(h for h in headlines if "Report Alpha" in h)
    beta_snippet = next(h for h in headlines if "Report Beta" in h)

    # The regression: alpha's snippet silently inherited beta's cached body
    # via the shared path, with no fetch made for alpha's own URL.
    assert "TARGET-BETA" not in alpha_snippet
    assert url_alpha in calls

    # Beta's own URL matches what's on disk, so it is a legitimate reuse and
    # must not regress into a wasted re-fetch.
    assert url_beta not in calls
    assert "TARGET-BETA" in beta_snippet


def test_retry_reuse_decodes_before_comparing_to_a_predecode_work_dir(
    tmp_path, monkeypatch
):
    """A work dir written by the pre-full-text-fetch code holds a raw,
    entity-encoded excerpt. The new code decodes the cache excerpt on read,
    which makes it shorter — so comparing the old raw body's length to the
    newly-decoded excerpt's length is not apples to apples, and the raw
    body looks "longer" (and gets falsely "reused") purely from encoding,
    not because any fetch happened. The comparison must decode the
    work-dir body first.
    """
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()

    headline = "Old Deploy Article"
    url = "https://news.antiwar.com/old-deploy/"
    raw_encoded_body = "he called it a &#8220;landing ship&#8221; [&#8230;]"

    _write_rss_cache_file(
        rss_cache,
        today,
        "antiwar_news",
        headline,
        url,
        text=raw_encoded_body,
    )

    work_dir = tmp_path / "work"
    articles_rss_dir = work_dir / "articles" / "rss" / "antiwar_news"
    articles_rss_dir.mkdir(parents=True)
    art_path = articles_rss_dir / f"{_slugify(headline)}.md"
    # Hand-write the work-dir article exactly as the pre-diff code would
    # have left it: the raw, still-encoded excerpt, untouched by any fetch.
    art_path.write_text(
        f"# {headline}\n\nURL: {url}\nSource: antiwar_news\n\n{raw_encoded_body}",
        encoding="utf-8",
    )

    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: _make_empty_plan(),
    )

    calls: list[str] = []

    def _fake_extract(u: str) -> str:
        calls.append(u)
        return "FRESH FETCHED TEXT " * 50

    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", _fake_extract)

    collect_fp_artifacts(
        "job-1",
        work_dir,
        scripts_source_dir=tmp_path / "scripts",
        fp_routed_dir=tmp_path / "routed",
        homepage_cache_dir=tmp_path / "hp",
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=tmp_path / "sem",
    )

    assert calls == [url]
    assert "FRESH FETCHED TEXT" in art_path.read_text()
    assert "&#8230;" not in art_path.read_text()
