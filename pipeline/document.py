from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    title: str
    byline: str
    canonical_url: str
    description: str
    report_text: str
    read_html: str | None
    slug: str
    style: str
    wordcount: int
    default_category: str
