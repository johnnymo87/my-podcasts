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
    assert sentinel["candidates"]["levine"] == 2
    assert sentinel["deduped"]["levine"] == 1
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


def test_find_rundown_article_source_no_false_match(tmp_path):
    """Word-overlap match does not match unrelated articles.

    Was test_find_rundown_article_text_no_false_match, exercised through the
    now-deleted _find_rundown_article_text wrapper. _find_rundown_article_text
    was the dry-run path's own drifted duplicate of
    consumer._assemble_writer_inputs (my-podcasts-a3x); --dry-run now calls
    the real assembler, so the wrapper is gone. This scenario has no
    equivalent among the find_rundown_article_source tests below, so it is
    kept, converted to call the underlying resolver directly.
    """
    articles_dir = tmp_path / "articles" / "zvi"
    articles_dir.mkdir(parents=True)
    zvi_file = articles_dir / "2026-03-19-dog-story.md"
    zvi_file.write_text(
        "# The Lighter Side\n\nPost: AI Weekly\nURL: https://zvi.com/ai\n\n"
        "A heartwarming story about a dog who learned to paint."
    )

    index = {"The Lighter Side": "articles/zvi/2026-03-19-dog-story.md"}
    (tmp_path / "headline_index.json").write_text(json.dumps(index))

    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        # Completely unrelated headline
        headline = "Federal Reserve raises interest rates to combat inflation"
        source = "zvi"

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert text == ""
    assert path is None


def test_find_rundown_article_source_exa_headerless_file_no_longer_surfaced(tmp_path):
    """Headerless Exa files (FP format, no `## [title](url)` sections) are no
    longer surfaced by the Rundown resolver's Exa fallback.

    Was test_find_rundown_article_text_exa_headerless_file_no_longer_surfaced,
    exercised through the now-deleted _find_rundown_article_text wrapper (see
    test_find_rundown_article_source_no_false_match above for why). No
    equivalent exists among the tests below, so it is kept, converted to
    call the underlying resolver directly.

    Before this change the resolver used `exa_text_if_hit`, which trusts any
    headerless file unconditionally (that trust is still correct and is
    tested directly against `exa_text_if_hit` in test_exa_client.py -- this
    test is about the resolver, not that function). The resolver now uses
    `exa_result_sections`, which additionally requires `## [title](url)`
    sections; a headerless file has none, so it yields no text here. This
    is not a regression for The Rundown: its own collector
    (things_happen_collector.py Phase 3) always writes headers plus `## [`
    sections for a hit. The headerless shape is FP-only and FP does not read
    through this resolver.
    """
    exa_dir = tmp_path / "enrichment" / "exa"
    exa_dir.mkdir(parents=True)
    slug = _slugify("Legacy Format Story")
    (exa_dir / f"{slug}.md").write_text(
        "# Legacy Format Story\n\nURL: https://example.com/legacy\n\n"
        "Article text with no Result header at all."
    )

    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        headline = "Legacy Format Story"
        source = "levine"

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert text == ""
    assert path is None


def test_find_rundown_article_source_reports_path_on_exact_match(tmp_path):
    """Exact index match (return site 1) reports the work-dir-relative path."""
    from pipeline.__main__ import find_rundown_article_source

    index = {"Some Headline": "articles/00-some-headline.md"}
    (tmp_path / "articles").mkdir()
    (tmp_path / "articles" / "00-some-headline.md").write_text(
        "# Some Headline\n\nURL: u\n\nbody text here", encoding="utf-8"
    )
    (tmp_path / "headline_index.json").write_text(json.dumps(index))

    class FakeDirective:
        headline = "Some Headline"
        source = ""

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert "body text here" in text
    assert path == "articles/00-some-headline.md"


def test_find_rundown_article_source_reports_path_on_word_overlap_match(tmp_path):
    """Word-overlap index match (return site 2) reports the resolved path."""
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

    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        # Editor reformulated "Chip City" into a descriptive headline
        headline = "Nvidia to spend $26 billion to build open weight AI models"
        source = "zvi"

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert "Nvidia" in text
    assert path == "articles/zvi/2026-03-19-chip-city.md"


