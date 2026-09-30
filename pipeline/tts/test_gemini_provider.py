from __future__ import annotations

import base64
import pickle
import random
import struct
import threading

import pytest
import requests

from pipeline.tts import providers
from pipeline.tts.config import GeminiConfig
from pipeline.tts.providers import GeminiProvider, Synthesis, TTSProviderError


CFG = GeminiConfig(
    model="gemini-3.8-flash-lite-tts", voice="Kore", style="calm, measured news anchor"
)
KEY = "sentinel-key-9f3a7c"
PCM = b"\x01\x00\x02\x00" * 6_000  # 6,000 frames = 0.25 s


def wav_bytes(
    pcm: bytes = PCM,
    *,
    declared: int | None = None,
    rate: int = 24_000,
    channels: int = 1,
    width: int = 2,
    c2pa: int = 0,
) -> bytes:
    """RIFF / 'fmt ' 16 / 'data' [/ trailing 'C2PA'], RIFF size covering it all.

    Mirrors the layout of a real Gemini response (T3-FACTS.md).
    """
    declared = len(pcm) if declared is None else declared
    fmt = struct.pack(
        "<HHIIHH",
        1,
        channels,
        rate,
        rate * channels * width,
        channels * width,
        width * 8,
    )
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data"
        + struct.pack("<I", declared)
        + pcm
    )
    if c2pa:
        body += b"C2PA" + struct.pack("<I", c2pa) + b"\0" * c2pa
    return b"RIFF" + struct.pack("<I", len(body)) + body


class FakeResponse:
    def __init__(self, status_code: int = 200, body=None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text or (repr(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def ok_body(
    wav: bytes | None = None,
    *,
    finish: str = "STOP",
    mime: str = "audio/wav",
    data: str | None = None,
    parts: list | None = None,
) -> dict:
    wav = wav_bytes() if wav is None else wav
    inline = {
        "inlineData": {
            "mimeType": mime,
            "data": base64.b64encode(wav).decode() if data is None else data,
        }
    }
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [inline] if parts is None else parts,
                },
                "finishReason": finish,
            }
        ],
        "usageMetadata": {"promptTokenCount": 513, "candidatesTokenCount": 5012},
    }


def error_body(code: int, status: str, message: str, details=None) -> dict:
    err = {"code": code, "message": message, "status": status}
    if details is not None:
        err["details"] = details
    return {"error": err}


class FakeSession:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict]] = []
        self.closed = 0

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed += 1


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)


def _provider(monkeypatch, *responses, **kw) -> tuple[GeminiProvider, FakeSession]:
    session = FakeSession(responses)
    monkeypatch.setattr(providers, "_make_gemini_session", lambda: session)
    return GeminiProvider(**kw), session


def _fail(monkeypatch, response, **kw) -> TTSProviderError:
    provider, _ = _provider(monkeypatch, response)
    with pytest.raises(TTSProviderError) as exc:
        provider.synthesize("Hello.", CFG, **kw)
    assert KEY not in str(exc.value)
    return exc.value


# --- request shape -----------------------------------------------------------


def test_request_shape_with_style(monkeypatch) -> None:
    provider, session = _provider(monkeypatch, FakeResponse(200, ok_body()))
    provider.synthesize("Hello there.", CFG)
    [(url, kwargs)] = session.calls
    assert url == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.8-flash-lite-tts:generateContent"
    )
    assert kwargs["headers"]["x-goog-api-key"] == KEY
    assert KEY not in url
    assert kwargs["json"] == {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": "Hello there.",
                        "speech_metadata": {"style": "calm, measured news anchor"},
                    }
                ],
            }
        ],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}
            },
        },
    }
    assert kwargs["timeout"] == (10.0, 90.0)


def test_request_omits_speech_metadata_without_style(monkeypatch) -> None:
    provider, session = _provider(monkeypatch, FakeResponse(200, ok_body()))
    provider.synthesize("Hi.", GeminiConfig("m", "Kore"))
    [(_, kwargs)] = session.calls
    assert kwargs["json"]["contents"][0]["parts"] == [{"text": "Hi."}]


