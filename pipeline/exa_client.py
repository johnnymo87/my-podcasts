from __future__ import annotations

import os
from dataclasses import dataclass

from exa_py import Exa


@dataclass(frozen=True)
class ExaResult:
    title: str
    url: str
    text: str


def search_related(
    headline: str,
    *,
    include_domains: list[str] | None = None,
    num_results: int = 3,
) -> list[ExaResult]:
    """Search Exa for articles related to a headline.

    Returns empty list if EXA_API_KEY is not set or on any error.
    """
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return []

    try:
        exa = Exa(api_key=api_key)
        response = exa.search(
            headline,
            num_results=num_results,
            type="auto",
            contents={"text": {"max_characters": 3000}},
            include_domains=include_domains,
        )

        return [
            ExaResult(
                title=r.title or "",
                url=r.url or "",
                text=r.text or "",
            )
            for r in response.results
        ]
    except Exception:
        return []
