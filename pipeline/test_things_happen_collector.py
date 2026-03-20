from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pipeline.article_fetcher import Article
from pipeline.exa_client import ExaResult
from pipeline.things_happen_collector import _slugify, collect_all_artifacts
from pipeline.things_happen_editor import RundownResearchPlan, RundownStoryDirective


def test_slugify() -> None:
    assert _slugify("Hello World!") == "hello-world"
    assert _slugify("A -- B") == "a-b"
    assert (
        _slugify("Very Long Headline That Exceeds Fifty Characters Limit")
        == "very-long-headline-that-exceeds-fifty-characters-l"
    )


@patch("pipeline.things_happen_collector.fetch_all_articles")
@patch("pipeline.things_happen_collector.resolve_redirect_url")
@patch("pipeline.things_happen_collector.generate_rundown_research_plan")
@patch("pipeline.things_happen_collector.search_related")
@patch("pipeline.things_happen_collector.sync_zvi_cache")
def test_collect_all_artifacts(
    mock_zvi,
    mock_exa,
    mock_plan,
    mock_resolve,
    mock_fetch,
    tmp_path,
) -> None:
    # Setup mocks
    mock_resolve.return_value = "http://resolved.com"

    mock_fetch.return_value = [
        Article(
            headline="Test Article", url="http://resolved.com", content="Full text here"
        )
    ]

    mock_plan.return_value = RundownResearchPlan(
        themes=["Tech"],
        directives=[
            RundownStoryDirective(
                headline="Test Article",
                source="levine",
                priority=1,
                theme="Tech",
                needs_exa=True,
                exa_query="exa test",
                is_foreign_policy=False,
                fp_query="",
                include_in_episode=True,
            )
        ],
    )

    mock_exa.return_value = [ExaResult(title="Exa", url="http://exa", text="Exa text")]

    # Setup fake context scripts
    scripts_dir = tmp_path / "scripts" / "the-rundown"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "2026-03-01.txt").write_text("old script")

    # Create Levine cache with today's date JSON file
    _et = ZoneInfo("America/New_York")
    today = datetime.now(tz=_et).strftime("%Y-%m-%d")
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    links = [{"raw_url": "http://raw.com", "headline_context": "context"}]
    (levine_cache / f"{today}.json").write_text(json.dumps(links))

    # Run collector with empty semafor cache dir
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    work_dir = tmp_path / "work"

    collect_all_artifacts(
        "job123",
        work_dir,
        levine_cache_dir=levine_cache,
        scripts_source_dir=scripts_dir,
        semafor_cache_dir=semafor_cache,
    )

    # Verify directories created
    assert (work_dir / "articles").exists()
    assert (work_dir / "enrichment" / "exa").exists()
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

    # Verify plan.json written
    plan_path = work_dir / "plan.json"
    assert plan_path.exists()
    import json as _json

    plan_data = _json.loads(plan_path.read_text())
    assert "themes" in plan_data
    assert "directives" in plan_data

    # Verify headline_index.json written with Levine article
    index_path = work_dir / "headline_index.json"
    assert index_path.exists()
    index = _json.loads(index_path.read_text())
    assert "Test Article" in index
    assert index["Test Article"].startswith("articles/")