def test_style_never_enters_the_text(monkeypatch) -> None:
    provider, session = _provider(monkeypatch, FakeResponse(200, ok_body()))
    provider.synthesize("Hi.", CFG)
    [(_, kwargs)] = session.calls
    assert "calm" not in kwargs["json"]["contents"][0]["parts"][0]["text"]


def test_timeout_override_and_short_budget(monkeypatch) -> None:
    provider, session = _provider(monkeypatch, FakeResponse(200, ok_body()))
    provider.synthesize("Hi.", CFG, timeout=4.0)
    assert session.calls[0][1]["timeout"] == (4.0, 4.0)
    provider2, session2 = _provider(
        monkeypatch, FakeResponse(200, ok_body()), timeout=30
    )
    provider2.synthesize("Hi.", CFG)
    assert session2.calls[0][1]["timeout"] == (10.0, 30)


# --- success -----------------------------------------------------------------


def test_success_returns_pcm_and_synthesis_details(monkeypatch) -> None:
    provider, _ = _provider(monkeypatch, FakeResponse(200, ok_body()))
    assert provider.synthesize("Hi.", CFG) == PCM
    provider, _ = _provider(monkeypatch, FakeResponse(200, ok_body()))
    syn = provider.synthesize_detailed("Hi.", CFG)
    assert isinstance(syn, Synthesis)
    assert syn.pcm == PCM
    assert syn.finish_reason == "STOP"
    assert syn.prompt_tokens == 513 and syn.audio_tokens == 5012
    assert syn.elapsed_s >= 0


def test_missing_usage_metadata_is_none(monkeypatch) -> None:
    body = ok_body()
    del body["usageMetadata"]
    provider, _ = _provider(monkeypatch, FakeResponse(200, body))
    syn = provider.synthesize_detailed("Hi.", CFG)
    assert syn.prompt_tokens is None and syn.audio_tokens is None


def test_trailing_c2pa_chunk_is_not_spliced_into_audio(monkeypatch) -> None:
    wav = wav_bytes(PCM, c2pa=6_016)
    assert len(wav) > 44 + len(PCM) + 6_000  # the trailer really is there
    provider, _ = _provider(monkeypatch, FakeResponse(200, ok_body(wav)))
    assert provider.synthesize("Hi.", CFG) == PCM


# --- classification: transport -----------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        requests.Timeout("slow"),
        requests.ConnectionError("reset"),
        requests.RequestException("other"),
    ],
)
def test_transport_errors_are_infra(monkeypatch, exc) -> None:
    assert _fail(monkeypatch, exc).kind == "infra"


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
def test_5xx_and_408_are_infra(monkeypatch, status) -> None:
    err = _fail(
        monkeypatch,
        FakeResponse(status, error_body(status, "UNAVAILABLE", "try later")),
    )
    assert err.kind == "infra"
    assert f"HTTP {status}" in str(err) and "UNAVAILABLE" in str(err)


# --- classification: 429 -----------------------------------------------------

_QUOTA = "type.googleapis.com/google.rpc.QuotaFailure"


def test_429_with_per_day_quota_is_fatal(monkeypatch) -> None:
    details = [
        {
            "@type": _QUOTA,
            "violations": [
                {
                    "quotaMetric": "generativelanguage.googleapis.com/requests",
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel",
                }
            ],
        }
    ]
    err = _fail(
        monkeypatch,
        FakeResponse(
            429, error_body(429, "RESOURCE_EXHAUSTED", "quota exceeded", details)
        ),
    )
    assert err.kind == "fatal" and not err.retryable


def test_429_with_daily_metric_is_fatal(monkeypatch) -> None:
    details = [
        {
            "@type": _QUOTA,
            "violations": [{"quotaMetric": "x/daily_tokens", "quotaId": "Other"}],
        }
    ]
    body = error_body(429, "RESOURCE_EXHAUSTED", "q", details)
    assert _fail(monkeypatch, FakeResponse(429, body)).kind == "fatal"


