from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from exa_py import Exa


@dataclass(frozen=True)
class ExaResult:
    title: str
    url: str
    text: str


def search_related_status(
    headline: str,
    *,
    include_domains: list[str] | None = None,
    num_results: int = 3,
) -> tuple[list[ExaResult], str]:
    """Search Exa, reporting why the result list is empty when it is.

    Status is one of ``hit``, ``empty``, ``no_key``, or ``error:{ExcClass}``.
    ``search_related`` is the status-free view of this, kept because the FP
    collector relies on errors being swallowed.
    """
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return [], "no_key"

    try:
        exa = Exa(api_key=api_key)
        response = exa.search(
            headline,
            num_results=num_results,
            type="auto",
            contents={"text": {"max_characters": 3000}},
            include_domains=include_domains,
        )

        results = [
            ExaResult(
                title=r.title or "",
                url=r.url or "",
                text=r.text or "",
            )
            for r in response.results
        ]
    except Exception as exc:
        return [], f"error:{type(exc).__name__}"

    return results, ("hit" if results else "empty")


def search_related(
    headline: str,
    *,
    include_domains: list[str] | None = None,
    num_results: int = 3,
) -> list[ExaResult]:
    """Search Exa for articles related to a headline.

    Returns empty list if EXA_API_KEY is not set or on any error.
    """
    results, _status = search_related_status(
        headline,
        include_domains=include_domains,
        num_results=num_results,
    )
    return results


def exa_file_path(work_dir: Path, slug: str) -> Path:
    """Path to a slug's Exa enrichment file under a job's work directory."""
    return work_dir / "enrichment" / "exa" / f"{slug}.md"


def exa_text_if_hit(work_dir: Path, slug: str) -> str:
    """Exa file contents, but only when the search actually returned results.

    The collector writes this file unconditionally so that misses are
    observable. That makes gating the readers mandatory: a `Result: empty`
    stub is not article text and must never reach the writer or the show
    notes.
    """
    exa_file = exa_file_path(work_dir, slug)
    if not exa_file.exists():
        return ""
    text = exa_file.read_text(encoding="utf-8")
    # The write format is fixed (title / Result / Query / blank line), so the
    # Result header is always line 2 -- 5 lines is generous headroom to find
    # it, not a magic count to match against a length elsewhere.
    for line in text.split("\n")[:5]:
        if line.startswith("Result: "):
            return text if line.removeprefix("Result: ").strip() == "hit" else ""
    # No Result header: an FP-written file. This branch is PERMANENT, not a
    # migration shim -- show_notes._find_article_file is shared with the FP
    # path, whose collector (fp_collector.py:396-398) writes no header and is
    # deliberately not being changed. Deleting this branch as "legacy" would
    # silently break FP show notes.
    return text
