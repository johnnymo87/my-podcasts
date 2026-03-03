from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.exa_client import ExaResult, search_related


def _make_mock_result(title: str, url: str, text: str) -> MagicMock:
    r = MagicMock()
    r.title = title
    r.url = url
    r.text = text
    return r


def test_search_related_returns_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock Exa, verify results are returned as ExaResult dataclasses."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    mock_response = MagicMock()
    mock_response.results = [
        _make_mock_result("Article One", "https://example.com/1", "Some text here"),
        _make_mock_result("Article Two", "https://example.com/2", "More text here"),
    ]

    with patch("pipeline.exa_client.Exa") as MockExa:
        mock_exa_instance = MockExa.return_value
        mock_exa_instance.search_and_contents.return_value = mock_response

        results = search_related("test headline")

    assert len(results) == 2
    assert isinstance(results[0], ExaResult)
    assert results[0].title == "Article One"
    assert results[0].url == "https://example.com/1"
    assert results[0].text == "Some text here"
    assert isinstance(results[1], ExaResult)
    assert results[1].title == "Article Two"


def test_search_related_with_include_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock Exa, verify include_domains is passed through."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    mock_response = MagicMock()
    mock_response.results = [
        _make_mock_result("Domain Article", "https://reuters.com/1", "Reuters text"),
    ]

    with patch("pipeline.exa_client.Exa") as MockExa:
        mock_exa_instance = MockExa.return_value
        mock_exa_instance.search_and_contents.return_value = mock_response

        results = search_related(
            "test headline",
            include_domains=["reuters.com", "apnews.com"],
        )

        call_kwargs = mock_exa_instance.search_and_contents.call_args
        assert call_kwargs.kwargs.get("include_domains") == [
            "reuters.com",
            "apnews.com",
        ]

    assert len(results) == 1
    assert results[0].title == "Domain Article"


def test_search_related_returns_empty_on_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No EXA_API_KEY env var → returns []."""
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    results = search_related("some headline")

    assert results == []


def test_search_related_returns_empty_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exa constructor raises → returns []."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    with patch("pipeline.exa_client.Exa") as MockExa:
        MockExa.side_effect = RuntimeError("connection failed")

        results = search_related("some headline")

    assert results == []
