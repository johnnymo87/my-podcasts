from __future__ import annotations

from pipeline.chinatalk_classifier import is_transcript


def test_is_transcript_returns_false_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert is_transcript("any body", "any subject") is False


from unittest.mock import MagicMock, patch


def test_is_transcript_returns_true_on_yes(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_response = MagicMock()
    mock_response.text = "YES"
    with patch("pipeline.chinatalk_classifier.genai") as mock_genai:
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = mock_response

        assert is_transcript("Speaker A: Hello\nSpeaker B: Hi", "ChinaTalk: Episode 42") is True

        mock_genai.Client.assert_called_once_with(api_key="fake-key")
        call_kwargs = mock_client.models.generate_content.call_args[1]
        assert "gemini-3.1-flash-lite" in call_kwargs["model"]