def test_find_rundown_article_source_reports_path_on_legacy_flat_match(tmp_path):
    """Legacy flat Levine glob (return site 3) reports its own relative path."""
    articles_dir = tmp_path / "articles"
    articles_dir.mkdir(parents=True)
    slug = _slugify("Flat Levine Story")
    art_file = articles_dir / f"00-{slug}.md"
    art_file.write_text("# Flat Levine Story\n\nURL: u\n\nFlat body text.")

    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        headline = "Flat Levine Story"
        source = "levine"

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert "Flat body text." in text
    assert path == f"articles/00-{slug}.md"


def test_find_rundown_article_source_reports_path_on_legacy_semafor_match(tmp_path):
    """Legacy Semafor slug match (return site 4) reports its relative path."""
    semafor_dir = tmp_path / "articles" / "semafor"
    semafor_dir.mkdir(parents=True)
    slug = _slugify("Semafor Story")
    semafor_file = semafor_dir / f"{slug}.md"
    semafor_file.write_text("# Semafor Story\n\nURL: u\n\nSemafor body text.")

    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        headline = "Semafor Story"
        source = "semafor"

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert "Semafor body text." in text
    assert path == f"articles/semafor/{slug}.md"


def test_find_rundown_article_source_reports_path_on_legacy_zvi_glob_match(tmp_path):
    """Legacy Zvi glob match (return site 5) reports its own relative path."""
    zvi_dir = tmp_path / "articles" / "zvi"
    zvi_dir.mkdir(parents=True)
    slug = _slugify("Zvi Glob Story")
    zvi_file = zvi_dir / f"2026-03-19-{slug}.md"
    zvi_file.write_text("# Zvi Glob Story\n\nURL: u\n\nZvi body text.")

    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        headline = "Zvi Glob Story"
        source = "zvi"

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert "Zvi body text." in text
    assert path == f"articles/zvi/2026-03-19-{slug}.md"


def test_find_rundown_article_source_reports_path_on_exa_hit(tmp_path):
    """Exa hit (return site 6) reports the Exa enrichment file's relative path."""
    exa_dir = tmp_path / "enrichment" / "exa"
    exa_dir.mkdir(parents=True)
    slug = _slugify("Paywalled Story")
    exa_file = exa_dir / f"{slug}.md"
    # Production shape (things_happen_collector.py Phase 3): bookkeeping
    # headers followed by "## [title](url)" result sections.
    exa_file.write_text(
        "# Exa Results for: Paywalled Story\nResult: hit\nQuery: paywalled story\n\n"
        "## [Paywalled Story](https://example.com/paywalled)\n"
        "Full article text recovered via Exa search.\n\n"
    )

    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        headline = "Paywalled Story"
        source = "levine"

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert "Full article text recovered via Exa search." in text
    assert path == f"enrichment/exa/{slug}.md"


def test_find_rundown_article_source_exa_gated_miss_returns_none(tmp_path):
    """The Exa gate rejecting a `Result: empty` stub (site 6, miss branch) must
    fall through to the final miss (site 7): no text, no path -- not the path
    to a file whose text is being withheld."""
    exa_dir = tmp_path / "enrichment" / "exa"
    exa_dir.mkdir(parents=True)
    slug = _slugify("No Results Story")
    (exa_dir / f"{slug}.md").write_text("Result: empty\n\nNo results found for query.")

    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        headline = "No Results Story"
        source = "levine"

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert text == ""
    assert path is None


def test_find_rundown_article_source_reports_miss(tmp_path):
    """Final fallback (return site 7) reports no text and no path."""
    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        headline = "Nothing Matches This"
        source = ""

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert text == ""
    assert path is None


