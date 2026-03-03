from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.xai_client import XaiResult, search_twitter


def _make_mock_response(text: str) -> MagicMock:
    """Build a mock response matching xAI's response.output structure."""
    content_item = MagicMock()
    content_item.type = "output_text"
    content_item.text = text

    output_item = MagicMock()
    output_item.type = "message"
    output_item.content = [content_item]

    response = MagicMock()
    response.output = [output_item]
    return response


def test_search_twitter_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock OpenAI client, verify XaiResult is returned with correct summary text."""
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    mock_response = _make_mock_response("Summary of Twitter discussion.")

    with patch("pipeline.xai_client.OpenAI") as MockOpenAI:
        mock_client = MockOpenAI.return_value
        mock_client.responses.create.return_value = mock_response

        result = search_twitter("Test headline")

    assert result is not None
    assert isinstance(result, XaiResult)
    assert result.summary == "Summary of Twitter discussion."


def test_search_twitter_returns_none_on_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No XAI_API_KEY env var → returns None."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    result = search_twitter("Test headline")

    assert result is None


def test_search_twitter_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI constructor raises → returns None."""
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    with patch("pipeline.xai_client.OpenAI") as MockOpenAI:
        MockOpenAI.side_effect = RuntimeError("connection failed")

        result = search_twitter("Test headline")

    assert result is None