def test_fp_links_routed_to_staging(tmp_path, monkeypatch):
    """FP-flagged links are written to fp-routed-links dir, not enriched."""
    # Mock all external calls
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

    # Editor returns one FP link and one non-FP link
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_rundown_research_plan",
        lambda *a, **kw: RundownResearchPlan(
            themes=["FP", "Crypto"],
            directives=[
                RundownStoryDirective(
                    headline="Iran War Escalates",
                    source="levine",
                    priority=1,
                    theme="FP",
                    needs_exa=False,
                    exa_query="",
                    is_foreign_policy=True,
                    fp_query="iran war",
                    include_in_episode=False,
                ),
                RundownStoryDirective(
                    headline="Bitcoin Rises",
                    source="levine",
                    priority=2,
                    theme="Crypto",
                    needs_exa=False,
                    exa_query="",
                    is_foreign_policy=False,
                    fp_query="",
                    include_in_episode=True,
                ),
            ],
        ),
    )

    routed_dir = tmp_path / "fp-routed"
    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()

    # Write links to levine cache
    _et = ZoneInfo("America/New_York")
    today = datetime.now(tz=_et).strftime("%Y-%m-%d")
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    links_raw = [
        {"raw_url": "https://example.com/iran", "headline": "Iran War Escalates"},
        {"raw_url": "https://example.com/btc", "headline": "Bitcoin Rises"},
    ]
    (levine_cache / f"{today}.json").write_text(json.dumps(links_raw))

    collect_all_artifacts(
        "test-job",
        work_dir,
        levine_cache_dir=levine_cache,
        scripts_source_dir=scripts_dir,
        fp_routed_dir=routed_dir,
        semafor_cache_dir=semafor_cache,
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
        "pipeline.things_happen_collector.generate_rundown_research_plan",
        lambda *a, **kw: RundownResearchPlan(themes=[], directives=[]),
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )

    today = datetime.now(tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    # Pre-populate semafor cache with today's articles
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    (semafor_cache / f"{today}-tech-company-ipo.md").write_text(
        f"# Tech Company IPO\n\nURL: https://semafor.com/tech-ipo\nPublished: {today}\nSource: semafor\nCategory: Technology\nType: article\n\nA tech company goes public"
    )
    (semafor_cache / f"{today}-gulf-tensions-rise.md").write_text(
        f"# Gulf Tensions Rise\n\nURL: https://semafor.com/gulf-tensions\nPublished: {today}\nSource: semafor\nCategory: Gulf\nType: article\n\nTensions in the Gulf"
    )

    # Empty levine cache
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_all_artifacts(
        "test-job",
        work_dir,
        levine_cache_dir=levine_cache,
        scripts_source_dir=scripts_dir,
        semafor_cache_dir=semafor_cache,
    )

    # Tech article should appear (category=Technology -> "th")
    semafor_dir = work_dir / "articles" / "semafor"
    assert semafor_dir.exists()
    semafor_files = list(semafor_dir.glob("*.md"))
    # Only the Tech article, not the Gulf one (FP-only)
    assert len(semafor_files) == 1
    assert "Tech Company IPO" in semafor_files[0].read_text()

    # Semafor headline should be in headline_index.json
    index_path = work_dir / "headline_index.json"
    assert index_path.exists()
    index = json.loads(index_path.read_text())
    assert "Tech Company IPO" in index
    assert "semafor/" in index["Tech Company IPO"]


def test_semafor_headlines_sent_to_editor(tmp_path, monkeypatch):
    """Semafor TH articles are included in headlines_with_snippets for the editor."""
    captured_snippets = []

    def mock_plan(headlines_with_snippets, **kwargs):
        captured_snippets.extend(headlines_with_snippets)
        return RundownResearchPlan(themes=[], directives=[])

    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_rundown_research_plan", mock_plan
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )

    today = datetime.now(tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    (semafor_cache / f"{today}-pwc-ai.md").write_text(
        f"# PwC US boss tells staff to embrace AI\n\nURL: https://semafor.com/pwc-ai\nPublished: {today}\nSource: semafor\nCategory: CEO\nType: article\n\nPwC's US chief tells employees to integrate AI into daily work."
    )

    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    work_dir = tmp_path / "work"

    collect_all_artifacts(
        "test-semafor-snippets",
        work_dir,
        levine_cache_dir=levine_cache,
        semafor_cache_dir=semafor_cache,
    )

    semafor_snippets = [s for s in captured_snippets if "[semafor]" in s]
    assert len(semafor_snippets) == 1
    assert "PwC US boss tells staff to embrace AI" in semafor_snippets[0]


def test_zvi_articles_added_to_work_dir(tmp_path, monkeypatch):
    """Zvi posts published today appear in work_dir/articles/zvi/."""
    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_rundown_research_plan",
        lambda *a, **kw: RundownResearchPlan(themes=[], directives=[]),
    )

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

    # Empty levine cache
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()

    collect_all_artifacts(
        "test-job",
        work_dir,
        levine_cache_dir=levine_cache,
        scripts_source_dir=scripts_dir,
        zvi_cache_dir=zvi_cache,
        semafor_cache_dir=semafor_cache,
    )

    zvi_dir = work_dir / "articles" / "zvi"
    assert zvi_dir.exists()
    zvi_files = list(zvi_dir.glob("*.md"))
    assert len(zvi_files) == 1
    assert "Fresh Essay" in zvi_files[0].read_text()


