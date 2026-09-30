"""TTS providers. Each returns raw 24 kHz mono s16le PCM for one chunk."""

from __future__ import annotations

from typing import TYPE_CHECKING

import openai


if TYPE_CHECKING:
    from pipeline.tts.config import RenderConfig

_RETRYABLE_STATUS = frozenset({408, 409, 429})


class TTSProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _make_openai_client_unguarded(*, timeout: float) -> openai.OpenAI:
    # max_retries=0: the renderer owns retries, so the SDK's hidden 2 retries x
    # 600 s default timeout cannot silently multiply a render's wall time.
    # ``timeout`` is a float, which httpx applies *per operation* (connect, and
    # each read gap) - not a total deadline. A slowly-streaming response can
    # exceed it; wall time is bounded by the renderer, not by this value.
    return openai.OpenAI(max_retries=0, timeout=timeout)


def _make_openai_client(timeout: float) -> openai.OpenAI:
    return _make_openai_client_unguarded(timeout=timeout)


def _is_insufficient_quota(exc: openai.APIError) -> bool:
    # The SDK parses ``code``/``type`` out of the (already-unwrapped) error body.
    return "insufficient_quota" in (exc.code, exc.type)


def _status_is_retryable(exc: openai.APIStatusError) -> bool:
    code = exc.status_code
    if code == 429 and _is_insufficient_quota(exc):
        return False  # out of credit: retrying cannot help
    return code in _RETRYABLE_STATUS or code >= 500


class OpenAIProvider:
    """Renders one chunk of text to PCM via OpenAI.

    ``synthesize`` is total for API failures: every ``openai.APIError`` becomes a
    ``TTSProviderError`` with ``retryable`` set. The one deliberate exception is
    a missing/invalid API key, which raises ``openai.OpenAIError`` from client
    construction on the first call. That is configuration, not a transient
    failure, so it propagates unwrapped and the renderer sees a
    non-``TTSProviderError`` (fail loudly, never retry).

    Use as a context manager, or call ``close()``, to release the HTTP client.
    """

    # Changing the request shape (model/voice/format/params sent to OpenAI) or
    # ``max_chars`` (which moves chunk boundaries) changes the audio for the same
    # text: bump ``cache.RENDERER_VERSION`` so stale cache entries are not replayed.
    max_chars = 4096

    def __init__(self, *, timeout: float = 120.0) -> None:
        self._timeout = timeout
        self._client: openai.OpenAI | None = None

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def __enter__(self) -> OpenAIProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def synthesize(self, text: str, config: RenderConfig) -> bytes:
        if self._client is None:
            self._client = _make_openai_client(self._timeout)
        try:
            response = self._client.audio.speech.create(
                model=config.openai_model,
                voice=config.openai_voice,
                input=text,
                response_format="pcm",
            )
            data = response.read()
        except openai.APIStatusError as exc:
            raise TTSProviderError(
                f"OpenAI HTTP {exc.status_code}: {exc}",
                retryable=_status_is_retryable(exc),
            ) from exc
        except openai.APIConnectionError as exc:
            raise TTSProviderError(f"OpenAI connection: {exc}", retryable=True) from exc
        except openai.APIError as exc:
            raise TTSProviderError(f"OpenAI API error: {exc}", retryable=False) from exc
        if not data or len(data) % 2:
            raise TTSProviderError(
                f"OpenAI returned {len(data)} bytes of PCM", retryable=True
            )
        return data