def test_exa_fallback_has_no_bookkeeping_headers(tmp_path):
    """Exa fallback (site 6) must not leak Result:/Query: metadata into the
    writer prompt -- article text is fed verbatim, and those lines are
    bookkeeping written by the collector, not article content."""
    from pipeline.__main__ import find_rundown_article_source
    from pipeline.exa_client import exa_file_path
    from pipeline.things_happen_collector import _slugify

    class FakeDirective:
        headline = "Some Very Distinctive Headline About Widgets"
        source = ""

    slug = _slugify(FakeDirective.headline)
    exa_path = exa_file_path(tmp_path, slug)
    exa_path.parent.mkdir(parents=True, exist_ok=True)
    exa_path.write_text(
        "# Exa Results for: X\nResult: hit\nQuery: widgets\n\n"
        "## [Widget News](https://w.example)\nReal body text.\n\n",
        encoding="utf-8",
    )
    text, src = find_rundown_article_source(FakeDirective(), tmp_path)
    assert "Real body text." in text
    assert "Result:" not in text
    assert "Query:" not in text
    assert src == f"enrichment/exa/{slug}.md"


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

    # Finding 5: the two routed-away articles (fp, skip) are candidates that
    # never reach dedup or the writer -- they must be recorded, not silently
    # dropped, so IN - ROUTE - DEDUP still accounts for every candidate.
    sentinel = json.loads((work_dir / "collection_done.json").read_text())
    assert sentinel["candidates"]["semafor"] == 3
    assert sentinel["routed_away"]["semafor"] == 2
    assert sentinel["deduped"]["semafor"] == 0


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

    # The status also survives in the sentinel, which is archived; the work dir
    # holding the file above is reaped from /tmp after 10 days.
    sentinel = json.loads((work_dir / "collection_done.json").read_text())
    assert sentinel["exa_outcomes"] == {_slugify(headline): "empty"}


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

    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        headline = exa_headline
        source = "levine"

    text, _path = find_rundown_article_source(FakeDirective(), work_dir)
    assert "Exa recovered body text." in text

    # Prove this isn't a tautology: remove the Exa file the collector wrote
    # and confirm the reader now finds nothing -- i.e. the fallback really was
    # reached rather than short-circuiting on an earlier lookup.
    exa_file = work_dir / "enrichment" / "exa" / f"{_slugify(exa_headline)}.md"
    assert exa_file.exists()
    exa_file.unlink()
    text_after_removal, _path_after_removal = find_rundown_article_source(
        FakeDirective(), work_dir
    )
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


