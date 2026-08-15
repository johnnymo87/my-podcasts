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
@patch("pipeline.things_happen_collector.search_related_status")
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

    mock_exa.return_value = (
        [ExaResult(title="Exa", url="http://exa", text="Exa text")],
        "hit",
    )

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


def test_prior_urls_are_not_fetched(tmp_path, monkeypatch):
    """Dedup must happen before the HTTP fetch, not after."""
    fetched_urls: list[str] = []

    def fake_fetch_all_articles(links, delay_between=1.0):
        fetched_urls.extend(link["resolved_url"] for link in links)
        return [
            Article(
                headline=link["headline"],
                url=link["resolved_url"],
                content="content",
            )
            for link in links
        ]

    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles", fake_fetch_all_articles
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
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    links_raw = [
        {"raw_url": "https://example.com/covered", "headline": "Covered Story"},
        {"raw_url": "https://example.com/fresh", "headline": "Fresh Story"},
    ]
    (levine_cache / f"{today}.json").write_text(json.dumps(links_raw))

    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    work_dir = tmp_path / "work"

    collect_all_artifacts(
        "job-dedup-test",
        work_dir,
        levine_cache_dir=levine_cache,
        semafor_cache_dir=semafor_cache,
        prior_urls={"https://example.com/covered"},
    )

    # Dedup must happen before fetch: the fetcher must only be handed the
    # fresh URL, never the already-covered one.
    assert fetched_urls == ["https://example.com/fresh"]

    # Existing behavior preserved: the covered article never appears in the
    # collector's output artifacts.
    index = json.loads((work_dir / "headline_index.json").read_text())
    assert "Covered Story" not in index
    assert "Fresh Story" in index
    art_files = list((work_dir / "articles").glob("*.md"))
    assert len(art_files) == 1
    assert "Fresh Story" in art_files[0].read_text()

    # The sentinel records the dedup that just happened, so a later run can be
    # told apart from a run that simply had nothing to collect.
    sentinel = json.loads((work_dir / "collection_done.json").read_text())
    assert sentinel["levine_candidates"] == 2
    assert sentinel["levine_deduped"] == 1
    assert sentinel["levine_articles"] == 1


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
        "pipeline.things_happen_collector.search_related_status",
        lambda *a, **kw: ([], "empty"),
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


def test_find_rundown_article_text_exa_hit(tmp_path):
    """Exa fallback returns the body when the file reports a hit."""
    exa_dir = tmp_path / "enrichment" / "exa"
    exa_dir.mkdir(parents=True)
    slug = _slugify("Paywalled Story")
    (exa_dir / f"{slug}.md").write_text(
        "Result: hit\n\n# Paywalled Story\n\nURL: https://example.com/paywalled\n\n"
        "Full article text recovered via Exa search."
    )

    from pipeline.__main__ import _find_rundown_article_text

    class FakeDirective:
        headline = "Paywalled Story"
        source = "levine"

    text = _find_rundown_article_text(FakeDirective(), tmp_path)
    assert "Full article text recovered via Exa search." in text


def test_find_rundown_article_text_exa_empty_result_gated(tmp_path):
    """Exa fallback must not surface a `Result: empty` stub as article text."""
    exa_dir = tmp_path / "enrichment" / "exa"
    exa_dir.mkdir(parents=True)
    slug = _slugify("No Results Story")
    (exa_dir / f"{slug}.md").write_text("Result: empty\n\nNo results found for query.")

    from pipeline.__main__ import _find_rundown_article_text

    class FakeDirective:
        headline = "No Results Story"
        source = "levine"

    text = _find_rundown_article_text(FakeDirective(), tmp_path)
    assert text == ""


def test_find_rundown_article_text_exa_headerless_still_trusted(tmp_path):
    """Headerless Exa files (FP format) are trusted -- this branch is permanent."""
    exa_dir = tmp_path / "enrichment" / "exa"
    exa_dir.mkdir(parents=True)
    slug = _slugify("Legacy Format Story")
    (exa_dir / f"{slug}.md").write_text(
        "# Legacy Format Story\n\nURL: https://example.com/legacy\n\n"
        "Article text with no Result header at all."
    )

    from pipeline.__main__ import _find_rundown_article_text

    class FakeDirective:
        headline = "Legacy Format Story"
        source = "levine"

    text = _find_rundown_article_text(FakeDirective(), tmp_path)
    assert "Article text with no Result header at all." in text