def test_429_with_only_retry_info_is_infra(monkeypatch) -> None:
    details = [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "25s"}
    ]
    err = _fail(
        monkeypatch,
        FakeResponse(429, error_body(429, "RESOURCE_EXHAUSTED", "slow down", details)),
    )
    assert err.kind == "infra"


def test_429_with_per_minute_quota_failure_is_infra(monkeypatch) -> None:
    details = [
        {
            "@type": _QUOTA,
            "violations": [{"quotaId": "GenerateRequestsPerMinutePerProject"}],
        }
    ]
    body = error_body(429, "RESOURCE_EXHAUSTED", "q", details)
    assert _fail(monkeypatch, FakeResponse(429, body)).kind == "infra"


def test_429_with_unparseable_body_is_infra(monkeypatch) -> None:
    assert _fail(monkeypatch, FakeResponse(429, None, text="<html>")).kind == "infra"


# --- classification: 4xx ------------------------------------------------------


def test_400_api_key_invalid_is_fatal_and_reports_reason(monkeypatch) -> None:
    body = error_body(
        400,
        "INVALID_ARGUMENT",
        "API key not valid. Please pass a valid API key.",
        [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "API_KEY_INVALID",
                "domain": "googleapis.com",
            }
        ],
    )
    err = _fail(monkeypatch, FakeResponse(400, body))
    assert err.kind == "fatal"
    assert "HTTP 400" in str(err) and "INVALID_ARGUMENT" in str(err)
    assert "API_KEY_INVALID" in str(err)


def test_400_unknown_voice_is_fatal(monkeypatch) -> None:
    body = error_body(400, "INVALID_ARGUMENT", "No matching speaker voice found")
    err = _fail(monkeypatch, FakeResponse(400, body))
    assert err.kind == "fatal" and "No matching speaker voice" in str(err)


@pytest.mark.parametrize("status", [401, 403, 404, 410, 422])
def test_other_4xx_are_fatal(monkeypatch, status) -> None:
    body = error_body(status, "PERMISSION_DENIED", "nope")
    assert _fail(monkeypatch, FakeResponse(status, body)).kind == "fatal"


def test_error_message_is_truncated_to_300_chars(monkeypatch) -> None:
    body = error_body(400, "INVALID_ARGUMENT", "x" * 5_000)
    msg = str(_fail(monkeypatch, FakeResponse(400, body)))
    assert "x" * 300 in msg and "x" * 301 not in msg


def test_non_json_error_body_still_classifies(monkeypatch) -> None:
    err = _fail(monkeypatch, FakeResponse(403, None, text="Forbidden"))
    assert err.kind == "fatal" and "HTTP 403" in str(err)


def test_unexpected_non_error_status_is_infra(monkeypatch) -> None:
    assert _fail(monkeypatch, FakeResponse(204, None)).kind == "infra"


# --- classification: 200 bodies ----------------------------------------------


def test_200_non_json_is_infra(monkeypatch) -> None:
    assert _fail(monkeypatch, FakeResponse(200, None, text="<html>")).kind == "infra"


def test_200_json_that_is_not_an_object_is_infra(monkeypatch) -> None:
    assert _fail(monkeypatch, FakeResponse(200, ["x"])).kind == "infra"


def test_200_without_candidates_is_content_and_reports_block_reason(
    monkeypatch,
) -> None:
    err = _fail(
        monkeypatch, FakeResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}})
    )
    assert err.kind == "content" and "SAFETY" in str(err)


def test_finish_reason_other_with_no_content_is_content(monkeypatch) -> None:
    body = {"candidates": [{"finishReason": "OTHER"}]}
    err = _fail(monkeypatch, FakeResponse(200, body))
    assert err.kind == "content" and "OTHER" in str(err)


def test_stop_with_zero_inline_parts_is_content(monkeypatch) -> None:
    body = ok_body(parts=[{"text": "no audio here"}])
    assert _fail(monkeypatch, FakeResponse(200, body)).kind == "content"


