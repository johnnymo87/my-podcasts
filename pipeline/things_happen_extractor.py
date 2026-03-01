from __future__ import annotations

import re
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class ThingsHappenLink:
    link_text: str
    raw_url: str
    headline_context: str


def extract_things_happen(html_content: str) -> list[ThingsHappenLink]:
    """Extract links from the 'Things happen' section of a Levine email."""
    soup = BeautifulSoup(html_content, "html.parser")

    th_h2 = None
    for h2 in soup.find_all("h2"):
        if "things happen" in h2.get_text().lower():
            th_h2 = h2
            break

    if th_h2 is None:
        return []

    p_tag = th_h2.find_next("p")
    if p_tag is None:
        return []

    full_text = p_tag.get_text()
    # Split into sentences on ". " or ".\xa0" boundaries.
    sentences = re.split(r"\.[\s\xa0]+", full_text)

    links: list[ThingsHappenLink] = []
    for a_tag in p_tag.find_all("a"):
        href = a_tag.get("href", "")
        if not href:
            continue
        link_text = a_tag.get_text().strip()
        if not link_text:
            continue

        # Find the sentence containing this link text.
        headline_context = link_text
        for sentence in sentences:
            if link_text in sentence:
                headline_context = sentence.strip()
                break

        links.append(
            ThingsHappenLink(
                link_text=link_text,
                raw_url=href,
                headline_context=headline_context,
            )
        )

    return links


def resolve_redirect_url(url: str) -> str:
    """Follow redirects to get the final URL. Returns original on failure."""
    try:
        response = requests.head(url, allow_redirects=True, timeout=10)
        return response.url
    except Exception:
        return url
