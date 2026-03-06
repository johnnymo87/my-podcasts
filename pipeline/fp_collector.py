from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import feedparser
import requests
import trafilatura

from pipeline.exa_client import search_related
from pipeline.fp_editor import generate_fp_research_plan
from pipeline.fp_homepage_scraper import scrape_homepage
from pipeline.rss_sources import FP_DIGEST_RSS_SOURCES


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


def _fetch_rss_articles(days_back: int = 3) -> list[dict]:
    """Fetch articles from all FP_DIGEST_RSS_SOURCES.

    Returns a list of dicts with keys: source, headline, url, summary, text.
    """
    since = datetime.now(tz=UTC) - timedelta(days=days_back)
    articles: list[dict] = []

    session = requests.Session()
    session.headers.update({"User-Agent": "podcast-pipeline/1.0"})

    for src in FP_DIGEST_RSS_SOURCES:
        try:
            resp = session.get(src.feed_url, timeout=15)
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)
        except Exception as e:
            print(f"[fp_collector] Failed to fetch feed {src.feed_url}: {e}")
            continue

        for entry in feed.entries:
            # Parse publication date
            published = None
            for key in ("published_parsed", "updated_parsed"):
                st = entry.get(key)
                if st:
                    published = datetime.fromtimestamp(time.mktime(st), tz=UTC)
                    break

            if published and published < since:
                continue

            headline = (entry.get("title") or "").strip()
            url = (entry.get("link") or "").strip()
            summary = (entry.get("summary") or entry.get("description") or "").strip()

            if not headline or not url:
                continue

            articles.append(
                {
                    "source": src.name,
                    "headline": headline,
                    "url": url,
                    "summary": summary,
                    "text": "",
                }
            )

    return articles


def collect_fp_artifacts(
    job_id: str,
    work_dir: Path,
    scripts_source_dir: Path | None = None,
) -> None:
    """Orchestrate FP Digest collection.

    1. Creates directory structure.
    2. Scrapes antiwar.com homepage, fetches full text, writes articles.
    3. Fetches RSS articles, deduplicates, writes articles.
    4. Copies last 3 scripts for context.
    5. Builds headlines list, calls the FP editor for a research plan.
    6. Writes plan.json.
    7. Runs Exa enrichment for selected stories.
    8. Fetches full text for RSS articles with short content.
    """
    # Create directory structure
    articles_homepage_dir = work_dir / "articles" / "homepage"
    articles_rss_dir = work_dir / "articles" / "rss"
    exa_dir = work_dir / "enrichment" / "exa"
    context_dir = work_dir / "context"

    for d in (articles_homepage_dir, articles_rss_dir, exa_dir, context_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Phase 1: Homepage scraping
    homepage_links = scrape_homepage()
    homepage_urls: set[str] = set()

    for link in homepage_links:
        homepage_urls.add(link.url)
        text = _extract_article_text(link.url)

        region_slug = _slugify(link.region)
        region_dir = articles_homepage_dir / region_slug
        region_dir.mkdir(parents=True, exist_ok=True)

        slug = _slugify(link.headline)
        art_path = region_dir / f"{slug}.md"
        content = (
            f"# {link.headline}\n\nURL: {link.url}\nRegion: {link.region}\n\n{text}"
        )
        art_path.write_text(content, encoding="utf-8")

    # Phase 2: RSS article fetching
    rss_articles = _fetch_rss_articles()

    # Deduplicate RSS articles by URL against homepage links
    rss_articles = [a for a in rss_articles if a["url"] not in homepage_urls]

    for article in rss_articles:
        source_dir = articles_rss_dir / article["source"]
        source_dir.mkdir(parents=True, exist_ok=True)

        slug = _slugify(article["headline"])
        art_path = source_dir / f"{slug}.md"
        content = (
            f"# {article['headline']}\n\n"
            f"URL: {article['url']}\n"
            f"Source: {article['source']}\n\n"
            f"{article['text'] or article['summary']}"
        )
        art_path.write_text(content, encoding="utf-8")

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

    # Homepage headlines
    for link in homepage_links:
        source_label = f"homepage/{_slugify(link.region)}"
        # Try to read the written article text
        region_dir = articles_homepage_dir / _slugify(link.region)
        slug = _slugify(link.headline)
        art_path = region_dir / f"{slug}.md"
        text = ""
        if art_path.exists():
            raw = art_path.read_text(encoding="utf-8")
            # Strip header lines to get body text
            lines = raw.split("\n")
            text = "\n".join(lines[4:]).strip() if len(lines) > 4 else ""
        truncated = text[:300]
        suffix = "..." if len(text) > 300 else ""
        snippet = f"[{source_label}] {link.headline}\nContext: {truncated}{suffix}"
        headlines_with_snippets.append(snippet)

    # RSS headlines
    for article in rss_articles:
        source_label = f"rss/{article['source']}"
        text = article["text"] or article["summary"]
        truncated = text[:300]
        suffix = "..." if len(text) > 300 else ""
        snippet = (
            f"[{source_label}] {article['headline']}\nContext: {truncated}{suffix}"
        )
        headlines_with_snippets.append(snippet)

    # Phase 5: Generate research plan
    plan = generate_fp_research_plan(
        headlines_with_snippets, context_scripts=context_scripts
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

    # Phase 7: Fetch full text for short RSS articles
    for article in rss_articles:
        if len(article["text"]) < 500:
            full_text = _extract_article_text(article["url"])
            if full_text:
                article["text"] = full_text
                # Update the written file
                source_dir = articles_rss_dir / article["source"]
                slug = _slugify(article["headline"])
                art_path = source_dir / f"{slug}.md"
                if art_path.exists():
                    content = (
                        f"# {article['headline']}\n\n"
                        f"URL: {article['url']}\n"
                        f"Source: {article['source']}\n\n"
                        f"{full_text}"
                    )
                    art_path.write_text(content, encoding="utf-8")
