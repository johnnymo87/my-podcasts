"""TTS providers. Each returns raw 24 kHz mono s16le PCM for one chunk."""

from __future__ import annotations

import base64
import binascii
import io
import os
import re
import threading
import time
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import openai
import requests


if TYPE_CHECKING:
    from pipeline.tts.config import GeminiConfig, OpenAIConfig

_RETRYABLE_STATUS = frozenset({408, 409, 429})


ErrorKind = Literal["content", "infra", "fatal"]
_KINDS = frozenset({"content", "infra", "fatal"})


class TTSProviderError(Exception):
    """A provider could not produce audio for one chunk.

    Give exactly one of ``retryable`` (legacy, OpenAI call sites) or ``kind``.
    ``retryable=True`` means ``kind="infra"``, ``False`` means ``"fatal"``.
    ``retryable`` is always derived: everything but ``fatal`` may be retried.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool | None = None,
        kind: ErrorKind | None = None,
    ) -> None:
        super().__init__(message)
        if (retryable is None) == (kind is None):
            raise ValueError("give exactly one of retryable= or kind=")
        if kind is None:
            kind = "infra" if retryable else "fatal"
        if kind not in _KINDS:
            raise ValueError(f"unknown TTS error kind: {kind!r}")
        self.kind: ErrorKind = kind

    @property
    def retryable(self) -> bool:
        return self.kind != "fatal"

    def __reduce__(self):
        # The keyword-only constructor defeats default Exception pickling; T3b
        # sends these across a process boundary.
        return (_rebuild_provider_error, (str(self), self.kind))


def _rebuild_provider_error(message: str, kind: ErrorKind) -> TTSProviderError:
    return TTSProviderError(message, kind=kind)


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

    def synthesize(self, text: str, cfg: OpenAIConfig) -> bytes:
        if self._client is None:
            self._client = _make_openai_client(self._timeout)
        try:
            response = self._client.audio.speech.create(
                model=cfg.model,
                voice=cfg.voice,
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


# --- Gemini (REST) ----------------------------------------------------------

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_TIMEOUT_SECONDS = 90.0

_GEMINI_RATE = 24_000
_GEMINI_CHANNELS = 1
_GEMINI_SAMPLE_WIDTH = 2
_QUOTA_FAILURE = "type.googleapis.com/google.rpc.QuotaFailure"
# A 429 is fatal only when it names a nonrenewable (daily) quota. Anything
# ambiguous is treated as a bounded transient: the caller's budget caps retries.
_DAILY_QUOTA = re.compile(r"(?i)per.?day|daily")
_MAX_DETAIL_CHARS = 300


@dataclass(frozen=True)
class Synthesis:
    pcm: bytes
    finish_reason: str
    prompt_tokens: int | None
    audio_tokens: int | None  # usageMetadata.candidatesTokenCount
    elapsed_s: float


def _make_gemini_session_unguarded() -> requests.Session:
    # A plain Session mounts adapters with max_retries=0: the renderer owns
    # retries, so hidden adapter retries cannot multiply a render's wall time.
    return requests.Session()


def _make_gemini_session() -> requests.Session:
    return _make_gemini_session_unguarded()


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _error_detail(resp: Any) -> str:
    """HTTP status, ``error.status``, truncated ``error.message``, ErrorInfo reason."""
    parts = [f"HTTP {resp.status_code}"]
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 -- a non-JSON error body is still an error
        body = None
    err = _as_dict(_as_dict(body).get("error"))
    if err:
        if err.get("status"):
            parts.append(str(err["status"]))
        if err.get("message"):
            parts.append(str(err["message"])[:_MAX_DETAIL_CHARS])
        for d in _as_list(err.get("details")):
            reason = _as_dict(d).get("reason")
            if reason:
                parts.append(f"reason={reason}")
                break
    else:
        text = getattr(resp, "text", "") or ""
        if text:
            parts.append(text[:_MAX_DETAIL_CHARS])
    return ": ".join(parts)


def _is_daily_quota_429(resp: Any) -> bool:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return False
    for d in _as_list(_as_dict(_as_dict(body).get("error")).get("details")):
        d = _as_dict(d)
        if d.get("@type") != _QUOTA_FAILURE:
            continue
        for v in _as_list(d.get("violations")):
            v = _as_dict(v)
            if any(
                _DAILY_QUOTA.search(str(v.get(field, "")))
                for field in ("quotaId", "quotaMetric")
            ):
                return True
    return False


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class GeminiProvider:
    """Renders one chunk of text to PCM via the Gemini REST API.

    Thread-safe: each thread gets its own ``requests.Session`` (sessions are not
    documented as thread-safe), and ``close()`` closes every session created.
    Every failure is a ``TTSProviderError`` carrying a ``kind`` (see the plan's
    classification table). The API key is read from ``GEMINI_API_KEY`` on each
    call, sent only in a header, and scrubbed from every raised message.

    Bounds: ``timeout`` is per-operation (connect, and each read gap), NOT a
    wall-clock deadline, so a slowly-trickling response can outlive it; the
    caller's process-level kill (T3b) is the real bound. ``close()`` closes the
    sessions but does not abort a request already in flight on another thread.
    """

    # An application limit, not a documented vendor limit (a real 2399-char
    # chunk took 33 s and made 156.6 s of audio). Changing it moves chunk
    # boundaries: bump ``cache.RENDERER_VERSION``.
    max_chars = 3000

    def __init__(self, *, timeout: float = DEFAULT_GEMINI_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        self._local = threading.local()
        self._lock = threading.Lock()
        self._sessions: list[requests.Session] = []
        self._epoch = 0  # bumped by close(): a thread's cached session is then stale

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None or self._local.epoch != self._epoch:
            session = _make_gemini_session()
            with self._lock:
                self._sessions.append(session)
                self._local.epoch = self._epoch
            self._local.session = session
        return session

    def close(self) -> None:
        with self._lock:
            sessions, self._sessions = self._sessions, []
            self._epoch += 1
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001 -- closing must never raise
                pass

    def __enter__(self) -> GeminiProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def synthesize(
        self, text: str, cfg: GeminiConfig, *, timeout: float | None = None
    ) -> bytes:
        return self.synthesize_detailed(text, cfg, timeout=timeout).pcm

    def synthesize_detailed(
        self, text: str, cfg: GeminiConfig, *, timeout: float | None = None
    ) -> Synthesis:
        t = timeout if timeout is not None else self._timeout
        if t <= 0:
            raise TTSProviderError("Gemini: no time budget left", kind="infra")
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise TTSProviderError("GEMINI_API_KEY is not set", kind="fatal")
        if not (key.isascii() and key.isprintable()):
            # A header value with control/non-ASCII characters cannot be sent;
            # say so without echoing any of it.
            raise TTSProviderError(
                "GEMINI_API_KEY has invalid characters", kind="fatal"
            )

        def fail(kind: ErrorKind, message: str) -> TTSProviderError:
            return TTSProviderError(message.replace(key, "[redacted]"), kind=kind)

        part: dict[str, Any] = {"text": text}
        if cfg.style:
            # Never in ``text``: a style preamble there is read aloud.
            part["speech_metadata"] = {"style": cfg.style}
        body = {
            "contents": [{"role": "user", "parts": [part]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": cfg.voice}}
                },
            },
        }
        started = time.monotonic()
        transport_error: TTSProviderError | None = None
        try:
            resp = self._session().post(
                f"{GEMINI_API_BASE}/models/{cfg.model}:generateContent",
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json=body,
                timeout=(min(10.0, t), t),
                allow_redirects=False,  # a redirect must not carry the key elsewhere
            )
        except (
            requests.exceptions.InvalidHeader,
            requests.exceptions.InvalidURL,
            requests.exceptions.InvalidSchema,
            requests.exceptions.MissingSchema,
        ) as exc:
            # Our request is malformed: retrying the same request cannot help.
            transport_error = fail(
                "fatal", f"Gemini request invalid: {type(exc).__name__}: {exc}"
            )
        except requests.RequestException as exc:
            transport_error = fail(
                "infra", f"Gemini request failed: {type(exc).__name__}: {exc}"
            )
        if transport_error is not None:
            # Raised outside the ``except`` block, so neither __cause__ nor
            # __context__ carries the requests exception (whose ``.request``
            # holds the key header).
            raise transport_error
        elapsed = time.monotonic() - started

        status = resp.status_code
        if status != 200:
            detail = _error_detail(resp)
            if status >= 500 or status in (408, 409):
                raise fail("infra", f"Gemini {detail}")
            if status == 429:
                if _is_daily_quota_429(resp):
                    raise fail("fatal", f"Gemini {detail} (daily quota exhausted)")
                raise fail("infra", f"Gemini {detail}")
            if 400 <= status < 500:
                raise fail("fatal", f"Gemini {detail}")
            raise fail("infra", f"Gemini unexpected {detail}")

        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise fail("infra", "Gemini 200 with a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise fail("infra", "Gemini 200 with a non-object JSON body")

        candidates = _as_list(payload.get("candidates"))
        if not candidates:
            block = _as_dict(payload.get("promptFeedback")).get("blockReason")
            raise fail(
                "content",
                "Gemini returned no candidates"
                + (f" (blockReason={block})" if block else ""),
            )
        cand = _as_dict(candidates[0])
        finish = cand.get("finishReason")
        if finish != "STOP":
            raise fail("content", f"Gemini finishReason={finish!r}, no usable audio")
        parts = _as_list(_as_dict(cand.get("content")).get("parts"))
        inline = [
            _as_dict(p["inlineData"])
            for p in parts
            if isinstance(p, dict) and isinstance(p.get("inlineData"), dict)
        ]
        if not inline:
            raise fail("content", "Gemini STOP with no inlineData part")
        if len(inline) > 1:
            raise fail("fatal", f"Gemini STOP with {len(inline)} inlineData parts")
        mime = str(inline[0].get("mimeType", ""))
        if not mime.lower().startswith("audio/wav"):
            raise fail("fatal", f"Gemini audio mimeType {mime!r}, expected audio/wav")

        pcm = self._decode_wav(inline[0].get("data"), fail)
        usage = _as_dict(payload.get("usageMetadata"))
        return Synthesis(
            pcm=pcm,
            finish_reason=finish,
            prompt_tokens=_opt_int(usage.get("promptTokenCount")),
            audio_tokens=_opt_int(usage.get("candidatesTokenCount")),
            elapsed_s=elapsed,
        )

    @staticmethod
    def _decode_wav(data: object, fail) -> bytes:
        """Exactly the declared PCM frames of a 24 kHz mono s16 WAV.

        Parsed with ``wave``, never by stripping a fixed header: real responses
        carry a trailing ``C2PA`` chunk after ``data`` that a slice-to-end would
        splice into the audio as noise.
        """
        if not isinstance(data, str):
            raise fail("infra", "Gemini inlineData has no base64 data")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise fail("infra", "Gemini audio is not valid base64") from exc
        # Broad on purpose, and around the parser only: wave/chunk raise
        # RuntimeError, struct.error, EOFError, ... on corrupt bytes, and a
        # corrupt transfer is infra whatever the parser called it. Our own
        # format/frame checks below are outside the try so they are not swallowed.
        try:
            with wave.open(io.BytesIO(raw), "rb") as w:
                rate, channels, width = (
                    w.getframerate(),
                    w.getnchannels(),
                    w.getsampwidth(),
                )
                frames = w.getnframes()
                format_ok = (rate, channels, width) == (
                    _GEMINI_RATE,
                    _GEMINI_CHANNELS,
                    _GEMINI_SAMPLE_WIDTH,
                )
                pcm = w.readframes(frames) if format_ok else b""
        except Exception as exc:  # noqa: BLE001
            raise fail(
                "infra",
                f"Gemini audio is not a readable WAV: {type(exc).__name__}: {exc}",
            ) from exc
        if not format_ok:
            raise fail(
                "fatal",
                f"Gemini WAV is {rate} Hz/{channels} ch/{width * 8}-bit, "
                "expected 24000 Hz/1 ch/16-bit",
            )
        if frames == 0:
            raise fail("infra", "Gemini WAV has zero frames")
        if len(pcm) != frames * width:
            raise fail(
                "infra",
                f"Gemini WAV truncated: {len(pcm)} of {frames * width} bytes",
            )
        return pcm
