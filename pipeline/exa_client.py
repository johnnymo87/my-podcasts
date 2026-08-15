from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exa_py import Exa


# exa_py issues requests.get/requests.post with no timeout= (verified in the
# installed package, exa_py/api.py:1417-1439), so a stuck TCP connection is
# not an exception -- it blocks forever. The consumer is a single loop
# serving four pipelines, so one wedged call would stall all of them. Bound
# it from our side instead.
_EXA_TIMEOUT_SECONDS = 30


def _search_with_timeout(exa: Exa, headline: str, **kwargs: object) -> Any:
    """Run exa.search in a worker thread, bounded by _EXA_TIMEOUT_SECONDS.

    A fresh, single-use executor per call (not a shared/module-level pool):
    reusing one pool would let a hung call block every later call queued
    behind it, reintroducing the wedge this function exists to prevent.

    On timeout we deliberately do NOT join or cancel the worker thread --
    shutdown(wait=False) returns immediately without waiting for the hung
    call to finish. (A `with ThreadPoolExecutor(...)` block would be wrong
    here: __exit__ calls shutdown(wait=True), which blocks until the hung
    task completes and reintroduces the exact hang this is meant to avoid.)
    The abandoned thread leaks one idle socket until the hung call eventually
    returns or errors; that is the accepted tradeoff for a long-lived loop.
    """
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(exa.search, headline, **kwargs)
    try:
        return future.result(timeout=_EXA_TIMEOUT_SECONDS)
    finally:
        executor.shutdown(wait=False)


@dataclass(frozen=True)
class ExaResult:
    title: str
    url: str
    text: str


def search_related_status(
    headline: str,
    *,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
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
        response = _search_with_timeout(
            exa,
            headline,
            num_results=num_results,
            type="auto",
            contents={"text": {"max_characters": 3000}},
            include_domains=include_domains,
            exclude_domains=exclude_domains,
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


def exa_result_sections(work_dir: Path, slug: str, *, limit: int = 2) -> str:
    """The `## [title](url)` sections of a slug's Exa file, headers stripped.

    `exa_text_if_hit` returns the raw file, which carries the `Result:` and
    `Query:` bookkeeping headers. Those must never reach the writer prompt,
    which consumes article text verbatim. This is the writer-facing view.

    Returns "" when the file is absent, when the search was not a hit, or
    when the file carries no result sections. `limit` caps how many results
    are returned; syndicated copies of the same wire story are common, so
    the tail is usually redundant.
    """
    text = exa_text_if_hit(work_dir, slug)
    if not text:
        return ""
    # Anchor on the "## [title](url)" shape the collector writes, NOT on a
    # bare "## ": Exa result bodies are scraped article text and can contain
    # their own markdown headings, which would split a section mid-body and
    # silently drop the next result.
    parts = re.split(r"\n(?=## \[)", text)
    sections = [p.strip() for p in parts if p.lstrip().startswith("## [")]
    if not sections:
        return ""
    return "\n\n".join(sections[:limit])
