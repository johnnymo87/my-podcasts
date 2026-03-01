from __future__ import annotations

from pipeline.article_fetcher import FetchedArticle, fetch_article


def test_archive_today_success(monkeypatch) -> None:
    def fake_get(url, **kwargs):
        class FakeResponse:
            status_code = 200
            text = "<html><body><article><p>Full article text here.</p></article></body></html>"
            url = "https://archive.today/2026/https://example.com/article"

        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)

    result = fetch_article("https://example.com/article", "Test headline")
    assert isinstance(result, FetchedArticle)
    assert result.source_tier == "archive"
    assert "Full article text" in result.content


def test_archive_fails_falls_back_to_live(monkeypatch) -> None:
    call_count = 0

    def fake_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if "archive" in url:

            class FailResponse:
                status_code = 429
                text = ""
                url = "https://archive.today/newest/https://example.com/article"

            return FailResponse()
        else:

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
    a = FetchedArticle(url="x", content="y", source_tier="archive")
    assert "full archived article" in a.source_label.lower()

    b = FetchedArticle(url="x", content="y", source_tier="live")
    assert "publicly available" in b.source_label.lower()

    c = FetchedArticle(url="x", content="y", source_tier="headline_only")
    assert "headline" in c.source_label.lower()
