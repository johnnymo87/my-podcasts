from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.article_fetcher import fetch_all_articles
from pipeline.exa_client import search_related
from pipeline.rss_sources import categorize_semafor_article
from pipeline.things_happen_editor import generate_research_plan
from pipeline.things_happen_extractor import resolve_redirect_url
from pipeline.xai_client import search_twitter
from pipeline.zvi_cache import search_zvi_cache, sync_zvi_cache


def _slugify(text: str) -> str:
    """Create a safe filename slug from a headline."""
    safe = "".join(c if c.isalnum() else "-" for c in text.lower())
    # condense multiple dashes
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:50]


def collect_all_artifacts(
    job_id: str,
    links_raw: list[dict],
    work_dir: Path,
    scripts_source_dir: Path | None = None,
    fp_routed_dir: Path | None = None,
    zvi_cache_dir: Path | None = None,
    semafor_cache_dir: Path | None = None,
    lookback_days: int = 2,
) -> None:
    """
    Phase 1: Fetch articles and setup directories.
    Phase 2: Generate research plan via Editor AI.
    Phase 2b: Route FP-flagged directives to fp_routed_dir (if configured).
    Phase 3: Execute deep enrichment on non-FP directives only.
    """
    # Create directory structure
    articles_dir = work_dir / "articles"
    enrichment_dir = work_dir / "enrichment"
    exa_dir = enrichment_dir / "exa"
    xai_dir = enrichment_dir / "xai"
    rss_dir = enrichment_dir / "rss"
    context_dir = work_dir / "context"

    for d in (articles_dir, exa_dir, xai_dir, rss_dir, context_dir):
        d.mkdir(parents=True, exist_ok=True)

    _et = ZoneInfo("America/New_York")
    lookback_dates = set()
    for i in range(lookback_days):
        d = (datetime.now(tz=_et) - timedelta(days=i)).strftime("%Y-%m-%d")
        lookback_dates.add(d)

    # Phase 1: Base Collection
    for link in links_raw:
        link["resolved_url"] = resolve_redirect_url(link["raw_url"])

    articles = fetch_all_articles(links_raw, delay_between=1.0)

    headlines_with_snippets = []

    for i, art in enumerate(articles):
        slug = f"{i:02d}-{_slugify(art.headline)}"
        art_path = articles_dir / f"{slug}.md"

        # Write article to disk
        content = f"# {art.headline}\n\nURL: {art.url}\n\n{art.content}"
        art_path.write_text(content, encoding="utf-8")

        # Build snippet for the Editor AI
        truncated = art.content[:300]
        suffix = "..." if len(art.content) > 300 else ""
        snippet = f"Headline: {art.headline}\nContext: {truncated}{suffix}"
        headlines_with_snippets.append(snippet)

    # Phase 1b: Semafor articles from cache (TH categories)
    semafor_dir = articles_dir / "semafor"
    semafor_dir.mkdir(parents=True, exist_ok=True)
    _semafor_cache = semafor_cache_dir or Path("/persist/my-podcasts/semafor-cache")
    if not _semafor_cache.exists():
        print(f"[collector] WARNING: Semafor cache not found at {_semafor_cache}")
    if _semafor_cache.exists():
        for cached in sorted(_semafor_cache.glob("*.md")):
            if not any(cached.name.startswith(d) for d in lookback_dates):
                continue
            text = cached.read_text(encoding="utf-8")
            lines = text.split("\n")
            headline = lines[0].lstrip("# ").strip()
            category = ""
            for line in lines[1:8]:
                if line.startswith("Category: "):
                    category = line[10:].strip()
            routing = categorize_semafor_article(category)
            if routing not in ("th", "both"):
                continue
            slug = _slugify(headline)
            art_path = semafor_dir / f"{slug}.md"
            if not art_path.exists():
                art_path.write_text(text, encoding="utf-8")

    # Phase 1c: Zvi articles (day-of posts from persistent cache)
    zvi_cache = (
        zvi_cache_dir
        if zvi_cache_dir is not None
        else Path("/persist/my-podcasts/zvi-cache")
    )
    sync_zvi_cache(zvi_cache)
    zvi_dir = articles_dir / "zvi"
    zvi_dir.mkdir(parents=True, exist_ok=True)
    for cached_file in zvi_cache.glob("*.md"):
        if any(cached_file.name.startswith(d) for d in lookback_dates):
            target = zvi_dir / cached_file.name
            if not target.exists():
                target.write_text(
                    cached_file.read_text(encoding="utf-8"), encoding="utf-8"
                )

    # Zvi headlines
    for zvi_path in zvi_dir.glob("*.md"):
        text = zvi_path.read_text(encoding="utf-8")
        parts = text.split("\n\n", 2)
        headline = parts[0].lstrip("# ").strip() if parts else ""
        body = parts[2].strip() if len(parts) > 2 else ""
        truncated = body[:300]
        suffix = "..." if len(body) > 300 else ""
        snippet = f"[zvi] {headline}\nContext: {truncated}{suffix}"
        headlines_with_snippets.append(snippet)

    # Copy trailing window context (last 3 scripts)
    scripts_dir = (
        scripts_source_dir
        if scripts_source_dir is not None
        else Path("/persist/my-podcasts/scripts/things-happen")
    )
    if scripts_dir.exists():
        scripts = sorted(scripts_dir.glob("*.txt"), reverse=True)[:3]
        for script in scripts:
            target = context_dir / script.name
            if not target.exists():
                # Read and write instead of symlink to avoid cross-device link issues
                target.write_text(script.read_text(encoding="utf-8"))

    # Phase 2: Editor AI
    directives = generate_research_plan(headlines_with_snippets)

    # Partition directives: FP links go to staging, non-FP get enriched
    fp_directives = [d for d in directives if d.is_foreign_policy]
    non_fp_directives = [d for d in directives if not d.is_foreign_policy]

    # Write FP directives to the staging directory
    if fp_directives and fp_routed_dir is None:
        print(
            f"[collector] {len(fp_directives)} FP directives not routed (fp_routed_dir not configured)"
        )
    if fp_directives and fp_routed_dir is not None:
        fp_routed_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(tz=_et).strftime("%Y-%m-%d")
        routed_path = fp_routed_dir / f"{today}-{job_id}.json"
        routed_data = []
        for d in fp_directives:
            # Find the matching article for context
            art = next((a for a in articles if a.headline == d.headline), None)
            routed_data.append(
                {
                    "headline": d.headline,
                    "url": art.url if art else "",
                    "snippet": (art.content[:500] if art else ""),
                }
            )
        routed_path.write_text(json.dumps(routed_data, indent=2), encoding="utf-8")

    # Phase 3: Deep Enrichment (non-FP only)
    for i, directive in enumerate(non_fp_directives):
        slug = f"{i:02d}-{_slugify(directive.headline)}"

        # Exa search
        if directive.needs_exa and directive.exa_query:
            try:
                exa_results = search_related(directive.exa_query)
                if exa_results:
                    out = f"# Exa Results for: {directive.headline}\nQuery: {directive.exa_query}\n\n"
                    for exa_r in exa_results:
                        out += f"## [{exa_r.title}]({exa_r.url})\n{exa_r.text}\n\n"
                    (exa_dir / f"{slug}.md").write_text(out, encoding="utf-8")
            except Exception as e:
                print(f"[collector] Exa search failed for '{directive.exa_query}': {e}")

        # xAI search
        if directive.needs_xai and directive.xai_query:
            try:
                xai_result = search_twitter(directive.xai_query)
                if xai_result:
                    out = f"# Twitter Summary for: {directive.headline}\nQuery: {directive.xai_query}\n\n{xai_result.summary}"
                    (xai_dir / f"{slug}.md").write_text(out, encoding="utf-8")
            except Exception as e:
                print(f"[collector] xAI search failed for '{directive.xai_query}': {e}")

        # Zvi cache search (AI)
        if directive.is_ai and directive.ai_query:
            try:
                zvi_results = search_zvi_cache(directive.ai_query, zvi_cache)
                if zvi_results:
                    out = f"# Zvi Perspectives for: {directive.headline}\nQuery: {directive.ai_query}\n\n"
                    for r in zvi_results:
                        out += f"## [{r['headline']}]\n{r['snippet']}\n\n"
                    (rss_dir / f"{slug}-ai.md").write_text(out, encoding="utf-8")
            except Exception as e:
                print(
                    f"[collector] Zvi cache search failed for '{directive.ai_query}': {e}"
                )