def test_semafor_reads_from_cache(tmp_path, monkeypatch):
    """Semafor TH articles are read from cache with lookback window."""
    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_rundown_research_plan",
        lambda *a, **kw: RundownResearchPlan(themes=[], directives=[]),
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )

    _et = ZoneInfo("America/New_York")
    today = datetime.now(tz=_et).strftime("%Y-%m-%d")
    yesterday = (datetime.now(tz=_et) - timedelta(days=1)).strftime("%Y-%m-%d")
    old_date = "2026-01-01"

    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()

    # TH-category article from today — should be included
    (semafor_cache / f"{today}-tech-ipo.md").write_text(
        f"# Tech Company IPO\n\nURL: https://semafor.com/tech-ipo\nPublished: {today}\nSource: semafor\nCategory: Technology\nType: article\n\nA tech company goes public"
    )

    # FP-only category article from today — should be excluded
    (semafor_cache / f"{today}-gulf-tensions.md").write_text(
        f"# Gulf Tensions Rise\n\nURL: https://semafor.com/gulf-tensions\nPublished: {today}\nSource: semafor\nCategory: Gulf\nType: article\n\nTensions in the Gulf"
    )

    # TH-category article from yesterday — should be included (within lookback)
    (semafor_cache / f"{yesterday}-business-deal.md").write_text(
        f"# Big Business Deal\n\nURL: https://semafor.com/biz-deal\nPublished: {yesterday}\nSource: semafor\nCategory: Business\nType: article\n\nA major acquisition announced"
    )

    # TH-category article from old date — should be excluded (outside lookback)
    (semafor_cache / f"{old_date}-old-tech.md").write_text(
        f"# Old Tech Story\n\nURL: https://semafor.com/old-tech\nPublished: {old_date}\nSource: semafor\nCategory: Technology\nType: article\n\nAn old tech story"
    )

    # Empty levine cache
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_all_artifacts(
        "test-job",
        work_dir,
        levine_cache_dir=levine_cache,
        scripts_source_dir=scripts_dir,
        semafor_cache_dir=semafor_cache,
        lookback_days=2,
    )

    semafor_dir = work_dir / "articles" / "semafor"
    assert semafor_dir.exists()
    semafor_files = list(semafor_dir.glob("*.md"))

    # Only today's TH article and yesterday's TH article should be included
    assert len(semafor_files) == 2
    contents = [f.read_text() for f in semafor_files]
    headlines = [c.split("\n")[0].lstrip("# ").strip() for c in contents]
    assert "Tech Company IPO" in headlines
    assert "Big Business Deal" in headlines
    # Gulf (FP-only) and Old Tech Story (outside lookback) should NOT be present
    assert "Gulf Tensions Rise" not in headlines
    assert "Old Tech Story" not in headlines


def test_headline_index_includes_zvi(tmp_path, monkeypatch):
    """Zvi section titles are included in headline_index.json."""
    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_rundown_research_plan",
        lambda *a, **kw: RundownResearchPlan(themes=[], directives=[]),
    )

    zvi_cache = tmp_path / "zvi-cache"
    zvi_cache.mkdir()
    today = datetime.now(tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    (zvi_cache / f"{today}-chip-city.md").write_text(
        f"# Chip City\n\nPost: AI Weekly\nURL: https://zvi.com/ai\nPublished: {today}\n"
        "Type: roundup-section\n\n"
        "Nvidia to spend $26 billion to build open weight AI models."
    )

    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )

    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    work_dir = tmp_path / "work"

    collect_all_artifacts(
        "test-zvi-index",
        work_dir,
        levine_cache_dir=levine_cache,
        semafor_cache_dir=tmp_path / "semafor",
        zvi_cache_dir=zvi_cache,
    )

    index_path = work_dir / "headline_index.json"
    assert index_path.exists()
    index = json.loads(index_path.read_text())
    assert "Chip City" in index
    assert "zvi/" in index["Chip City"]


