"""Transcribe rendered audio with Gemini, for the omission detector.

The model gets AUDIO ONLY, never the script: a transcriber that can see the
script can "hear" what it expects. Callers align afterwards (verify.py).

The SDK's own retries are OFF (``HttpRetryOptions(attempts=1)``) and the
timeout is explicit; the caller owns retry policy and, in T3, the hard
deadline (a child process). ``timeout`` is per HTTP request, not a wall clock.
"""

from __future__ import annotations

import io
import os
import time
import wave
from dataclasses import dataclass

import httpx
from google import genai
from google.genai import types

from pipeline.tts.config import PCM_SAMPLE_RATE


ASR_MODEL = "gemini-3.8-flash"
ASR_PROMPT_VERSION = "1"
ASR_PROMPT = (
    "Transcribe the speech in this audio verbatim. Output only the spoken words "
    "as plain text: no timestamps, no speaker labels, no headings, no commentary. "
    "Write numbers as digits."
)
DEFAULT_ASR_TIMEOUT_SECONDS = 90.0


class TranscriptionUnavailable(Exception):
    """Explicit evidence the transcript cannot be trusted: never a pass.

    ``reason`` is one of ``asr_error``, ``asr_timeout``, ``asr_empty``,
    ``asr_incomplete`` (finish reason other than STOP, or none).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


@dataclass(frozen=True)
class Transcription:
    text: str
    model: str
    prompt_version: str
    finish_reason: str
    elapsed_s: float
    input_tokens: int | None
    output_tokens: int | None


def _make_genai_client_unguarded(*, timeout_s: float) -> genai.Client:
    return genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=types.HttpOptions(
            timeout=int(timeout_s * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def _make_genai_client(timeout_s: float) -> genai.Client:
    return _make_genai_client_unguarded(timeout_s=timeout_s)


class GeminiTranscriber:
    """Callable ``(audio_bytes, mime_type) -> Transcription``. Client is lazy."""

    def __init__(
        self, *, model: str = ASR_MODEL, timeout_s: float = DEFAULT_ASR_TIMEOUT_SECONDS
    ) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self._client: genai.Client | None = None

    def __call__(self, audio: bytes, mime_type: str) -> Transcription:
        if self._client is None:
            self._client = _make_genai_client(self.timeout_s)
        started = time.monotonic()
        try:
            resp = self._client.models.generate_content(
                model=self.model,
                contents=[
                    types.Part.from_bytes(data=audio, mime_type=mime_type),
                    ASR_PROMPT,
                ],
                config=types.GenerateContentConfig(temperature=0),
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise TranscriptionUnavailable("asr_timeout", repr(exc)) from exc
        except Exception as exc:  # any SDK/transport failure = no transcript
            raise TranscriptionUnavailable("asr_error", repr(exc)) from exc
        elapsed = time.monotonic() - started

        if not resp.candidates:
            raise TranscriptionUnavailable("asr_empty", "no candidates")
        finish = resp.candidates[0].finish_reason
        finish_name = finish.name if finish is not None else "NONE"
        if finish_name != "STOP":
            raise TranscriptionUnavailable(
                "asr_incomplete", f"finish_reason={finish_name}"
            )
        text = resp.text or ""
        if not text.strip():
            raise TranscriptionUnavailable("asr_empty", "blank transcript")
        usage = resp.usage_metadata
        return Transcription(
            text=text,
            model=self.model,
            prompt_version=ASR_PROMPT_VERSION,
            finish_reason=finish_name,
            elapsed_s=elapsed,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def pcm_to_wav(pcm: bytes, sample_rate: int = PCM_SAMPLE_RATE) -> bytes:
    """Wrap 16-bit mono PCM (what providers return) in a WAV container."""
    if len(pcm) % 2:
        raise ValueError(
            f"PCM length {len(pcm)} is not a whole number of 16-bit samples"
        )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()
