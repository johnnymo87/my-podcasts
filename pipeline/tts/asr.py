"""Transcribe rendered audio with Gemini, for the omission detector.

The model gets AUDIO ONLY, never the script: a transcriber that can see the
script can "hear" what it expects. Callers align afterwards (verify.py).

A response that came back but is unusable (blocked, empty, incomplete) still
reports what it consumed: ``TranscriptionUnavailable.usage`` carries it, read by
the same helper (``_read_usage``) as a success, so cost tracking survives a block
(``my-podcasts-9p3.18``). A call with no response has ``usage=None``: unknown.

The SDK's own retries are OFF (``HttpRetryOptions(attempts=1)``) and a
timeout is set explicitly; the caller owns retry policy. That timeout is an
httpx per-phase / per-read limit, NOT a per-request or wall-clock bound: a
server that trickles bytes can exceed it many times over. The real bound is
T3's child-process kill, so nothing here may be relied on to return promptly.
"""

from __future__ import annotations

import io
import math
import os
import threading
import time
import wave
from dataclasses import dataclass, replace

import httpx
from google import genai
from google.genai import types

from pipeline.tts.config import PCM_SAMPLE_RATE
from pipeline.tts.normalize import normalize_tokens


ASR_MODEL = "gemini-3.8-flash"
ASR_PROMPT_VERSION = "1"
ASR_PROMPT = (
    "Transcribe the speech in this audio verbatim. Output only the spoken words "
    "as plain text: no timestamps, no speaker labels, no headings, no commentary. "
    "Write numbers as digits."
)
ASR_TEMPERATURE = 0
DEFAULT_ASR_TIMEOUT_SECONDS = 90.0

# "default" sends no thinking config (the model's own default applies); "low"
# sends ThinkingLevel.LOW. gemini-3.8-flash rejects MINIMAL (HTTP 400). Production
# uses "low" (DEFAULT_THINKING); "default" stays selectable so the calibration
# harness can still run the model's own behaviour as a comparison.
THINKING_SETTINGS = ("default", "low")
DEFAULT_THINKING = "low"


def _check_thinking(thinking: str) -> str:
    if thinking not in THINKING_SETTINGS:
        raise ValueError(
            f"thinking must be one of {THINKING_SETTINGS}, got {thinking!r}"
        )
    return thinking


def policy_for(model: str = ASR_MODEL, thinking: str = DEFAULT_THINKING) -> str:
    """The ASR policy string for a model and thinking setting."""
    _check_thinking(thinking)
    return (
        f"{model}|prompt-v{ASR_PROMPT_VERSION}|temp{ASR_TEMPERATURE}"
        f"|thinking-{thinking}"
    )


# Everything that decides which audio passes, on the ASR side, for the
# production default: "gemini-3.8-flash|prompt-v1|temp0|thinking-low". It is
# derived from policy_for() (never retyped) so the two cannot drift. Changing
# ASR_MODEL, ASR_PROMPT (bump ASR_PROMPT_VERSION with it) or the generation
# config changes it, and with it verify.VERIFIER_POLICY, which is folded into
# the render cache key. A GeminiTranscriber built with another model or
# thinking setting reports its own string as ``.policy`` and on every
# Transcription, and verify_audio records that in the verdict.
#
# Why "low": the T5 thinking pilot (8 faithful dev bases + 8 dev cuts, 2
# repeats, both settings) found identical detection, zero false alarms and zero
# confirmed reconstructions either way, but "low" is about 2x faster (median
# ~3.4 s vs ~5.4 s per chunk) with no thinking tokens, and its worst clean-base
# margin was tighter (net_missing 2 over a 5-token span vs 4 over 12 for the
# model default). Before this was "thinking-default", an implicit setting.
ASR_POLICY = policy_for()


def _generation_config(thinking: str = DEFAULT_THINKING) -> types.GenerateContentConfig:
    """The one place the request's generation config is built (see ASR_POLICY)."""
    _check_thinking(thinking)
    if thinking == "low":
        return types.GenerateContentConfig(
            temperature=ASR_TEMPERATURE,
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.LOW
            ),
        )
    return types.GenerateContentConfig(temperature=ASR_TEMPERATURE)


# The one reason the Gemini phase retries; matched by equality, never by text.
ASR_BLOCKED = "asr_blocked"


