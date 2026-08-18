from __future__ import annotations

import html as html_mod
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import trafilatura

from pipeline.article_resolver import slugify as _slugify
from pipeline.exa_client import search_related
from pipeline.fp_editor import generate_fp_research_plan
from pipeline.freshness import (
    annotate_headlines,
    classify_headlines,
    format_coverage_ledger,
)
from pipeline.rss_sources import categorize_semafor_article


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


# Bodies shorter than this are RSS teasers worth re-fetching in full.
#
# Measured 2026-08-18 over all 1780 files in the antiwar RSS cache: the three
# antiwar feeds top out at 454 chars (n=1633, median ~350) while the full-text
# caitlinjohnstone feed bottoms out at 767 (n=147, median 4623). 600 sits in
# that gap. Deliberately a content-only rule rather than a feed allowlist, so
# it retires itself if antiwar ever publishes full text.
#
# Note the gate sees the body *after* HTML entities are decoded, while the
# corpus was measured on raw bodies. Decoding only shortens (&#8217; -> '), so
# every measured body moves away from the threshold, not toward it.
_TEASER_MAX_CHARS = 600

# Worst case for the 14-day adaptive lookback ceiling is ~140 candidates at
# ~10 new cache files/day; at ~1.5s per fetch plus a 1s delay that is ~6
# minutes. Cap the work and take the newest candidates.
_MAX_RSS_FETCHES = 40

# Seconds between outbound article fetches; matches the Levine path's
# fetch_all_articles(..., delay_between=1.0).
_RSS_FETCH_DELAY = 1.0


def _should_fetch_full_text(excerpt: str, url: str) -> bool:
    """True when a cached RSS body looks like a teaser worth re-fetching."""
    if not url.strip():
        return False
    return len(excerpt) < _TEASER_MAX_CHARS


def _prior_fetched_body(art_path: Path, excerpt: str) -> str | None:
    """Return a previous attempt's fetched body for this article, if any.

    The work dir survives retries, so a body already longer than the cache
    excerpt is text a prior attempt successfully fetched. Returns None when
    there is nothing to reuse — including when the prior body is merely the
    excerpt, which means that attempt's fetch failed and should be retried.
    """
    if not art_path.exists():
        return None
    parts = art_path.read_text(encoding="utf-8").split("\n\n", 2)
    body = parts[2].strip() if len(parts) > 2 else ""
    return body if len(body) > len(excerpt) else None