def _run_collector_basic(tmp_path, monkeypatch):
    """Run the collector with a single Levine article carrying tier metadata.

    Mirrors the monkeypatch style already used in this file for
    fetch_all_articles / resolve_redirect_url / generate_rundown_research_plan
    / sync_zvi_cache. Returns work_dir.
    """
    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles",
        lambda *a, **kw: [
            Article(
                headline="Test Article",
                url="http://resolved.com",
                content="Full text here",
                source_tier="live",
                extracted_chars=250,
            )
        ],
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
                    headline="Test Article",
                    source="levine",
                    priority=1,
                    theme="Tech",
                    needs_exa=False,
                    exa_query="",
                    is_foreign_policy=False,
                    fp_query="",
                    include_in_episode=True,
                )
            ],
        ),
    )

    today = datetime.now(tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    (levine_cache / f"{today}.json").write_text(
        json.dumps([{"raw_url": "http://raw.com", "headline_context": "context"}])
    )
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    zvi_cache = tmp_path / "zvi-cache"
    zvi_cache.mkdir()
    work_dir = tmp_path / "work"

    collect_all_artifacts(
        "job-basic-test",
        work_dir,
        levine_cache_dir=levine_cache,
        semafor_cache_dir=semafor_cache,
        zvi_cache_dir=zvi_cache,
    )
    return work_dir


def test_tiers_sidecar_records_tier_and_chars(tmp_path, monkeypatch):
    work_dir = _run_collector_basic(tmp_path, monkeypatch)
    tiers = json.loads((work_dir / "tiers.json").read_text(encoding="utf-8"))
    entry = next(iter(tiers.values()))
    assert entry["tier"] in {"live", "paywalled", "http_error", "fetch_error"}
    assert isinstance(entry["extracted_chars"], int)
    assert entry["url"]


def test_tiers_sidecar_is_keyed_by_relative_article_path(tmp_path, monkeypatch):
    work_dir = _run_collector_basic(tmp_path, monkeypatch)
    tiers = json.loads((work_dir / "tiers.json").read_text(encoding="utf-8"))
    for rel_path in tiers:
        assert (work_dir / rel_path).exists()


def test_sentinel_records_per_source_counts(tmp_path, monkeypatch):
    work_dir = _run_collector_basic(tmp_path, monkeypatch)
    sentinel = json.loads((work_dir / "collection_done.json").read_text())
    assert "started_at" in sentinel
    for key in ("levine", "semafor", "zvi"):
        assert key in sentinel["candidates"]
        assert key in sentinel["deduped"]


def test_article_files_have_no_tier_header(tmp_path, monkeypatch):
    """Tier metadata must not leak into the writer prompt."""
    work_dir = _run_collector_basic(tmp_path, monkeypatch)
    for f in (work_dir / "articles").glob("*.md"):
        assert "Source-Tier" not in f.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 3 gate: fire Exa on the measured fetch tier, not the editor's guess.
# ---------------------------------------------------------------------------


def _run_collector_for_exa_gate(
    tmp_path,
    monkeypatch,
    *,
    articles,
    directive,
    exa_results=None,
    exa_status="hit",
):
    """Run the collector with one directive against a controlled article set.

    Captures every search_related_status call (args, kwargs) for inspection.
    Mirrors the monkeypatch style already used in this file for
    fetch_all_articles / resolve_redirect_url / generate_rundown_research_plan
    / sync_zvi_cache. Returns (work_dir, calls).
    """
    calls: list[tuple[tuple, dict]] = []

    def _fake_search(*args, **kwargs):
        calls.append((args, kwargs))
        return (exa_results or [], exa_status)

    monkeypatch.setattr(
        "pipeline.things_happen_collector.fetch_all_articles",
        lambda *a, **kw: articles,
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.resolve_redirect_url", lambda u: u
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: []
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_rundown_research_plan",
        lambda *a, **kw: RundownResearchPlan(themes=["Tech"], directives=[directive]),
    )
    monkeypatch.setattr(
        "pipeline.things_happen_collector.search_related_status", _fake_search
    )

    _et = ZoneInfo("America/New_York")
    today = datetime.now(tz=_et).strftime("%Y-%m-%d")
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    (levine_cache / f"{today}.json").write_text(
        json.dumps([{"raw_url": a.url, "headline": a.headline} for a in articles])
    )
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    zvi_cache = tmp_path / "zvi-cache"
    zvi_cache.mkdir()
    work_dir = tmp_path / "work"

    collect_all_artifacts(
        "job-exa-gate",
        work_dir,
        levine_cache_dir=levine_cache,
        semafor_cache_dir=semafor_cache,
        zvi_cache_dir=zvi_cache,
    )
    return work_dir, calls


def test_exa_fires_for_stubbed_article_without_editor_flag(tmp_path, monkeypatch):
    """The editor flags 4% of directives; the tier signal must drive this."""
    article = Article(
        headline="Widget Maker Struggles",
        url="https://paywalled.example/a",
        content="stub",
        source_tier="paywalled",
    )
    directive = RundownStoryDirective(
        headline="Widget Maker Struggles",
        source="levine",
        priority=1,
        theme="Tech",
        needs_exa=False,
        exa_query="",
        is_foreign_policy=False,
        fp_query="",
        include_in_episode=True,
    )
    _work_dir, calls = _run_collector_for_exa_gate(
        tmp_path,
        monkeypatch,
        articles=[article],
        directive=directive,
        exa_results=[ExaResult(title="X", url="https://open.example/x", text="body")],
        exa_status="hit",
    )
    assert len(calls) == 1
    args, _kwargs = calls[0]
    assert args[0] == "Widget Maker Struggles"


def test_exa_skips_live_article(tmp_path, monkeypatch):
    """A successfully fetched article does not need substitution."""
    article = Article(
        headline="Fine Article",
        url="https://open.example/a",
        content="full text",
        source_tier="live",
    )
    directive = RundownStoryDirective(
        headline="Fine Article",
        source="levine",
        priority=1,
        theme="Tech",
        needs_exa=False,
        exa_query="",
        is_foreign_policy=False,
        fp_query="",
        include_in_episode=True,
    )
    _work_dir, calls = _run_collector_for_exa_gate(
        tmp_path, monkeypatch, articles=[article], directive=directive
    )
    assert calls == []


def test_exa_fires_for_unmatched_directive_with_needs_exa(tmp_path, monkeypatch):
    """Semafor/Zvi directives have no matching Levine article; the editor
    flag must still be honored, or this silently regresses today's
    behavior."""
    directive = RundownStoryDirective(
        headline="Semafor Story",
        source="semafor",
        priority=1,
        theme="Markets",
        needs_exa=True,
        exa_query="",
        is_foreign_policy=False,
        fp_query="",
        include_in_episode=True,
    )
    _work_dir, calls = _run_collector_for_exa_gate(
        tmp_path,
        monkeypatch,
        articles=[],
        directive=directive,
        exa_status="empty",
    )
    assert len(calls) == 1
    from pipeline.things_happen_collector import BYPASS_DOMAINS

    args, kwargs = calls[0]
    assert args[0] == "Semafor Story"
    # No matched article means no origin domain to exclude.
    assert kwargs["exclude_domains"] == list(BYPASS_DOMAINS)


def test_exa_excludes_origin_domain_and_bypass_mirrors(tmp_path, monkeypatch):
    """8/12 spike searches ranked the paywalled origin first."""
    article = Article(
        headline="Paywalled Piece",
        url="https://www.bloomberg.com/news/x",
        content="stub",
        source_tier="paywalled",
    )
    directive = RundownStoryDirective(
        headline="Paywalled Piece",
        source="levine",
        priority=1,
        theme="Tech",
        needs_exa=False,
        exa_query="",
        is_foreign_policy=False,
        fp_query="",
        include_in_episode=True,
    )
    _work_dir, calls = _run_collector_for_exa_gate(
        tmp_path, monkeypatch, articles=[article], directive=directive
    )
    assert len(calls) == 1
    _args, kwargs = calls[0]
    exclude = kwargs["exclude_domains"]
    assert "bloomberg.com" in exclude
    assert "archive.ph" in exclude


def test_exa_filters_bypass_domain_result_locally(tmp_path, monkeypatch):
    """exclude_domains is a request parameter honored by a third-party API;
    ethics policy must not depend on Exa's compliance."""
    article = Article(
        headline="Paywalled Piece",
        url="https://www.bloomberg.com/news/x",
        content="stub",
        source_tier="paywalled",
    )
    directive = RundownStoryDirective(
        headline="Paywalled Piece",
        source="levine",
        priority=1,
        theme="Tech",
        needs_exa=False,
        exa_query="",
        is_foreign_policy=False,
        fp_query="",
        include_in_episode=True,
    )
    exa_results = [
        ExaResult(title="Mirror", url="https://archive.ph/xyz", text="mirrored body"),
        ExaResult(title="Legit", url="https://reuters.com/story", text="real body"),
    ]
    work_dir, _calls = _run_collector_for_exa_gate(
        tmp_path,
        monkeypatch,
        articles=[article],
        directive=directive,
        exa_results=exa_results,
        exa_status="hit",
    )
    slug = _slugify(directive.headline)
    text = (work_dir / "enrichment" / "exa" / f"{slug}.md").read_text(encoding="utf-8")
    assert "archive.ph" not in text
    assert "Legit" in text
    assert "real body" in text
    assert "Result: hit" in text


def test_exa_reports_filtered_when_local_filter_empties_a_hit(tmp_path, monkeypatch):
    """A hit whose only result is rejected locally reports "filtered".

    Deliberately NOT "empty": Exa did find coverage and the deny-list rejected
    all of it. Collapsing the two together would hide a compliance failure --
    if Exa ever stopped honouring exclude_domains, the funnel would show an
    unexplained empty-rate spike with no recorded cause.
    """
    article = Article(
        headline="Paywalled Piece",
        url="https://www.bloomberg.com/news/x",
        content="stub",
        source_tier="paywalled",
    )
    directive = RundownStoryDirective(
        headline="Paywalled Piece",
        source="levine",
        priority=1,
        theme="Tech",
        needs_exa=False,
        exa_query="",
        is_foreign_policy=False,
        fp_query="",
        include_in_episode=True,
    )
    exa_results = [
        ExaResult(title="Mirror", url="https://archive.ph/xyz", text="mirrored")
    ]
    work_dir, _calls = _run_collector_for_exa_gate(
        tmp_path,
        monkeypatch,
        articles=[article],
        directive=directive,
        exa_results=exa_results,
        exa_status="hit",
    )
    slug = _slugify(directive.headline)
    text = (work_dir / "enrichment" / "exa" / f"{slug}.md").read_text(encoding="utf-8")
    assert "Result: filtered" in text
    assert "archive.ph" not in text
    sentinel = json.loads((work_dir / "collection_done.json").read_text())
    assert sentinel["exa_outcomes"][slug] == "filtered"


def test_double_space_headline_slug_match_regression(tmp_path, monkeypatch):
    """Levine headlines come from sentence extraction and can carry a double
    space that Gemini normalizes away when it echoes the headline back.
    Verified: exact-equality match loses 3 of 38 real selected directives to
    this. Matching must be by slug, not raw headline equality."""
    raw_headline = "US Set to  Pay Most Tariffs"  # double space, real shape
    echoed_headline = "US Set to Pay Most Tariffs"  # Gemini-normalized
    article = Article(
        headline=raw_headline,
        url="https://paywalled.example/tariffs",
        content="stub",
        source_tier="paywalled",
    )
    directive = RundownStoryDirective(
        headline=echoed_headline,
        source="levine",
        priority=1,
        theme="Tech",
        needs_exa=False,
        exa_query="",
        is_foreign_policy=False,
        fp_query="",
        include_in_episode=True,
    )
    _work_dir, calls = _run_collector_for_exa_gate(
        tmp_path, monkeypatch, articles=[article], directive=directive
    )
    assert len(calls) == 1
    args, kwargs = calls[0]
    # The query is directive.exa_query or directive.headline -- the echoed
    # (Gemini) headline, not the raw one -- but firing at all is the point.
    assert args[0] == echoed_headline
    assert "paywalled.example" in kwargs["exclude_domains"]


def test_exa_prefers_editor_query_when_present(tmp_path, monkeypatch):
    article = Article(
        headline="Paywalled Piece",
        url="https://paywalled.example/a",
        content="stub",
        source_tier="paywalled",
    )
    directive = RundownStoryDirective(
        headline="Paywalled Piece",
        source="levine",
        priority=1,
        theme="Tech",
        needs_exa=False,
        exa_query="widget factory keywords",
        is_foreign_policy=False,
        fp_query="",
        include_in_episode=True,
    )
    _work_dir, calls = _run_collector_for_exa_gate(
        tmp_path, monkeypatch, articles=[article], directive=directive
    )
    assert len(calls) == 1
    args, _kwargs = calls[0]
    assert args[0] == "widget factory keywords"


def test_exa_skipped_when_not_included_in_episode(tmp_path, monkeypatch):
    """Directives never written to the episode are never worth an Exa call."""
    article = Article(
        headline="Paywalled Piece",
        url="https://paywalled.example/a",
        content="stub",
        source_tier="paywalled",
    )
    directive = RundownStoryDirective(
        headline="Paywalled Piece",
        source="levine",
        priority=1,
        theme="Tech",
        needs_exa=True,
        exa_query="",
        is_foreign_policy=False,
        fp_query="",
        include_in_episode=False,
    )
    _work_dir, calls = _run_collector_for_exa_gate(
        tmp_path, monkeypatch, articles=[article], directive=directive
    )
    assert calls == []


# ---------------------------------------------------------------------------
# _host_banned: local defence-in-depth filter, independent of exclude_domains.
# ---------------------------------------------------------------------------


def test_host_banned_exact_bypass_domain():
    from pipeline.things_happen_collector import _host_banned

    assert _host_banned("https://archive.ph/xyz", "") is True


def test_host_banned_subdomain_of_bypass_domain():
    from pipeline.things_happen_collector import _host_banned

    assert _host_banned("https://news.archive.ph/xyz", "") is True


def test_host_banned_origin_domain():
    from pipeline.things_happen_collector import _host_banned

    assert _host_banned("https://bloomberg.com/story", "bloomberg.com") is True


def test_host_banned_subdomain_of_origin():
    from pipeline.things_happen_collector import _host_banned

    assert _host_banned("https://amp.bloomberg.com/story", "bloomberg.com") is True


def test_host_banned_empty_or_garbage_url():
    from pipeline.things_happen_collector import _host_banned

    assert _host_banned("", "bloomberg.com") is True
    assert _host_banned("not a url", "bloomberg.com") is True


def test_host_banned_legitimate_host_passes():
    from pipeline.things_happen_collector import _host_banned

    assert _host_banned("https://reuters.com/story", "bloomberg.com") is False