def test_find_rundown_article_text_fuzzy_matches_zvi(tmp_path):
    """Fuzzy lookup finds Zvi article when editor reformulates the headline."""
    # Setup a work dir with a Zvi article and headline index
    articles_dir = tmp_path / "articles" / "zvi"
    articles_dir.mkdir(parents=True)
    zvi_file = articles_dir / "2026-03-19-chip-city.md"
    zvi_file.write_text(
        "# Chip City\n\nPost: AI Weekly\nURL: https://zvi.com/ai\n\n"
        "Nvidia to spend $26 billion to build open weight AI models. "
        "The company announced plans for inference workloads."
    )

    index = {"Chip City": "articles/zvi/2026-03-19-chip-city.md"}
    (tmp_path / "headline_index.json").write_text(json.dumps(index))

    from pipeline.__main__ import _find_rundown_article_text

    class FakeDirective:
        # Editor reformulated "Chip City" into a descriptive headline
        headline = "Nvidia to spend $26 billion to build open weight AI models"
        source = "zvi"

    text = _find_rundown_article_text(FakeDirective(), tmp_path)
    assert text
    assert "Chip City" in text
    assert "Nvidia" in text


def test_find_rundown_article_text_exact_match(tmp_path):
    """Exact headline match in index takes priority."""
    articles_dir = tmp_path / "articles"
    articles_dir.mkdir(parents=True)
    art_file = articles_dir / "00-test-article.md"
    art_file.write_text("# Test Article\n\nURL: http://example.com\n\nContent here.")

    index = {"Test Article": "articles/00-test-article.md"}
    (tmp_path / "headline_index.json").write_text(json.dumps(index))

    from pipeline.__main__ import _find_rundown_article_text

    class FakeDirective:
        headline = "Test Article"
        source = "levine"

    text = _find_rundown_article_text(FakeDirective(), tmp_path)
    assert text
    assert "Test Article" in text


def test_find_rundown_article_text_no_false_match(tmp_path):
    """Fuzzy lookup does not match unrelated articles."""
    articles_dir = tmp_path / "articles" / "zvi"
    articles_dir.mkdir(parents=True)
    zvi_file = articles_dir / "2026-03-19-dog-story.md"
    zvi_file.write_text(
        "# The Lighter Side\n\nPost: AI Weekly\nURL: https://zvi.com/ai\n\n"
        "A heartwarming story about a dog who learned to paint."
    )

    index = {"The Lighter Side": "articles/zvi/2026-03-19-dog-story.md"}
    (tmp_path / "headline_index.json").write_text(json.dumps(index))

    from pipeline.__main__ import _find_rundown_article_text

    class FakeDirective:
        # Completely unrelated headline
        headline = "Federal Reserve raises interest rates to combat inflation"
        source = "zvi"

    text = _find_rundown_article_text(FakeDirective(), tmp_path)
    assert text == ""


def test_collector_works_without_levine_links(tmp_path, monkeypatch):
    """Collector proceeds with Semafor + Zvi when no Levine links exist."""
    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_rundown_research_plan",
        lambda *a, **kw: RundownResearchPlan(themes=[], directives=[]),
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )

    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()  # empty
    work_dir = tmp_path / "work"
    collect_all_artifacts(
        "job-no-levine",
        work_dir,
        levine_cache_dir=levine_cache,
        semafor_cache_dir=tmp_path / "semafor",  # empty
        zvi_cache_dir=tmp_path / "zvi",  # empty
    )
    assert work_dir.exists()
    assert (work_dir / "articles").exists()