# The block reasons the phase may retry: an allowlist, not "any real reason". The
# 2026-10-03 re-test showed only ``OTHER`` is transient (the same audio blocked
# twice, then passed twice); SAFETY, PROHIBITED_CONTENT, BLOCKLIST and any
# category the API adds later are content verdicts nobody has shown to clear on a
# retry, so they stay ``asr_empty`` and fail the phase at once.
RETRYABLE_BLOCK_REASONS = frozenset({types.BlockedReason.OTHER})
_RETRYABLE_BLOCK_NAMES = frozenset(r.name for r in RETRYABLE_BLOCK_REASONS)


def _block_name(block: object) -> str | None:
    """The block reason's enum name whether the SDK handed us the enum or a string
    (``"OTHER"``, or an enum's ``"BlockedReason.OTHER"`` rendering); None if absent."""
    if block is None:
        return None
    name = getattr(block, "name", None)
    if not isinstance(name, str):
        name = str(block).rsplit(".", 1)[-1]
    return name.strip().upper() or None


def _is_retryable_block(block: object) -> bool:
    return _block_name(block) in _RETRYABLE_BLOCK_NAMES


@dataclass(frozen=True)
class AsrUsage:
    """What a response that DID come back reported: its elapsed time and token
    counts. ``None`` for a count means the response did not say and it cannot be
    derived (unknown, not zero)."""

    elapsed_s: float
    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None
    # What verify_audio records on the unavailable verdict's AsrInfo: the
    # response's finish reason ("NONE" when it had no candidates) and the
    # transcript's length (0 unless a transcript came back).
    finish_reason: str = "NONE"
    transcript_chars: int = 0


