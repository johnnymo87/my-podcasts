from __future__ import annotations

from pathlib import Path

from pipeline.article_fetcher import fetch_all_articles
from pipeline.exa_client import search_related
from pipeline.rss_sources import search_rss_sources
from pipeline.things_happen_editor import generate_research_plan
from pipeline.things_happen_extractor import resolve_redirect_url
from pipeline.xai_client import search_twitter


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
) -> None:
    """
    Phase 1: Fetch articles and setup directories.
    Phase 2: Generate research plan via Editor AI.
    Phase 3: Execute deep enrichment searches.
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

    # Phase 1: Base Collection
    for link in links_raw:
        link["resolved_url"] = resolve_redirect_url(link["raw_url"])

    articles = fetch_all_articles(links_raw, delay_between=1.0)

    if not articles:
        return

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

    # Phase 3: Deep Enrichment
    for i, directive in enumerate(directives):
        slug = f"{i:02d}-{_slugify(directive.headline)}"

        # Exa search
        if directive.needs_exa and directive.exa_query:
            exa_results = search_related(directive.exa_query)
            if exa_results:
                out = f"# Exa Results for: {directive.headline}\nQuery: {directive.exa_query}\n\n"
                for exa_r in exa_results:
                    out += f"## [{exa_r.title}]({exa_r.url})\n{exa_r.text}\n\n"
                (exa_dir / f"{slug}.md").write_text(out, encoding="utf-8")

        # xAI search
        if directive.needs_xai and directive.xai_query:
            xai_result = search_twitter(directive.xai_query)
            if xai_result:
                out = f"# Twitter Summary for: {directive.headline}\nQuery: {directive.xai_query}\n\n{xai_result.summary}"
                (xai_dir / f"{slug}.md").write_text(out, encoding="utf-8")

        # RSS search
        if directive.is_foreign_policy and directive.fp_query:
            rss_results = search_rss_sources(directive.fp_query)
            if rss_results:
                out = f"# RSS Alternative Perspectives for: {directive.headline}\nQuery: {directive.fp_query}\n\n"
                for rss_r in rss_results:
                    pub = (
                        rss_r.published.strftime("%Y-%m-%d")
                        if rss_r.published
                        else "Unknown"
                    )
                    out += f"## [{rss_r.source}] {rss_r.title}\nPublished: {pub}\nURL: {rss_r.url}\n\n{rss_r.text}\n\n"
                (rss_dir / f"{slug}.md").write_text(out, encoding="utf-8")
