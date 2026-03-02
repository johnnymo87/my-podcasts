from __future__ import annotations

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


def test_all_fetches_fail_returns_headline_only(monkeypatch) -> None:
    def fake_get(url, **kwargs):
        raise Exception("network error")

    monkeypatch.setattr("requests.get", fake_get)

    result = fetch_article("https://example.com/article", "Test headline")
    assert result.source_tier == "headline_only"
    assert result.content == "Test headline"


def test_source_tier_label_messages() -> None:
    b = FetchedArticle(url="x", content="y", source_tier="live")
    assert "publicly available" in b.source_label.lower()

    c = FetchedArticle(url="x", content="y", source_tier="headline_only")
    assert "headline" in c.source_label.lower()