def test_semafor_routing_header_preferred_over_category(tmp_path, monkeypatch):
    """Routing: header overrides Category-based routing for Semafor articles."""
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
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()

    # Article 1: Category=Gulf (FP-only by default), but Routing=th → should be INCLUDED
    (semafor_cache / f"{today}-gulf-with-th-routing.md").write_text(
        f"# Gulf Story With TH Routing\n\nURL: https://semafor.com/gulf-th\nPublished: {today}\nSource: semafor\nCategory: Gulf\nRouting: th\nType: article\n\nA Gulf story routed to TH."
    )

    # Article 2: Category empty, Routing=fp → should be EXCLUDED from Rundown
    (semafor_cache / f"{today}-fp-only-article.md").write_text(
        f"# FP Only Article\n\nURL: https://semafor.com/fp-only\nPublished: {today}\nSource: semafor\nCategory: \nRouting: fp\nType: article\n\nAn FP-only article."
    )

    # Article 3: Category empty, Routing=skip → should be EXCLUDED
    (semafor_cache / f"{today}-skipped-article.md").write_text(
        f"# Skipped Article\n\nURL: https://semafor.com/skip\nPublished: {today}\nSource: semafor\nCategory: \nRouting: skip\nType: article\n\nThis article should be skipped."
    )

    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    work_dir = tmp_path / "work"

    collect_all_artifacts(
        "test-routing-header",
        work_dir,
        levine_cache_dir=levine_cache,
        semafor_cache_dir=semafor_cache,
    )

    semafor_dir = work_dir / "articles" / "semafor"
    assert semafor_dir.exists()
    semafor_files = list(semafor_dir.glob("*.md"))

    # Only the first article (Routing: th) should be included
    assert len(semafor_files) == 1
    content = semafor_files[0].read_text()
    assert "Gulf Story With TH Routing" in content
    assert "FP Only Article" not in content
    assert "Skipped Article" not in content


def _run_collector_with_exa(
    tmp_path,
    monkeypatch,
    exa_status: str = "hit",
    headline: str = "Paywalled Story",
):
    """Run the collector with search_related_status stubbed to a given outcome.

    Mirrors the monkeypatch style already used in this file for
    fetch_all_articles / resolve_redirect_url / generate_rundown_research_plan
    / sync_zvi_cache. Produces a plan with exactly one non-FP directive that
    needs Exa. Returns work_dir.
    """
    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_rundown_research_plan",
        lambda *a, **kw: RundownResearchPlan(
            themes=["Tech"],
            directives=[
                RundownStoryDirective(
                    headline=headline,
                    source="levine",
                    priority=1,
                    theme="Tech",
                    needs_exa=True,
                    exa_query="exa test query",
                    is_foreign_policy=False,
                    fp_query="",
                    include_in_episode=True,
                )
            ],
        ),
    )

    exa_results = (
        [ExaResult(title="Exa", url="http://exa", text="Exa text")]
        if exa_status == "hit"
        else []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.search_related_status",
        lambda *a, **kw: (exa_results, exa_status),
    )

    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    zvi_cache = tmp_path / "zvi-cache"
    zvi_cache.mkdir()
    work_dir = tmp_path / "work"

    collect_all_artifacts(
        "job-exa-test",
        work_dir,
        levine_cache_dir=levine_cache,
        semafor_cache_dir=semafor_cache,
        zvi_cache_dir=zvi_cache,
    )
    return work_dir


def test_exa_hit_writes_bare_slug_filename(tmp_path, monkeypatch):
    """The write path must use the bare slug the readers look up -- no index prefix."""
    headline = "Paywalled Story"
    work_dir = _run_collector_with_exa(
        tmp_path, monkeypatch, exa_status="hit", headline=headline
    )
    expected = work_dir / "enrichment" / "exa" / f"{_slugify(headline)}.md"
    assert expected.exists()
    assert "Result: hit" in expected.read_text()


