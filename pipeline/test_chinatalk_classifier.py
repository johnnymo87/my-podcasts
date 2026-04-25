from __future__ import annotations

from pipeline.chinatalk_classifier import is_transcript


def test_is_transcript_returns_false_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert is_transcript("any body", "any subject") is False
