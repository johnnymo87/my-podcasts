"""Directive→article matching, shared by the collector, consumer, and show notes.

Leaf module: imports nothing from ``pipeline`` so any module can use it without
risking an import cycle. See docs/plans/2026-08-17-unify-directive-article-join.md
for the measurements behind the cascade's design.
"""

from __future__ import annotations


# Number of leading lines of an article file that may carry the ``URL:`` header.
# Bounded so a URL appearing in body prose is never mistaken for the source URL.
_URL_HEADER_LINES = 8


def slugify(text: str) -> str:
    """Create a safe filename slug from a headline.

    This is the *article-matching* slug family. ``script_processor`` and
    ``blog_poller`` have a deliberately different implementation for R2 keys
    and episode slugs: they use a regex that strips non-ASCII letters, while
    this one keeps them (``str.isalnum()`` is True for 'é'). Do not merge them.
    """
    safe = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:50]


def extract_url(text: str) -> str | None:
    """Return the ``URL:`` header value from article markdown, if present.

    All three Rundown sources write this header (verified across 335 real
    article files: 103 Levine, 166 Semafor, 66 Zvi -- 100% coverage).

    Unused in this PR: staged for the FP-routing fix, which must recover a
    source URL from a resolved article file. Deliberately NOT wired into
    ``show_notes._extract_url_from_article``, which scans the whole file and
    tolerates a missing space after ``URL:`` -- different behavior on purpose.
    """
    for line in text.split("\n")[:_URL_HEADER_LINES]:
        if line.startswith("URL: "):
            url = line[5:].strip()
            return url or None
    return None


def resolve_headline(
    headline: str, index: dict[str, str]
) -> tuple[str | None, str | None]:
    """Resolve a directive headline to a work-dir-relative article path.

    Cascade, in order:
      1. Exact match on the headline the collector recorded.
      2. Unique slug match (absorbs whitespace/punctuation reformulation).

    Returns ``(rel_path, None)`` on a hit and ``(None, reason)`` on a miss.

    There is deliberately **no** fuzzy tier. A word-overlap fallback used to sit
    here; measured against real data, a *wrong* article scored at least one
    query word in 50 of 54 cases and tied the correct article at a perfect score
    in one, so no threshold could separate them -- while exact+slug already
    covered 54/54. Article text is fed verbatim to the writer and published
    unread, so a miss (observable, degrades the section) is strictly preferable
    to a wrong match (invisible, fabricates confidently).
    """
    if headline in index:
        return index[headline], None

    slug = slugify(headline)
    if not slug:
        return None, "index_no_match"

    matches = {rel for key, rel in index.items() if slugify(key) == slug}
    if len(matches) == 1:
        return matches.pop(), None
    if len(matches) > 1:
        return None, "slug_ambiguous"
    return None, "index_no_match"
