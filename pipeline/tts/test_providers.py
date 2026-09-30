from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import openai
import pytest

from pipeline.tts import providers
from pipeline.tts.config import openai_config
from pipeline.tts.providers import OpenAIProvider, TTSProviderError


CONFIG = openai_config(model="tts-1-hd", voice="onyx")


def _client_returning(data: bytes) -> MagicMock:
    client = MagicMock()
    client.audio.speech.create.return_value.read.return_value = data
    return client


def test_synthesize_requests_pcm_with_config(monkeypatch) -> None:
    client = _client_returning(b"\x01\x00" * 10)
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    pcm = OpenAIProvider().synthesize("Hello.", CONFIG)
    assert pcm == b"\x01\x00" * 10
    client.audio.speech.create.assert_called_once_with(
        model="tts-1-hd", voice="onyx", input="Hello.", response_format="pcm"
    )


def test_client_is_built_with_sdk_retries_off(monkeypatch) -> None:
    built = {}

    def fake_openai(**kwargs):
        built.update(kwargs)
        return _client_returning(b"\x00\x00")

    monkeypatch.setattr(providers.openai, "OpenAI", fake_openai)
    providers._make_openai_client_unguarded(timeout=42.0)
    assert built == {"max_retries": 0, "timeout": 42.0}


@pytest.mark.parametrize("data", [b"", b"\x00"])
def test_empty_or_odd_pcm_is_retryable(monkeypatch, data: bytes) -> None:
    monkeypatch.setattr(
        providers, "_make_openai_client", lambda timeout: _client_returning(data)
    )
    with pytest.raises(TTSProviderError) as exc:
        OpenAIProvider().synthesize("Hello.", CONFIG)
    assert exc.value.retryable


def _status_error(code: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
    response = httpx.Response(code, request=request)
    return openai.APIStatusError("boom", response=response, body=None)


@pytest.mark.parametrize(
    "code,retryable",
    [(500, True), (503, True), (429, True), (400, False), (401, False)],
)
def test_status_errors_map_retryability(
    monkeypatch, code: int, retryable: bool
) -> None:
    client = MagicMock()
    client.audio.speech.create.side_effect = _status_error(code)
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    with pytest.raises(TTSProviderError) as exc:
        OpenAIProvider().synthesize("Hello.", CONFIG)
    assert exc.value.retryable is retryable


def test_connection_error_is_retryable(monkeypatch) -> None:
    client = MagicMock()
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
    client.audio.speech.create.side_effect = openai.APITimeoutError(request=request)
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    with pytest.raises(TTSProviderError) as exc:
        OpenAIProvider().synthesize("Hello.", CONFIG)
    assert exc.value.retryable


def test_autouse_guard_blocks_real_client() -> None:
    with pytest.raises(AssertionError, match="real OpenAI"):
        providers._make_openai_client(timeout=1.0)
