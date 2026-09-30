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


def _status_error(code: int, body: object | None = None) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
    response = httpx.Response(code, request=request)
    return openai.APIStatusError("boom", response=response, body=body)


@pytest.mark.parametrize(
    "code,retryable",
    [
        (500, True),
        (503, True),
        (408, True),
        (409, True),
        (429, True),
        (400, False),
        (401, False),
        (404, False),
    ],
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


def test_insufficient_quota_429_is_not_retryable(monkeypatch) -> None:
    client = MagicMock()
    client.audio.speech.create.side_effect = _status_error(
        429, body={"code": "insufficient_quota", "message": "out of credit"}
    )
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    with pytest.raises(TTSProviderError) as exc:
        OpenAIProvider().synthesize("Hello.", CONFIG)
    assert exc.value.retryable is False


def test_rate_limit_429_with_other_code_is_retryable(monkeypatch) -> None:
    client = MagicMock()
    client.audio.speech.create.side_effect = _status_error(
        429, body={"code": "rate_limit_exceeded"}
    )
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    with pytest.raises(TTSProviderError) as exc:
        OpenAIProvider().synthesize("Hello.", CONFIG)
    assert exc.value.retryable is True


def test_bare_connection_error_is_retryable(monkeypatch) -> None:
    client = MagicMock()
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
    client.audio.speech.create.side_effect = openai.APIConnectionError(request=request)
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    with pytest.raises(TTSProviderError) as exc:
        OpenAIProvider().synthesize("Hello.", CONFIG)
    assert exc.value.retryable


def test_generic_api_error_is_not_retryable(monkeypatch) -> None:
    client = MagicMock()
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
    client.audio.speech.create.side_effect = openai.APIError(
        "weird", request, body=None
    )
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    with pytest.raises(TTSProviderError) as exc:
        OpenAIProvider().synthesize("Hello.", CONFIG)
    assert exc.value.retryable is False


def test_close_closes_client_and_is_idempotent(monkeypatch) -> None:
    client = _client_returning(b"\x00\x00")
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    provider = OpenAIProvider()
    provider.close()  # before any client was built: no-op
    provider.synthesize("Hello.", CONFIG)
    provider.close()
    provider.close()
    client.close.assert_called_once_with()


def test_context_manager_closes_client(monkeypatch) -> None:
    client = _client_returning(b"\x00\x00")
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    with OpenAIProvider() as provider:
        provider.synthesize("Hello.", CONFIG)
    client.close.assert_called_once_with()


def test_autouse_guard_blocks_real_client() -> None:
    with pytest.raises(AssertionError, match="real OpenAI"):
        providers._make_openai_client(timeout=1.0)