def test_stop_with_multiple_inline_parts_is_fatal(monkeypatch) -> None:
    part = {"inlineData": {"mimeType": "audio/wav", "data": "AAAA"}}
    body = ok_body(parts=[part, part])
    assert _fail(monkeypatch, FakeResponse(200, body)).kind == "fatal"


@pytest.mark.parametrize("mime", ["audio/mpeg", "audio/L16;rate=24000", "video/wav"])
def test_non_wav_mime_is_fatal(monkeypatch, mime) -> None:
    assert _fail(monkeypatch, FakeResponse(200, ok_body(mime=mime))).kind == "fatal"


def test_wav_mime_is_case_insensitive_and_allows_parameters(monkeypatch) -> None:
    provider, _ = _provider(
        monkeypatch, FakeResponse(200, ok_body(mime="Audio/WAV; codecs=1"))
    )
    assert provider.synthesize("Hi.", CFG) == PCM


def test_invalid_base64_is_infra(monkeypatch) -> None:
    err = _fail(monkeypatch, FakeResponse(200, ok_body(data="not base64!!")))
    assert err.kind == "infra"


def test_non_wav_bytes_are_infra(monkeypatch) -> None:
    data = base64.b64encode(b"this is not a wav file at all").decode()
    assert _fail(monkeypatch, FakeResponse(200, ok_body(data=data))).kind == "infra"


def test_zero_frames_is_infra(monkeypatch) -> None:
    wav = wav_bytes(b"")
    assert _fail(monkeypatch, FakeResponse(200, ok_body(wav))).kind == "infra"


def test_truncated_data_is_infra(monkeypatch) -> None:
    wav = wav_bytes(PCM[:-1000], declared=len(PCM))
    err = _fail(monkeypatch, FakeResponse(200, ok_body(wav)))
    assert err.kind == "infra"


def test_44_1_khz_is_fatal(monkeypatch) -> None:
    wav = wav_bytes(rate=44_100)
    assert _fail(monkeypatch, FakeResponse(200, ok_body(wav))).kind == "fatal"


def test_stereo_is_fatal(monkeypatch) -> None:
    wav = wav_bytes(PCM, channels=2)
    assert _fail(monkeypatch, FakeResponse(200, ok_body(wav))).kind == "fatal"


def test_8_bit_samples_are_fatal(monkeypatch) -> None:
    wav = wav_bytes(PCM, width=1)
    assert _fail(monkeypatch, FakeResponse(200, ok_body(wav))).kind == "fatal"


# --- key handling ---------------------------------------------------------------


@pytest.mark.parametrize("value", [None, ""])
def test_missing_or_empty_key_is_fatal_and_builds_no_session(
    monkeypatch, value
) -> None:
    if value is None:
        monkeypatch.delenv("GEMINI_API_KEY")
    else:
        monkeypatch.setenv("GEMINI_API_KEY", value)
    made: list[int] = []
    monkeypatch.setattr(providers, "_make_gemini_session", lambda: made.append(1))
    with pytest.raises(TTSProviderError, match="GEMINI_API_KEY is not set") as exc:
        GeminiProvider().synthesize("Hi.", CFG)
    assert exc.value.kind == "fatal"
    assert made == []


def test_key_is_read_per_call(monkeypatch) -> None:
    provider, session = _provider(monkeypatch, FakeResponse(200, ok_body()))
    provider.synthesize("a", CFG)
    monkeypatch.setenv("GEMINI_API_KEY", "rotated")
    provider.synthesize("b", CFG)
    assert [c[1]["headers"]["x-goog-api-key"] for c in session.calls] == [
        KEY,
        "rotated",
    ]


def test_key_never_appears_in_any_error_message(monkeypatch) -> None:
    # Even a server (or transport error) that echoes the key back is scrubbed.
    echo = f"bad key {KEY}"
    cases = [
        FakeResponse(400, error_body(400, "INVALID_ARGUMENT", echo)),
        FakeResponse(503, error_body(503, "UNAVAILABLE", echo)),
        FakeResponse(403, None, text=echo),
        requests.ConnectionError(echo),
        FakeResponse(200, None, text=echo),
        FakeResponse(200, {"promptFeedback": {"blockReason": echo}}),
    ]
    for case in cases:
        provider, _ = _provider(monkeypatch, case)
        with pytest.raises(TTSProviderError) as exc:
            provider.synthesize("Hi.", CFG)
        assert KEY not in str(exc.value), case


