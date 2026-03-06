from __future__ import annotations

from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class HomepageLink:
    """A single link scraped from antiwar.com's region-grouped link section."""

    region: str
    headline: str
    url: str


def parse_homepage_links(html: str) -> list[HomepageLink]:
    """Parse antiwar.com HTML and extract region-grouped links.

    Finds all ``<td class="hotspot">`` elements as region headers. For each,
    walks to the parent ``<tr>``, finds the next sibling ``<tr>`` which
    contains the nested link table, and extracts all ``<a>`` tags with their
    href and text.
    """
    soup = BeautifulSoup(html, "html.parser")
    links: list[HomepageLink] = []

    for hotspot_td in soup.find_all("td", class_="hotspot"):
        region = hotspot_td.get_text(strip=True)
        if not region:
            continue

        # Walk up to the parent <tr>, then find the next sibling <tr>.
        parent_tr = hotspot_td.find_parent("tr")
        if parent_tr is None:
            continue

        next_tr = parent_tr.find_next_sibling("tr")
        if next_tr is None:
            continue

        for a_tag in next_tr.find_all("a"):
            href = a_tag.get("href", "").strip()
            headline = a_tag.get_text(strip=True)
            if not href or not headline:
                continue
            links.append(HomepageLink(region=region, headline=headline, url=href))

    return links


def scrape_homepage() -> list[HomepageLink]:
    """Fetch antiwar.com and return all region-grouped links.

    Uses a ``podcast-pipeline/1.0`` User-Agent and a 30-second timeout.
    """
    response = requests.get(
        "https://www.antiwar.com",
        headers={"User-Agent": "podcast-pipeline/1.0"},
        timeout=30,
    )
    response.raise_for_status()
    return parse_homepage_links(response.text)
