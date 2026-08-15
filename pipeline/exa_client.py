from __future__ import annotations

import os
from dataclasses import dataclass

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