def test_no_time_budget_is_infra_without_a_request(monkeypatch) -> None:
    made: list[int] = []
    monkeypatch.setattr(providers, "_make_gemini_session", lambda: made.append(1))
    for t in (0, -1.5):
        with pytest.raises(TTSProviderError, match="no time budget left") as exc:
            GeminiProvider().synthesize("Hi.", CFG, timeout=t)
        assert exc.value.kind == "infra"
    assert made == []


# --- sessions --------------------------------------------------------------------


def test_one_session_per_thread_and_close_closes_all(monkeypatch) -> None:
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        s = FakeSession([FakeResponse(200, ok_body())])
        sessions.append(s)
        return s

    monkeypatch.setattr(providers, "_make_gemini_session", factory)
    provider = GeminiProvider()
    provider.synthesize("main thread, call 1", CFG)
    provider.synthesize("main thread, call 2", CFG)  # reuses its session
    assert len(sessions) == 1

    results: list[bytes] = []
    t = threading.Thread(target=lambda: results.append(provider.synthesize("t", CFG)))
    t.start()
    t.join()
    assert results == [PCM] and len(sessions) == 2

    provider.close()
    assert [s.closed for s in sessions] == [1, 1]
    provider.close()  # idempotent
    assert [s.closed for s in sessions] == [1, 1]


def test_two_concurrent_threads_get_distinct_sessions(monkeypatch) -> None:
    sessions: list[FakeSession] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    class BarrierSession(FakeSession):
        def post(self, url, **kwargs):
            barrier.wait(timeout=5)  # both requests are in flight together
            return super().post(url, **kwargs)

    def factory() -> FakeSession:
        with lock:
            s = BarrierSession([FakeResponse(200, ok_body())])
            sessions.append(s)
            return s

    monkeypatch.setattr(providers, "_make_gemini_session", factory)
    provider = GeminiProvider()
    threads = [
        threading.Thread(target=provider.synthesize, args=("x", CFG)) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sessions) == 2 and all(len(s.calls) == 1 for s in sessions)
    provider.close()
    assert [s.closed for s in sessions] == [1, 1]


def test_synthesize_after_close_builds_a_fresh_session(monkeypatch) -> None:
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        s = FakeSession([FakeResponse(200, ok_body())])
        sessions.append(s)
        return s

    monkeypatch.setattr(providers, "_make_gemini_session", factory)
    provider = GeminiProvider()
    provider.synthesize("a", CFG)
    provider.close()
    provider.synthesize("b", CFG)
    assert len(sessions) == 2 and sessions[0].closed == 1 and sessions[1].closed == 0


def test_context_manager_closes_sessions(monkeypatch) -> None:
    provider, session = _provider(monkeypatch, FakeResponse(200, ok_body()))
    with provider:
        provider.synthesize("a", CFG)
    assert session.closed == 1


def test_max_chars_is_3000() -> None:
    assert GeminiProvider.max_chars == 3000


def test_unguarded_session_is_a_plain_session_without_adapter_retries() -> None:
    session = providers._make_gemini_session_unguarded()
    try:
        assert isinstance(session, requests.Session)
        assert session.get_adapter("https://x").max_retries.total == 0
    finally:
        session.close()


def test_autouse_guard_blocks_real_gemini_session() -> None:
    with pytest.raises(AssertionError, match="real Gemini"):
        providers._make_gemini_session()


# --- I1: malformed response shapes never escape as non-TTSProviderError -----------


def _outcome(monkeypatch, response) -> TTSProviderError | bytes:
    provider, _ = _provider(monkeypatch, response)
    try:
        return provider.synthesize("Hi.", CFG)
    except TTSProviderError as exc:
        assert KEY not in str(exc)
        return exc


