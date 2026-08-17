"""Directive→article matching, shared by the collector, consumer, and show notes.

Leaf module: imports nothing from ``pipeline`` so any module can use it without
risking an import cycle. See docs/plans/2026-08-17-unify-directive-article-join.md
for the measurements behind the cascade's design.
"""

from __future__ import annotations

import json
from pathlib import Path


# Number of leading lines of an article file that may carry the ``URL:`` header.
# Bounded so a URL appearing in body prose is never mistaken for the source URL.
_URL_HEADER_LINES = 8


# Framing matters: this text is retrieved by keyword search and is not
# guaranteed to cover the same story. Telling the writer that is the cheap
# mitigation for a wrong-story match in a pipeline with no human review.
#
# Lives here (a leaf module, imported by both consumer.py and __main__.py)
# rather than in consumer.py, where it originated: __main__.py's direct-Exa
# tier needs the same vocabulary and cannot import consumer without a cycle
# (consumer lazily imports __main__ already). consumer._OPEN_ACCESS_HEADING
# re-exports this so existing references keep working.
OPEN_ACCESS_HEADING = (
    "## Related coverage from other outlets\n"
    "(Retrieved by search. Use only the parts that clearly describe the "
    "story in the headline above; ignore anything that does not match.)"
)

# Used when there is no stub above the retrieved text at all -- the direct-
# Exa tier in __main__.find_rundown_article_source, reached when neither the
# index nor any legacy filesystem tier resolved the directive. Same framing
# vocabulary as OPEN_ACCESS_HEADING (outlets / retrieved by search / match
# the headline), adapted to say plainly that no original article text was
# available, since there is no true-headline stub anchoring the section here.
DIRECT_EXA_HEADING = (
    "## Third-party coverage retrieved by search\n"
    "(No original article text was available for this story. The following "
    "is third-party coverage from other outlets, retrieved by search. Use "
    "only the parts that clearly describe the story in the headline above; "
    "ignore anything that does not match.)"
)


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


def shadow_candidate(
    headline: str, index: dict[str, str]
) -> dict[str, str | float] | None:
    """Diagnostic only: what a headline-vs-headline fuzzy matcher would pick.

    Never used for resolution -- see ``resolve_headline``, which has no fuzzy
    tier and does not call this function. Scores Jaccard similarity of
    lowercased token sets against the *index keys* (headlines), never article
    bodies: body scoring is what made the deleted word-overlap tier length-
    biased (a longer body accumulates more incidental word overlap with an
    unrelated query, which is exactly the failure mode that produced a wrong
    match scoring higher than the correct one on real data).

    This exists to turn an unbounded unknown into forward-growing evidence.
    The corpus that justified deleting the fuzzy tier (54 directives, 10 work
    dirs) proved exact+slug sufficient *over that window*, but could not
    bound how often the editor reformulates a headline by word substitution
    (all 3 observed reformulations were whitespace-only). Logging the shadow
    candidate on every miss lets that question be answered with more data
    over time, without re-introducing the tier that fabricates.

    A non-zero score here is NOT by itself evidence that fuzzy matching
    should be restored: a plausible-looking shadow candidate is exactly what
    the deleted tier produced on its 50/54 wrong-but-scoring cases too. What
    would matter is a sustained pattern of misses whose shadow candidate is
    manually confirmed correct -- a single score, or even many, proves
    nothing on its own.

    Returns ``{"path": rel_path, "score": jaccard}`` for the best-scoring
    index key, or ``None`` if the index is empty or every key scores 0. Ties
    are broken by score first, then lexicographically smallest path, so the
    diagnostic is reproducible.
    """
    query_tokens = set(headline.lower().split())
    best: tuple[float, str] | None = None
    for key, rel in index.items():
        key_tokens = set(key.lower().split())
        union = query_tokens | key_tokens
        if not union:
            continue
        score = len(query_tokens & key_tokens) / len(union)
        if score <= 0:
            continue
        candidate = (score, rel)
        if best is None or candidate[0] > best[0]:
            best = candidate
        elif candidate[0] == best[0] and candidate[1] < best[1]:
            best = candidate

    if best is None:
        return None
    score, rel = best
    return {"path": rel, "score": round(score, 3)}


def load_index(work_dir: Path) -> dict[str, str]:
    """Load headline_index.json, returning {} if absent/unreadable/wrong-shape.

    The single reader of headline_index.json outside ``resolve_headline``'s
    caller cascade -- a second ad-hoc index read is exactly the "drifted
    duplicate implementation" bug class this project has already been burned
    by (my-podcasts-78b). Callers that must distinguish "absent" from
    "present but unreadable" (as ``find_rundown_article_source`` does, for
    its ``no_index`` vs ``index_unreadable`` miss reasons) should check
    ``(work_dir / "headline_index.json").exists()`` themselves before calling
    this -- a cheap existence check, not a duplicate parse.
    """
    index_path = work_dir / "headline_index.json"
    if not index_path.exists():
        return {}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(index, dict):
            raise ValueError("headline_index.json is not an object")
    except Exception:
        return {}
    return index
