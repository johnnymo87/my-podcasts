"""TTS providers. Each returns raw 24 kHz mono s16le PCM for one chunk."""

from __future__ import annotations

from typing import TYPE_CHECKING

import openai


if TYPE_CHECKING:
    from pipeline.tts.config import RenderConfig

PCM_SAMPLE_RATE = 24_000
PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * 2  # mono, 16-bit


class TTSProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _make_openai_client_unguarded(*, timeout: float) -> openai.OpenAI:
    # max_retries=0: the renderer owns retries, so the SDK's hidden 2 retries x
    # 600 s default timeout cannot silently multiply a render's wall time.
    return openai.OpenAI(max_retries=0, timeout=timeout)


def _make_openai_client(timeout: float) -> openai.OpenAI:
    return _make_openai_client_unguarded(timeout=timeout)


class OpenAIProvider:
    max_chars = 4096

    def __init__(self, *, timeout: float = 120.0) -> None:
        self._timeout = timeout
        self._client: openai.OpenAI | None = None

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
            retryable = exc.status_code >= 500 or exc.status_code == 429
            raise TTSProviderError(
                f"OpenAI HTTP {exc.status_code}: {exc}", retryable=retryable
            ) from exc
        except openai.APIConnectionError as exc:
            raise TTSProviderError(f"OpenAI connection: {exc}", retryable=True) from exc
        if not data or len(data) % 2:
            raise TTSProviderError(
                f"OpenAI returned {len(data)} bytes of PCM", retryable=True
            )
        return data