def _with_candidate(**over) -> dict:
    body = ok_body()
    body["candidates"][0].update(over)
    return body


_MALFORMED_200 = [
    {"candidates": {"a": 1}},
    {"candidates": "x"},
    {"candidates": ["x"]},
    {"candidates": [5]},
    {"candidates": [{}]},
    {"promptFeedback": "x"},
    {"promptFeedback": 5},
    _with_candidate(content="x"),
    _with_candidate(content=["x"]),
    _with_candidate(content={"parts": {"inlineData": {}}}),
    _with_candidate(content={"parts": "inlineData"}),
    _with_candidate(content={"parts": [5, "x", None]}),
    _with_candidate(content={"parts": [{"inlineData": "x"}]}),
    _with_candidate(content={"parts": [{"inlineData": 5}]}),
    _with_candidate(
        content={"parts": [{"inlineData": {"mimeType": "audio/wav", "data": 5}}]}
    ),
    _with_candidate(
        content={"parts": [{"inlineData": {"mimeType": 5, "data": "AAAA"}}]}
    ),
    {**ok_body(), "usageMetadata": "x"},
    {
        **ok_body(),
        "usageMetadata": {"promptTokenCount": "5", "candidatesTokenCount": []},
    },
]

_MALFORMED_ERRORS = [
    {"error": "boom"},
    {"error": ["boom"]},
    {"error": 5},
    {"error": {"details": 5}},
    {"error": {"details": "x"}},
    {"error": {"details": {"reason": "X"}}},
    {"error": {"details": [5, "x", None]}},
    {"error": {"details": [{"@type": _QUOTA, "violations": 5}]}},
    {"error": {"details": [{"@type": _QUOTA, "violations": "x"}]}},
    {"error": {"details": [{"@type": _QUOTA, "violations": {"quotaId": "PerDay"}}]}},
    {"error": {"details": [{"@type": _QUOTA, "violations": [5, "x", None]}]}},
    {"error": {"status": 5, "message": ["x"], "details": [{"reason": 5}]}},
    [1, 2],
    "string body",
    5,
]


@pytest.mark.parametrize("body", _MALFORMED_200)
def test_malformed_200_bodies_only_raise_provider_errors(monkeypatch, body) -> None:
    out = _outcome(monkeypatch, FakeResponse(200, body))
    assert isinstance(out, (TTSProviderError, bytes))


@pytest.mark.parametrize("status", [400, 429, 500, 403])
@pytest.mark.parametrize("body", _MALFORMED_ERRORS)
def test_malformed_error_bodies_only_raise_provider_errors(
    monkeypatch, status, body
) -> None:
    out = _outcome(monkeypatch, FakeResponse(status, body))
    assert isinstance(out, TTSProviderError)
    assert out.kind == {400: "fatal", 403: "fatal", 500: "infra", 429: "infra"}[status]


# --- I2: corrupt WAV bytes ---------------------------------------------------------


def test_corrupt_wav_fuzz_only_raises_provider_errors(monkeypatch) -> None:
    rng = random.Random(20260930)
    base = wav_bytes(PCM[:2_000], c2pa=64)
    provider, session = _provider(monkeypatch, FakeResponse(200, ok_body(base)))
    kinds: set[str] = set()
    for _ in range(3_000):
        raw = bytearray(base)
        for _ in range(rng.randint(1, 4)):
            # Bias toward the header, where wave's parser lives.
            pos = rng.randrange(60 if rng.random() < 0.8 else len(raw))
            raw[pos] = rng.randrange(256)
        mode = rng.random()
        if mode < 0.2:
            raw = raw[: rng.randrange(len(raw))]
        elif mode < 0.3:
            raw[rng.randrange(len(raw)) : 0] = bytes(
                rng.randrange(256) for _ in range(5)
            )
        session.responses = [FakeResponse(200, ok_body(bytes(raw)))]
        try:
            provider.synthesize("Hi.", CFG)
        except TTSProviderError as exc:
            kinds.add(exc.kind)
    assert {"infra"} <= kinds  # the fuzz actually reached the WAV classifier