def test_exa_empty_writes_file_with_status(tmp_path, monkeypatch):
    """A miss is still written to disk, so it's observable rather than absent."""
    headline = "Quiet Story"
    work_dir = _run_collector_with_exa(
        tmp_path, monkeypatch, exa_status="empty", headline=headline
    )
    expected = work_dir / "enrichment" / "exa" / f"{_slugify(headline)}.md"
    assert expected.exists()
    assert "Result: empty" in expected.read_text()


def test_exa_error_writes_file_with_status(tmp_path, monkeypatch):
    """An Exa exception becomes data in the file rather than a swallowed error."""
    headline = "Broken Story"
    work_dir = _run_collector_with_exa(
        tmp_path, monkeypatch, exa_status="error:RuntimeError", headline=headline
    )
    expected = work_dir / "enrichment" / "exa" / f"{_slugify(headline)}.md"
    assert expected.exists()
    assert "Result: error:RuntimeError" in expected.read_text()


def test_exa_reader_e2e_fallback_matches_writer(tmp_path, monkeypatch):
    """Collector's write path and __main__ reader's lookup path agree.

    Runs the real collector with an Exa hit, then calls the real reader with a
    directive whose headline is absent from headline_index.json and has no
    matching article file -- forcing control through the index exact match,
    the fuzzy word-overlap match, and the legacy slug fallback before it can
    reach the Exa fallback. This is the test that would have caught the
    original bug: each half wrote/read `{slug}.md` independently and the two
    conventions had silently drifted apart.
    """
    # A real Levine article populates headline_index.json with genuine
    # content, so the fuzzy-match loop actually runs (not vacuously, over an
    # empty index) and must correctly find no overlap with our Exa headline.
    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles",
        lambda *a, **kw: [
            Article(
                headline="Local Bakery Wins Award",
                url="http://resolved.example/bakery",
                content="A neighborhood bakery took home a regional pastry prize.",
            )
        ],
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )

    exa_headline = "Quantum Sensor Startup Raises Funding Round"
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_rundown_research_plan",
        lambda *a, **kw: RundownResearchPlan(
            themes=["Tech"],
            directives=[
                RundownStoryDirective(
                    headline=exa_headline,
                    source="levine",
                    priority=1,
                    theme="Tech",
                    needs_exa=True,
                    exa_query="quantum sensor startup funding",
                    is_foreign_policy=False,
                    fp_query="",
                    include_in_episode=True,
                )
            ],
        ),
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.search_related_status",
        lambda *a, **kw: (
            [
                ExaResult(
                    title="Coverage",
                    url="http://exa/quantum",
                    text="Exa recovered body text.",
                )
            ],
            "hit",
        ),
    )

    _et = ZoneInfo("America/New_York")
    today = datetime.now(tz=_et).strftime("%Y-%m-%d")
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    (levine_cache / f"{today}.json").write_text(
        json.dumps([{"raw_url": "http://raw.example/bakery", "headline": "bakery"}])
    )
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    zvi_cache = tmp_path / "zvi-cache"
    zvi_cache.mkdir()
    work_dir = tmp_path / "work"

    collect_all_artifacts(
        "job-exa-e2e",
        work_dir,
        levine_cache_dir=levine_cache,
        semafor_cache_dir=semafor_cache,
        zvi_cache_dir=zvi_cache,
    )

    # Sanity: the bakery article populated the index; our Exa headline did not.
    index = json.loads((work_dir / "headline_index.json").read_text())
    assert exa_headline not in index
    assert "Local Bakery Wins Award" in index

    from pipeline.__main__ import _find_rundown_article_text

    class FakeDirective:
        headline = exa_headline
        source = "levine"

    text = _find_rundown_article_text(FakeDirective(), work_dir)
    assert "Exa recovered body text." in text

    # Prove this isn't a tautology: remove the Exa file the collector wrote
    # and confirm the reader now finds nothing -- i.e. the fallback really was
    # reached rather than short-circuiting on an earlier lookup.
    exa_file = work_dir / "enrichment" / "exa" / f"{_slugify(exa_headline)}.md"
    assert exa_file.exists()
    exa_file.unlink()
    text_after_removal = _find_rundown_article_text(FakeDirective(), work_dir)
    assert text_after_removal == ""


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
