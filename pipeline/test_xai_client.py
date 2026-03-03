from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.xai_client import XaiResult, search_twitter


def _make_mock_response(text: str) -> MagicMock:
    """Build a mock Response with .content attribute."""
    response = MagicMock()
    response.content = text
    return response


def test_search_twitter_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock xai_sdk Client, verify XaiResult is returned with correct summary text."""
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    mock_response = _make_mock_response("Summary of Twitter discussion.")
    mock_chat = MagicMock()
    mock_chat.sample.return_value = mock_response

    with patch("xai_sdk.Client") as MockClient:
        mock_client = MockClient.return_value
        mock_client.chat.create.return_value = mock_chat

        result = search_twitter("Test headline")

    assert result is not None
    assert isinstance(result, XaiResult)
    assert result.summary == "Summary of Twitter discussion."
    mock_client.close.assert_called_once()


def test_search_twitter_returns_none_on_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No XAI_API_KEY env var -> returns None."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    result = search_twitter("Test headline")

    assert result is None


def test_search_twitter_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """xai_sdk.Client constructor raises -> returns None."""
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    with patch("xai_sdk.Client") as MockClient:
        MockClient.side_effect = RuntimeError("connection failed")

        result = search_twitter("Test headline")

    assert result is None


def test_search_twitter_returns_none_on_empty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty response content -> returns None."""
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    mock_response = _make_mock_response("")
    mock_chat = MagicMock()
    mock_chat.sample.return_value = mock_response

    with patch("xai_sdk.Client") as MockClient:
        mock_client = MockClient.return_value
        mock_client.chat.create.return_value = mock_chat

        result = search_twitter("Test headline")

    assert result is None
    mock_client.close.assert_called_once()
