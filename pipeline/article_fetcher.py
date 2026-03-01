from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

SOURCE_LABELS = {
    "archive": "Based on the full archived article",
    "live": "Based on the publicly available portion of the article",
    "headline_only": "Based on the headline alone",
}


@dataclass(frozen=True)
class FetchedArticle:
    url: str
    content: str
    source_tier: str  # "archive", "live", or "headline_only"

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source_tier, self.source_tier)


def _extract_article_text(html: str) -> str:
    """Extract readable text from an HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    article = soup.find("article")
    target = article if article else soup.find("body") or soup
    text = target.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def _try_archive_today(url: str) -> str | None:
    """Try to fetch the article from archive.today."""
    archive_url = f"https://archive.today/newest/{quote(url, safe='')}"
    try:
        response = requests.get(
            archive_url,
            headers=_BROWSER_HEADERS,
            timeout=15,
            allow_redirects=True,
        )
        if response.status_code != 200:
            return None
        if "/newest/" in response.url:
            return None
        return _extract_article_text(response.text)
    except Exception:
        return None


def _try_live_url(url: str) -> str | None:
    """Try to fetch the article directly."""
    try:
        response = requests.get(
            url,
            headers=_BROWSER_HEADERS,
            timeout=15,
            allow_redirects=True,
        )
        if response.status_code != 200:
            return None
        text = _extract_article_text(response.text)
        if len(text) < 200:
            return None
        return text
    except Exception:
        return None


def fetch_article(url: str, headline: str) -> FetchedArticle:
    """Fetch article content with fallback chain: archive -> live -> headline."""
    content = _try_archive_today(url)
    if content:
        return FetchedArticle(url=url, content=content, source_tier="archive")

    content = _try_live_url(url)
    if content:
        return FetchedArticle(url=url, content=content, source_tier="live")

    return FetchedArticle(url=url, content=headline, source_tier="headline_only")


def fetch_all_articles(
    links: list[dict],
    delay_between: float = 3.0,
) -> list[FetchedArticle]:
    """Fetch all articles with a delay between requests to be polite."""
    results: list[FetchedArticle] = []
    for i, link in enumerate(links):
        if i > 0:
            time.sleep(delay_between)
        article = fetch_article(link["resolved_url"], link["headline_context"])
        results.append(article)
    return results
