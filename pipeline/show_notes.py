from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Create a safe filename slug from a headline."""
    safe = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:50]


def _extract_url_from_article(path: Path) -> str | None:
    """Parse the URL: line from an article markdown file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("URL:"):
            url = stripped[4:].strip()
            return url if url else None
    return None


def _find_article_file(headline: str, source: str, work_dir: Path) -> Path | None:
    """Find the article file matching a directive headline.

    Uses the same search logic as the consumer's _find_article_text and
    __main__._find_rundown_article_text functions.
    """
    slug = _slugify(headline)
    if not slug:
        return None

    # Flat Levine-style articles (e.g. "00-headline.md")
    articles_dir = work_dir / "articles"
    if articles_dir.exists():
        for match in articles_dir.glob(f"*{slug}.md"):
            if match.parent == articles_dir:
                return match

    # Semafor articles
    semafor_file = articles_dir / "semafor" / f"{slug}.md"
    if semafor_file.exists():
        return semafor_file

    # Zvi articles (date-prefixed)
    zvi_dir = articles_dir / "zvi"
    if zvi_dir.exists():
        for match in zvi_dir.glob(f"*{slug}*.md"):
            return match

    # FP homepage articles (nested under region)
    homepage_dir = articles_dir / "homepage"
    if homepage_dir.exists():
        for match in homepage_dir.rglob(f"{slug}.md"):
            return match

    # FP RSS articles (nested under source)
    rss_dir = articles_dir / "rss"
    if rss_dir.exists():
        for match in rss_dir.rglob(f"{slug}.md"):
            return match

    # Routed articles
    routed_file = articles_dir / "routed" / f"{slug}.md"
    if routed_file.exists():
        return routed_file

    # Exa enrichment
    exa_file = work_dir / "enrichment" / "exa" / f"{slug}.md"
    if exa_file.exists():
        return exa_file

    return None


def _tokenize(text: str) -> set[str]:
    """Lowercase words of 3+ chars, for fuzzy headline matching."""
    return {w for w in text.lower().split() if len(w) >= 3}


def _headlines_match(article_title: str, covered_headline: str) -> bool:
    """Check if a covered headline matches an article title.

    Uses case-insensitive exact match first, then falls back to
    word-overlap: if >=50% of the shorter headline's significant words
    appear in the longer one, it's a match.
    """
    a_lower = article_title.lower()
    c_lower = covered_headline.lower()

    # Exact match
    if a_lower == c_lower:
        return True

    # Substring containment (either direction)
    if a_lower in c_lower or c_lower in a_lower:
        return True

    # Word overlap: require >=50% of the shorter headline's words AND
    # at least 2 shared words to avoid false positives on short headlines.
    a_words = _tokenize(article_title)
    c_words = _tokenize(covered_headline)
    if not a_words or not c_words:
        return False
    shorter, longer = (
        (a_words, c_words) if len(a_words) <= len(c_words) else (c_words, a_words)
    )
    overlap = len(shorter & longer)
    return overlap >= 2 and overlap >= len(shorter) * 0.5


def filter_show_notes_by_coverage(
    articles: list[dict],
    covered_headlines: list[str],
) -> list[dict]:
    """Filter show notes articles to only those the writer actually covered.

    If covered_headlines is empty, returns all articles (no filtering).
    Matching is fuzzy: case-insensitive, with substring and word-overlap
    fallbacks.
    """
    if not covered_headlines:
        return articles

    return [
        article
        for article in articles
        if any(
            _headlines_match(article["title"], headline)
            for headline in covered_headlines
        )
    ]


def extract_show_notes_articles(work_dir: Path) -> list[dict]:
    """Extract article titles, URLs, and themes from a work directory.

    Reads plan.json for themes and directives, then finds URLs from
    the corresponding article files. Returns a list of dicts:
    [{"title": str, "url": str | None, "theme": str}, ...]

    Ordered by theme order from the plan, then by priority within theme.
    Returns empty list if plan.json is missing or unparseable.
    """
    plan_path = work_dir / "plan.json"
    if not plan_path.exists():
        logger.warning("No plan.json found in %s", work_dir)
        return []

    try:
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read plan.json: %s", exc)
        return []

    themes: list[str] = plan_data.get("themes", [])
    directives: list[dict] = plan_data.get("directives", [])

    # Build theme ordering map
    theme_order = {theme: i for i, theme in enumerate(themes)}

    # Collect included directives with their URLs
    results: list[dict] = []
    for directive in directives:
        if not directive.get("include_in_episode", False):
            continue

        headline = directive.get("headline", "")
        source = directive.get("source", "")
        theme = directive.get("theme", "")
        priority = directive.get("priority", 99)

        article_file = _find_article_file(headline, source, work_dir)
        url = _extract_url_from_article(article_file) if article_file else None

        results.append(
            {
                "title": headline,
                "url": url,
                "theme": theme,
                "_theme_order": theme_order.get(theme, 999),
                "_priority": priority,
            }
        )

    # Sort by theme order, then priority
    results.sort(key=lambda r: (r["_theme_order"], r["_priority"]))

    # Strip internal sort keys
    return [
        {"title": r["title"], "url": r["url"], "theme": r["theme"]} for r in results
    ]