def test_chunk_seek_runtime_error_is_infra(monkeypatch) -> None:
    # wave.Chunk raises RuntimeError (not wave.Error) on some corrupt layouts.
    def boom(*a, **k):
        raise RuntimeError("seek on closed chunk")

    monkeypatch.setattr(providers.wave, "open", boom)
    assert _fail(monkeypatch, FakeResponse(200, ok_body())).kind == "infra"


def test_struct_error_in_wave_is_infra(monkeypatch) -> None:
    def boom(*a, **k):
        raise struct.error("unpack requires a buffer")

    monkeypatch.setattr(providers.wave, "open", boom)
    assert _fail(monkeypatch, FakeResponse(200, ok_body())).kind == "infra"


# --- I3: key handling ----------------------------------------------------------------


def test_key_is_stripped_before_use(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", f"  {KEY}\n")
    provider, session = _provider(monkeypatch, FakeResponse(200, ok_body()))
    provider.synthesize("Hi.", CFG)
    value = session.calls[0][1]["headers"]["x-goog-api-key"]
    assert value == KEY and "\n" not in value


@pytest.mark.parametrize("value", [" ", "\n", " \t\n"])
def test_whitespace_only_key_is_not_set(monkeypatch, value) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", value)
    made: list[int] = []
    monkeypatch.setattr(providers, "_make_gemini_session", lambda: made.append(1))
    with pytest.raises(TTSProviderError, match="GEMINI_API_KEY is not set") as exc:
        GeminiProvider().synthesize("Hi.", CFG)
    assert exc.value.kind == "fatal" and made == []


@pytest.mark.parametrize("bad", ["sécret-key", "ke\ny-inside", "k\u200bey"])
def test_key_with_invalid_characters_is_fatal_and_never_echoed(
    monkeypatch, bad
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", bad)
    made: list[int] = []
    monkeypatch.setattr(providers, "_make_gemini_session", lambda: made.append(1))
    with pytest.raises(TTSProviderError) as exc:
        GeminiProvider().synthesize("Hi.", CFG)
    assert exc.value.kind == "fatal" and made == []
    assert "GEMINI_API_KEY has invalid characters" in str(exc.value)
    assert bad.strip() not in str(exc.value)


def test_redirects_are_not_followed_and_3xx_is_infra(monkeypatch) -> None:
    provider, session = _provider(monkeypatch, FakeResponse(302, None, text="moved"))
    with pytest.raises(TTSProviderError) as exc:
        provider.synthesize("Hi.", CFG)
    assert exc.value.kind == "infra"
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.InvalidHeader("bad header"),
        requests.exceptions.InvalidURL("bad url"),
        requests.exceptions.InvalidSchema("bad schema"),
        requests.exceptions.MissingSchema("no schema"),
    ],
)
def test_request_construction_errors_are_fatal(monkeypatch, exc) -> None:
    assert _fail(monkeypatch, exc).kind == "fatal"


def test_409_is_infra(monkeypatch) -> None:
    body = error_body(409, "ABORTED", "conflict")
    assert _fail(monkeypatch, FakeResponse(409, body)).kind == "infra"


# --- S1/S2: no key-bearing cause chain; picklable errors ---------------------------


def test_transport_errors_do_not_chain_the_original_exception(monkeypatch) -> None:
    prepared = requests.Request(
        "POST", "https://x.example/", headers={"x-goog-api-key": KEY}
    ).prepare()
    for exc in (
        requests.ConnectionError("reset", request=prepared),
        requests.exceptions.InvalidHeader("bad", request=prepared),
    ):
        err = _fail(monkeypatch, exc)
        assert err.__cause__ is None and err.__context__ is None


@pytest.mark.parametrize("kind", ["content", "infra", "fatal"])
def test_provider_error_survives_pickle(kind) -> None:
    err = pickle.loads(pickle.dumps(TTSProviderError("boom: détail", kind=kind)))
    assert isinstance(err, TTSProviderError)
    assert str(err) == "boom: détail" and err.kind == kind
    assert err.retryable is (kind != "fatal")
