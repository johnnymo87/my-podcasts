from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import trafilatura

from pipeline.exa_client import search_related
from pipeline.fp_editor import generate_fp_research_plan
from pipeline.freshness import (
    annotate_headlines,
    classify_headlines,
    format_coverage_ledger,
)
from pipeline.rss_sources import categorize_semafor_article


def _slugify(text: str) -> str:
    """Create a safe filename slug from a headline."""
    safe = "".join(c if c.isalnum() else "-" for c in text.lower())
    # condense multiple dashes
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:50]


def _extract_article_text(url: str) -> str:
    """Fetch URL and extract main text via trafilatura."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "podcast-pipeline/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        text = trafilatura.extract(
            resp.content,
            url=url,
            include_comments=False,
            include_tables=False,
            include_formatting=False,
            favor_precision=True,
        )
        return (text or "").strip()
    except Exception:
        return ""


def collect_fp_artifacts(
    job_id: str,
    work_dir: Path,
    scripts_source_dir: Path | None = None,
    fp_routed_dir: Path | None = None,
    homepage_cache_dir: Path | None = None,
    antiwar_rss_cache_dir: Path | None = None,
    semafor_cache_dir: Path | None = None,
    lookback_days: int = 2,
    coverage_summary: list[dict] | None = None,  # NEW
) -> None:
    """Orchestrate FP Digest collection.

    1. Creates directory structure.
    2. Reads antiwar.com homepage articles from persistent cache.
    3. Reads RSS articles from persistent cache, deduplicates.
    4. Copies last 3 scripts for context.
    5. Builds headlines list, calls the FP editor for a research plan.
    6. Writes plan.json.
    7. Runs Exa enrichment for selected stories.
    """
    # Create directory structure
    articles_homepage_dir = work_dir / "articles" / "homepage"
    articles_rss_dir = work_dir / "articles" / "rss"
    exa_dir = work_dir / "enrichment" / "exa"
    context_dir = work_dir / "context"

    for d in (articles_homepage_dir, articles_rss_dir, exa_dir, context_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Compute lookback window in Eastern time
    _et = ZoneInfo("America/New_York")
    lookback_dates = set()
    for i in range(lookback_days):
        d = (datetime.now(tz=_et) - timedelta(days=i)).strftime("%Y-%m-%d")
        lookback_dates.add(d)

    def _in_window(filename: str) -> bool:
        return any(filename.startswith(d) for d in lookback_dates)

    # Phase 1: Read homepage articles from cache
    _homepage_cache = (
        homepage_cache_dir
        if homepage_cache_dir is not None
        else Path("/persist/my-podcasts/antiwar-homepage-cache")
    )
    homepage_urls: set[str] = set()

    if not _homepage_cache.exists():
        print(f"[fp_collector] WARNING: homepage cache not found at {_homepage_cache}")
    if _homepage_cache.exists():
        for cache_path in sorted(_homepage_cache.glob("*.md")):
            if not _in_window(cache_path.name):
                continue
            raw = cache_path.read_text(encoding="utf-8")
            lines = raw.split("\n")

            # Parse metadata
            title = lines[0].lstrip("# ").strip() if lines else ""
            url = ""
            region = ""
            for line in lines[1:]:
                if line.startswith("URL: "):
                    url = line[5:].strip()
                elif line.startswith("Region: "):
                    region = line[8:].strip()

            if not title or not url:
                continue

            homepage_urls.add(url)

            region_slug = _slugify(region) if region else "unknown"
            region_dir = articles_homepage_dir / region_slug
            region_dir.mkdir(parents=True, exist_ok=True)

            slug = _slugify(title)
            art_path = region_dir / f"{slug}.md"
            # Extract body (everything after the metadata block)
            # Find the blank line after metadata block (2 blank lines separate header from body)
            body_parts = raw.split("\n\n", 2)
            text = body_parts[2].strip() if len(body_parts) > 2 else ""

            content = f"# {title}\n\nURL: {url}\nRegion: {region}\n\n{text}"
            art_path.write_text(content, encoding="utf-8")

    # Phase 2: Read RSS articles from cache
    _rss_cache = (
        antiwar_rss_cache_dir
        if antiwar_rss_cache_dir is not None
        else Path("/persist/my-podcasts/antiwar-rss-cache")
    )
    rss_articles_data: list[dict] = []

    if not _rss_cache.exists():
        print(f"[fp_collector] WARNING: RSS cache not found at {_rss_cache}")
    if _rss_cache.exists():
        for cache_path in sorted(_rss_cache.glob("*.md")):
            if not _in_window(cache_path.name):
                continue
            raw = cache_path.read_text(encoding="utf-8")
            lines = raw.split("\n")

            title = lines[0].lstrip("# ").strip() if lines else ""
            url = ""
            source = ""
            for line in lines[1:]:
                if line.startswith("URL: "):
                    url = line[5:].strip()
                elif line.startswith("Source: "):
                    source = line[8:].strip()

            if not title or not url:
                continue

            # Skip if URL already in homepage
            if url in homepage_urls:
                continue

            body_parts = raw.split("\n\n", 2)
            text = body_parts[2].strip() if len(body_parts) > 2 else ""

            source_dir = articles_rss_dir / source
            source_dir.mkdir(parents=True, exist_ok=True)

            slug = _slugify(title)
            art_path = source_dir / f"{slug}.md"
            content = f"# {title}\n\nURL: {url}\nSource: {source}\n\n{text}"
            art_path.write_text(content, encoding="utf-8")

            rss_articles_data.append(
                {"headline": title, "url": url, "source": source, "text": text}
            )

    # Phase 2b: Pick up routed links from Things Happen
    articles_routed_dir = work_dir / "articles" / "routed"
    articles_routed_dir.mkdir(parents=True, exist_ok=True)
    routed_links_dir = (
        fp_routed_dir
        if fp_routed_dir is not None
        else Path("/persist/my-podcasts/fp-routed-links")
    )
    if routed_links_dir.exists():
        for routed_path in sorted(routed_links_dir.glob("*.json")):
            fname = routed_path.stem  # e.g. "2026-03-07-some-job-id"
            if _in_window(fname):
                routed_data = json.loads(routed_path.read_text())
                for item in routed_data:
                    headline = item.get("headline", "").strip()
                    if not headline:
                        continue
                    url = item.get("url", "")
                    if url not in homepage_urls:
                        text = (
                            _extract_article_text(url)
                            if url
                            else item.get("snippet", "")
                        )
                        slug = _slugify(headline)
                        art_path = articles_routed_dir / f"{slug}.md"
                        art_path.write_text(
                            f"# {headline}\n\nURL: {url}\nSource: levine-routed\n\n{text}",
                            encoding="utf-8",
                        )

    # Phase 2c: Read Semafor FP articles from cache
    articles_semafor_dir = work_dir / "articles" / "semafor"
    articles_semafor_dir.mkdir(parents=True, exist_ok=True)
    _semafor_cache = (
        semafor_cache_dir
        if semafor_cache_dir is not None
        else Path("/persist/my-podcasts/semafor-cache")
    )

    if not _semafor_cache.exists():
        print(f"[fp_collector] WARNING: Semafor cache not found at {_semafor_cache}")
    if _semafor_cache.exists():
        for cache_path in sorted(_semafor_cache.glob("*.md")):
            if not _in_window(cache_path.name):
                continue
            raw = cache_path.read_text(encoding="utf-8")
            lines = raw.split("\n")

            title = lines[0].lstrip("# ").strip() if lines else ""
            url = ""
            category = ""
            for line in lines[1:]:
                if line.startswith("URL: "):
                    url = line[5:].strip()
                elif line.startswith("Category: "):
                    category = line[10:].strip()

            if not title or not url:
                continue

            routing = categorize_semafor_article(category)
            if routing not in ("fp", "both"):
                continue

            if url in homepage_urls:
                continue

            body_parts = raw.split("\n\n", 2)
            text = body_parts[2].strip() if len(body_parts) > 2 else ""

            slug = _slugify(title)
            art_path = articles_semafor_dir / f"{slug}.md"
            art_path.write_text(
                f"# {title}\n\nURL: {url}\nSource: semafor\nCategory: {category}\n\n{text}",
                encoding="utf-8",
            )

    # Phase 3: Copy last 3 scripts for context
    scripts_dir = (
        scripts_source_dir
        if scripts_source_dir is not None
        else Path("/persist/my-podcasts/scripts/fp-digest")
    )
    context_scripts: list[str] = []
    if scripts_dir.exists():
        scripts = sorted(scripts_dir.glob("*.txt"), reverse=True)[:3]
        for script in scripts:
            target = context_dir / script.name
            if not target.exists():
                target.write_text(script.read_text(encoding="utf-8"))
            context_scripts.append(script.read_text(encoding="utf-8"))

    # Phase 4: Build headlines for the editor
    headlines_with_snippets: list[str] = []

    # Homepage headlines — read from written article files
    for region_dir in articles_homepage_dir.iterdir():
        if not region_dir.is_dir():
            continue
        region_slug = region_dir.name
        source_label = f"homepage/{region_slug}"
        for art_path in sorted(region_dir.glob("*.md")):
            raw = art_path.read_text(encoding="utf-8")
            lines = raw.split("\n")
            headline = lines[0].lstrip("# ").strip() if lines else ""
            body_parts = raw.split("\n\n", 2)
            text = body_parts[2].strip() if len(body_parts) > 2 else ""
            truncated = text[:300]
            suffix = "..." if len(text) > 300 else ""
            snippet = f"[{source_label}] {headline}\nContext: {truncated}{suffix}"
            headlines_with_snippets.append(snippet)

    # RSS headlines — iterate over rss_articles_data
    for article in rss_articles_data:
        source_label = f"rss/{article['source']}"
        text = article["text"]
        truncated = text[:300]
        suffix = "..." if len(text) > 300 else ""
        snippet = (
            f"[{source_label}] {article['headline']}\nContext: {truncated}{suffix}"
        )
        headlines_with_snippets.append(snippet)

    # Routed headlines
    for routed_path in articles_routed_dir.glob("*.md"):
        text = routed_path.read_text(encoding="utf-8")
        parts = text.split("\n\n", 2)
        headline = parts[0].lstrip("# ").strip() if parts else ""
        body = parts[2].strip() if len(parts) > 2 else ""
        truncated = body[:300]
        suffix = "..." if len(body) > 300 else ""
        snippet = f"[routed/levine] {headline}\nContext: {truncated}{suffix}"
        headlines_with_snippets.append(snippet)

    # Semafor FP headlines
    for semafor_path in articles_semafor_dir.glob("*.md"):
        text = semafor_path.read_text(encoding="utf-8")
        parts = text.split("\n\n", 2)
        headline = parts[0].lstrip("# ").strip() if parts else ""
        body = parts[2].strip() if len(parts) > 2 else ""
        truncated = body[:300]
        suffix = "..." if len(body) > 300 else ""
        snippet = f"[semafor] {headline}\nContext: {truncated}{suffix}"
        headlines_with_snippets.append(snippet)

    # Phase 4.5: Freshness annotation
    coverage_ledger: str | None = None
    if coverage_summary:
        classifications = classify_headlines(headlines_with_snippets, coverage_summary)
        headlines_with_snippets = annotate_headlines(
            headlines_with_snippets, classifications, coverage_summary
        )
        coverage_ledger = format_coverage_ledger(coverage_summary)
    elif context_scripts:
        # Fallback: extract themes from scripts when articles_json unavailable
        from pipeline.freshness import extract_themes_from_scripts

        fallback_coverage = extract_themes_from_scripts(context_scripts)
        if fallback_coverage:
            classifications = classify_headlines(
                headlines_with_snippets, fallback_coverage
            )
            headlines_with_snippets = annotate_headlines(
                headlines_with_snippets, classifications, fallback_coverage
            )
            coverage_ledger = format_coverage_ledger(fallback_coverage)

    # Phase 5: Generate research plan
    plan = generate_fp_research_plan(
        headlines_with_snippets,
        context_scripts=context_scripts if not coverage_ledger else None,
        coverage_ledger=coverage_ledger,
    )

    # Write plan.json
    plan_path = work_dir / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    # Phase 6: Exa enrichment for selected stories
    for directive in plan.directives:
        if directive.needs_exa and directive.include_in_episode and directive.exa_query:
            slug = _slugify(directive.headline)
            try:
                exa_results = search_related(directive.exa_query)
                if exa_results:
                    out = (
                        f"# Exa Results for: {directive.headline}\n"
                        f"Query: {directive.exa_query}\n\n"
                    )
                    for exa_r in exa_results:
                        out += f"## [{exa_r.title}]({exa_r.url})\n{exa_r.text}\n\n"
                    (exa_dir / f"{slug}.md").write_text(out, encoding="utf-8")
            except Exception as e:
                print(
                    f"[fp_collector] Exa search failed for '{directive.exa_query}': {e}"
                )