def collect_fp_artifacts(
    job_id: str,
    work_dir: Path,
    scripts_source_dir: Path | None = None,
    fp_routed_dir: Path | None = None,
    homepage_cache_dir: Path | None = None,
    antiwar_rss_cache_dir: Path | None = None,
    semafor_cache_dir: Path | None = None,
    lookback_days: int = 2,
    coverage_summary: list[dict] | None = None,
    prior_urls: set[str] | None = None,
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

    # Seed with URLs from prior episodes to prevent cross-day repetition
    if prior_urls:
        homepage_urls.update(prior_urls)

    if not _homepage_cache.exists():
        print(f"[fp_collector] WARNING: homepage cache not found at {_homepage_cache}")
    if _homepage_cache.exists():
        for cache_path in sorted(_homepage_cache.glob("*.md")):
            if not _in_window(cache_path.name):
                continue
            raw = cache_path.read_text(encoding="utf-8")
            lines = raw.split("\n")

            # Parse metadata — title may span multiple lines in older cache files
            title_parts = []
            url = ""
            region = ""
            title_done = False
            for line in lines:
                stripped = line.strip()
                if not title_done:
                    if line.startswith("# "):
                        title_parts.append(line.lstrip("# ").strip())
                    elif not stripped:
                        if title_parts:
                            title_done = True
                    elif title_parts:
                        # Continuation of multi-line title
                        title_parts.append(stripped)
                else:
                    if stripped.startswith("URL: "):
                        url = stripped[5:].strip()
                    elif stripped.startswith("Region: "):
                        region = stripped[8:].strip()

            title = " ".join(title_parts)

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
    fetch_log: list[dict] = []

    if not _rss_cache.exists():
        print(f"[fp_collector] WARNING: RSS cache not found at {_rss_cache}")
    if _rss_cache.exists():
        pending: list[dict] = []
        for cache_path in sorted(_rss_cache.glob("*.md")):
            if not _in_window(cache_path.name):
                continue
            raw = cache_path.read_text(encoding="utf-8")
            lines = raw.split("\n")

            title = " ".join(lines[0].lstrip("# ").split()) if lines else ""
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
            # Cached antiwar bodies are the raw RSS <summary>, so they carry
            # undecoded HTML entities (&#8217;, the trailing [&#8230;]). Decode
            # here as well as at sync time, because cache files already on disk
            # keep the old encoding for the length of the retention window.
            text = html_mod.unescape(
                body_parts[2].strip() if len(body_parts) > 2 else ""
            )
            pending.append(
                {"headline": title, "url": url, "source": source, "text": text}
            )

        # Upgrade teasers to full text, newest first, bounded.
        candidates = [
            item
            for item in reversed(pending)
            if _should_fetch_full_text(item["text"], item["url"])
        ][:_MAX_RSS_FETCHES]
        fetched_count = 0
        for item in candidates:
            # Collection re-runs from the top on every retry (the sentinel is
            # written last), and the retry budget is 51 attempts — so reuse
            # this work dir's own prior output rather than re-requesting. A
            # body that is still excerpt-length means last attempt's fetch
            # failed, and that one is worth retrying.
            prior = _prior_fetched_body(
                articles_rss_dir / item["source"] / f"{_slugify(item['headline'])}.md",
                item["text"],
            )
            if prior is not None:
                fetch_log.append(
                    {
                        "url": item["url"],
                        "headline": item["headline"],
                        "excerpt_chars": len(item["text"]),
                        "fetched_chars": len(prior),
                        "upgraded": True,
                        "reused": True,
                    }
                )
                item["text"] = prior
                continue

            if fetched_count > 0:
                time.sleep(_RSS_FETCH_DELAY)
            fetched_count += 1
            fetched = _extract_article_text(item["url"])
            # The excerpt is the floor, never the ceiling: an empty
            # extraction, an HTTP error, or a paywall stub all leave it in
            # place.
            upgraded = len(fetched) > len(item["text"])
            fetch_log.append(
                {
                    "url": item["url"],
                    "headline": item["headline"],
                    "excerpt_chars": len(item["text"]),
                    "fetched_chars": len(fetched),
                    "upgraded": upgraded,
                    "reused": False,
                }
            )
            if upgraded:
                item["text"] = fetched

        for item in pending:
            source_dir = articles_rss_dir / item["source"]
            source_dir.mkdir(parents=True, exist_ok=True)
            art_path = source_dir / f"{_slugify(item['headline'])}.md"
            art_path.write_text(
                f"# {item['headline']}\n\nURL: {item['url']}\n"
                f"Source: {item['source']}\n\n{item['text']}",
                encoding="utf-8",
            )
            rss_articles_data.append(item)

    # Written unconditionally — a missing cache dir is exactly the case
    # where "did this feature do anything?" most needs an answer, and it is
    # the case that would otherwise write no file. This is the only
    # visibility this feature has: FP has no funnel report.
    (work_dir / "rss_fetch.json").write_text(
        json.dumps(fetch_log, indent=2), encoding="utf-8"
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

            title = " ".join(lines[0].lstrip("# ").split()) if lines else ""
            url = ""
            category = ""
            routing = ""
            for line in lines[1:]:
                if line.startswith("URL: "):
                    url = line[5:].strip()
                elif line.startswith("Category: "):
                    category = line[10:].strip()
                elif line.startswith("Routing: "):
                    routing = line[9:].strip()

            if not title or not url:
                continue

            # Prefer Routing header; fall back to category-based classification
            if not routing:
                routing = categorize_semafor_article(category)
            if routing not in ("fp", "both"):
                continue

            if url in homepage_urls:
                continue

            body_parts = raw.split("\n\n", 2)
            # Cached Semafor bodies may still carry undecoded HTML entities
            # (source_cache's write-side fix does not retroactively rewrite
            # files already on disk for the length of the retention window),
            # so decode here too, for the same reason as the RSS path above.
            text = html_mod.unescape(
                body_parts[2].strip() if len(body_parts) > 2 else ""
            )

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

    # Write sentinel — collection completed successfully
    sentinel = {
        "job_id": job_id,
        "completed_at": datetime.now(tz=_et).isoformat(),
        "lookback_days": lookback_days,
        "directives": len(plan.directives),
    }
    (work_dir / "collection_done.json").write_text(
        json.dumps(sentinel, indent=2), encoding="utf-8"
    )
