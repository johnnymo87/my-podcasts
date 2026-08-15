from __future__ import annotations

from pipeline import article_fetcher
from pipeline.article_fetcher import FetchedArticle, fetch_article


def test_live_fetch_success(monkeypatch) -> None:
    def fake_get(url, **kwargs):
        class OkResponse:
            status_code = 200
            text = (
                "<html><body><p>Live article content. "
                + "x" * 200
                + "</p></body></html>"
            )
            url = "https://example.com/article"

        return OkResponse()

    monkeypatch.setattr("requests.get", fake_get)

    result = fetch_article("https://example.com/article", "Test headline")
    assert result.source_tier == "live"
    assert "Live article content" in result.content


def test_all_fetches_fail_returns_fetch_error(monkeypatch) -> None:
    def fake_get(url, **kwargs):
        raise Exception("network error")

    monkeypatch.setattr("requests.get", fake_get)

    result = fetch_article("https://example.com/article", "Test headline")
    assert result.source_tier == "fetch_error"
    assert result.content == "Test headline"


def test_source_tier_label_messages() -> None:
    b = FetchedArticle(url="x", content="y", source_tier="live")
    assert "publicly available" in b.source_label.lower()

    c = FetchedArticle(url="x", content="y", source_tier="headline_only")
    assert "headline" in c.source_label.lower()


class _Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def _long_page(n=500):
    return "<html><body><article>" + ("word " * n) + "</article></body></html>"


def test_live_fetch_is_tier_live(monkeypatch):
    monkeypatch.setattr(
        article_fetcher.requests, "get", lambda *a, **kw: _Resp(200, _long_page())
    )
    result = article_fetcher.fetch_article("https://x.test/a", "Headline")
    assert result.source_tier == "live"
    assert result.extracted_chars > 200
    assert "word" in result.content


def test_short_body_on_200_is_paywalled(monkeypatch):
    monkeypatch.setattr(
        article_fetcher.requests,
        "get",
        lambda *a, **kw: _Resp(200, "<html><body>Subscribe to read.</body></html>"),
    )
    result = article_fetcher.fetch_article("https://x.test/a", "Headline")
    assert result.source_tier == "paywalled"
    assert result.content == "Headline"
    assert 0 < result.extracted_chars < 200


def test_non_200_is_http_error(monkeypatch):
    monkeypatch.setattr(
        article_fetcher.requests, "get", lambda *a, **kw: _Resp(404, "nope")
    )
    result = article_fetcher.fetch_article("https://x.test/a", "Headline")
    assert result.source_tier == "http_error"
    assert result.extracted_chars == 0


def test_exception_is_fetch_error(monkeypatch):
    def _boom(*a, **kw):
        raise TimeoutError("boom")

    monkeypatch.setattr(article_fetcher.requests, "get", _boom)
    result = article_fetcher.fetch_article("https://x.test/a", "Headline")
    assert result.source_tier == "fetch_error"


def test_fetch_all_articles_preserves_tier(monkeypatch):
    """The discard of source_tier here is what hid a 93% stub rate."""
    monkeypatch.setattr(
        article_fetcher.requests, "get", lambda *a, **kw: _Resp(404, "nope")
    )
    articles = article_fetcher.fetch_all_articles(
        [{"resolved_url": "https://x.test/a", "headline_context": "Headline"}],
        delay_between=0,
    )
    assert articles[0].source_tier == "http_error"
    assert articles[0].extracted_chars == 0
