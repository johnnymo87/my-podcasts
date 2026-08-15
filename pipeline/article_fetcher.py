from __future__ import annotations

import time
from dataclasses import dataclass

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
    "live": "Based on the publicly available portion of the article",
    "paywalled": "Based on the headline alone",
    "http_error": "Based on the headline alone",
    "fetch_error": "Based on the headline alone",
    # Retained: pre-existing artifacts and the legacy summarizer path use it.
    "headline_only": "Based on the headline alone",
}


@dataclass(frozen=True)
class FetchedArticle:
    url: str
    content: str
    # "live" | "paywalled" | "http_error" | "fetch_error"
    source_tier: str
    # Characters extracted from the page body, before the headline fallback.
    # Recorded because the <200 paywall threshold is a weak proxy and needs to
    # stay retunable against history rather than by re-fetching.
    extracted_chars: int = 0

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source_tier, self.source_tier)


@dataclass(frozen=True)
class Article:
    """An article with its headline, URL, and fetched content."""

    headline: str
    url: str
    content: str
    source_tier: str = "unknown"
    extracted_chars: int = 0


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


_MIN_ARTICLE_CHARS = 200


def _try_live_url(url: str) -> tuple[str | None, str, int]:
    """Fetch the article, reporting the outcome rather than a bare None.

    Returns ``(text_or_None, tier, extracted_chars)``.
    """
    try:
        response = requests.get(
            url,
            headers=_BROWSER_HEADERS,
            timeout=15,
            allow_redirects=True,
        )
        if response.status_code != 200:
            return None, "http_error", 0
        text = _extract_article_text(response.text)
        if len(text) < _MIN_ARTICLE_CHARS:
            return None, "paywalled", len(text)
        return text, "live", len(text)
    except Exception:
        return None, "fetch_error", 0


def fetch_article(url: str, headline: str) -> FetchedArticle:
    """Fetch article content with fallback: live URL -> headline only."""
    content, tier, chars = _try_live_url(url)
    if content:
        return FetchedArticle(
            url=url, content=content, source_tier=tier, extracted_chars=chars
        )

    return FetchedArticle(
        url=url, content=headline, source_tier=tier, extracted_chars=chars
    )


def fetch_all_articles(
    links: list[dict],
    delay_between: float = 3.0,
) -> list[Article]:
    """Fetch all articles with a delay between requests to be polite."""
    results: list[Article] = []
    for i, link in enumerate(links):
        if i > 0:
            time.sleep(delay_between)
        fetched = fetch_article(link["resolved_url"], link["headline_context"])
        results.append(
            Article(
                headline=link["headline_context"],
                url=fetched.url,
                content=fetched.content,
                source_tier=fetched.source_tier,
                extracted_chars=fetched.extracted_chars,
            )
        )
    return results