def _count(value: object) -> int | None:
    """A reported token count only if it is a real int (not a bool); else unknown."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _read_usage(
    resp: object,
    elapsed_s: float,
    *,
    finish_reason: str = "NONE",
    transcript_chars: int = 0,
) -> AsrUsage:
    """The one place a response's usage is read, for a success and for every raise
    after a response came back, so the two cannot drift.

    ``output_tokens`` is ``candidates_token_count`` when reported. A blocked
    response omits it (2026-10-03 probe: prompt 1777, total 1777, no candidates
    count), and its output was genuinely 0, so when it is absent the output is
    ``total - prompt - thoughts``, but only when ``total`` and ``prompt`` are both
    reported and the result is not negative; otherwise it stays unknown.
    ``thinking_tokens`` is as reported (``None`` stays ``None``).

    Defensive: telemetry must never turn an unusable response into a different
    failure, so only real ints are used (anything else is unknown) and this never
    raises.
    """
    try:
        usage = getattr(resp, "usage_metadata", None)
        prompt = _count(getattr(usage, "prompt_token_count", None))
        output = _count(getattr(usage, "candidates_token_count", None))
        thoughts = _count(getattr(usage, "thoughts_token_count", None))
        if output is None:
            total = _count(getattr(usage, "total_token_count", None))
            if total is not None and prompt is not None:
                derived = total - prompt - (thoughts or 0)
                output = derived if derived >= 0 else None
    except Exception:  # noqa: BLE001
        prompt = output = thoughts = None
    return AsrUsage(
        elapsed_s=elapsed_s,
        input_tokens=prompt,
        output_tokens=output,
        thinking_tokens=thoughts,
        finish_reason=finish_reason,
        transcript_chars=transcript_chars,
    )


class TranscriptionUnavailable(Exception):
    """Explicit evidence the transcript cannot be trusted: never a pass.

    ``reason`` is one of ``asr_error``, ``asr_timeout``, ``asr_empty``,
    ``asr_incomplete`` (finish reason other than STOP, or none) or
    ``asr_blocked`` (no candidates AND ``prompt_feedback.block_reason`` is in
    ``RETRYABLE_BLOCK_REASONS``, today only ``OTHER``). ``asr_blocked`` is the
    only reason the Gemini phase retries (the same audio bytes were blocked twice
    and then passed twice in the 2026-10-03 re-test); a response with no
    candidates and any other block reason (SAFETY, PROHIBITED_CONTENT, ...), or
    none, stays ``asr_empty``, with the reason named in the detail.

    ``usage`` is set for every raise after a response came back (``asr_blocked``,
    both ``asr_empty`` shapes, ``asr_incomplete``): the request was made and
    reported what it consumed, so callers can keep the token totals known
    (``my-podcasts-9p3.18``). It is ``None`` when there was no response
    (``asr_error``, ``asr_timeout``, no budget, client construction): truly
    unknown. Whether Google bills a blocked prompt is unconfirmed; this is usage,
    not an invoice.
    """

    def __init__(
        self, reason: str, message: str, usage: AsrUsage | None = None
    ) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.usage = usage


@dataclass(frozen=True)
class Transcription:
    text: str
    model: str
    prompt_version: str
    finish_reason: str
    elapsed_s: float
    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None
    # The transcriber's policy string (model, prompt, generation config). None =
    # unknown (a fake or an older caller); verify then assumes ASR_POLICY.
    policy: str | None = None


def _make_genai_client_unguarded(
    *, timeout_s: float, base_url: str | None = None
) -> genai.Client:
    options = {
        # 0 means "no timeout" to the SDK, so never round down to it.
        "timeout": max(1, math.ceil(timeout_s * 1000)),
        "retry_options": types.HttpRetryOptions(attempts=1),
    }
    if base_url is not None:
        options["base_url"] = base_url
    return genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=types.HttpOptions(**options),
    )


def _make_genai_client(timeout_s: float) -> genai.Client:
    return _make_genai_client_unguarded(timeout_s=timeout_s)


class GeminiTranscriber:
    """Callable ``(audio_bytes, mime_type) -> Transcription``. Client is lazy.

    Safe to share across threads (lazy init and close are locked); T3 may
    equally build one per call. Usable as a context manager (exit closes).
    """

    def __init__(
        self,
        *,
        model: str = ASR_MODEL,
        timeout_s: float = DEFAULT_ASR_TIMEOUT_SECONDS,
        thinking: str = DEFAULT_THINKING,
    ) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self.thinking = _check_thinking(thinking)
        self._client: genai.Client | None = None
        self._lock = threading.Lock()

    @property
    def policy(self) -> str:
        """ASR policy string for this instance's model and thinking setting."""
        return policy_for(self.model, self.thinking)

    def __enter__(self) -> GeminiTranscriber:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get_client(self) -> genai.Client:
        with self._lock:
            if self._client is None:
                try:
                    self._client = _make_genai_client(self.timeout_s)
                except (KeyError, ValueError) as exc:
                    # Missing GEMINI_API_KEY / bad client config. Deliberately
                    # not `Exception`: the test guard's AssertionError must
                    # propagate, not be mistaken for an outage.
                    raise TranscriptionUnavailable("asr_error", repr(exc)) from exc
            return self._client

    def __call__(self, audio: bytes, mime_type: str) -> Transcription:
        if self.timeout_s <= 0:
            raise TranscriptionUnavailable("asr_timeout", "no time budget left")
        client = self._get_client()
        started = time.monotonic()
        try:
            resp = client.models.generate_content(
                model=self.model,
                contents=[
                    types.Part.from_bytes(data=audio, mime_type=mime_type),
                    ASR_PROMPT,
                ],
                config=_generation_config(self.thinking),
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise TranscriptionUnavailable("asr_timeout", repr(exc)) from exc
        except Exception as exc:  # any SDK/transport failure = no transcript
            raise TranscriptionUnavailable("asr_error", repr(exc)) from exc
        usage = _read_usage(resp, time.monotonic() - started)

        if not resp.candidates:
            feedback = getattr(resp, "prompt_feedback", None)
            block = getattr(feedback, "block_reason", None)
            detail = "no candidates"
            if block is not None:
                detail += f" (block_reason={getattr(block, 'name', block)})"
            if _is_retryable_block(block):
                # Structural, not textual: the phase retries exactly this reason.
                raise TranscriptionUnavailable(ASR_BLOCKED, detail, usage)
            raise TranscriptionUnavailable("asr_empty", detail, usage)
        finish = resp.candidates[0].finish_reason
        finish_name = finish.name if finish is not None else "NONE"
        if finish_name != "STOP":
            raise TranscriptionUnavailable(
                "asr_incomplete",
                f"finish_reason={finish_name}",
                replace(usage, finish_reason=finish_name),
            )
        text = resp.text or ""
        if not normalize_tokens(text):
            # Not just blank: "..." or a lone dash carries no words either, and
            # must not become a segment that reads as "everything omitted".
            raise TranscriptionUnavailable(
                "asr_empty",
                "transcript has no word tokens",
                replace(usage, finish_reason=finish_name, transcript_chars=len(text)),
            )
        return Transcription(
            text=text,
            model=self.model,
            prompt_version=ASR_PROMPT_VERSION,
            finish_reason=finish_name,
            elapsed_s=usage.elapsed_s,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            thinking_tokens=usage.thinking_tokens,
            policy=self.policy,
        )

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()


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
