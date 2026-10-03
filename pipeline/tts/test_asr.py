import io
import json
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from google.genai import types

from pipeline.tts import asr
from pipeline.tts.asr import (
    ASR_MODEL,
    ASR_PROMPT,
    GeminiTranscriber,
    TranscriptionUnavailable,
    pcm_to_wav,
)


def response(text="hello world", finish="STOP", candidates=True):
    cands = []
    if candidates:
        parts = [types.Part(text=text)] if text is not None else []
        cands = [
            types.Candidate(
                finish_reason=getattr(types.FinishReason, finish) if finish else None,
                content=types.Content(parts=parts),
            )
        ]
    return types.GenerateContentResponse(
        candidates=cands,
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=5
        ),
    )


class FakeModels:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result):
        self.models = FakeModels(result)
        self.closed = False

    def close(self):
        self.closed = True


def transcriber_with(result):
    client = FakeClient(result)
    t = GeminiTranscriber(timeout_s=30)
    patcher = patch.object(asr, "_make_genai_client", lambda timeout_s: client)
    return t, client, patcher


def test_success_returns_text_and_metadata():
    t, client, p = transcriber_with(response("hello world"))
    with p:
        out = t(b"RIFF...", "audio/wav")
    assert out.text == "hello world"
    assert out.finish_reason == "STOP"
    assert out.model == ASR_MODEL
    assert out.input_tokens == 100 and out.output_tokens == 5
    call = client.models.calls[0]
    assert call["model"] == ASR_MODEL
    # Audio only: exactly one audio part plus the fixed instruction, nothing else.
    contents = call["contents"]
    assert len(contents) == 2 and contents[1] == ASR_PROMPT
    assert contents[0].inline_data.mime_type == "audio/wav"
    assert call["config"].temperature == 0


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (response(finish="MAX_TOKENS"), "asr_incomplete"),
        (response(finish="SAFETY"), "asr_incomplete"),
        (response(finish="OTHER", text=None), "asr_incomplete"),
        (response(finish=None), "asr_incomplete"),
        (response(text="   "), "asr_empty"),
        (response(text="..."), "asr_empty"),  # punctuation only: no word tokens
        (response(text="— … !!"), "asr_empty"),
        (response(candidates=False), "asr_empty"),
        (RuntimeError("boom"), "asr_error"),
        (httpx.ReadTimeout("slow"), "asr_timeout"),
    ],
)
def test_unavailable_cases(result, reason):
    t, _, p = transcriber_with(result)
    with p, pytest.raises(TranscriptionUnavailable) as exc_info:
        t(b"x", "audio/wav")
    assert exc_info.value.reason == reason


def test_client_is_lazy_reused_and_closed():
    made = []

    def make(timeout_s):
        made.append(timeout_s)
        return FakeClient(response())

    t = GeminiTranscriber(timeout_s=42)
    with patch.object(asr, "_make_genai_client", make):
        assert made == []
        t(b"x", "audio/wav")
        t(b"x", "audio/wav")
        client = t._client
        t.close()
    assert made == [42]
    assert client.closed


def test_real_client_has_sdk_retries_off_and_timeout(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    with patch.object(asr.genai, "Client", fake_client):
        asr._make_genai_client_unguarded(timeout_s=90)
    opts = captured["http_options"]
    assert opts.timeout == 90_000
    assert opts.retry_options.attempts == 1
    assert captured["api_key"] == "k"


def test_guard_blocks_real_client_in_tests(_guard_violations):
    with pytest.raises(AssertionError, match="real Gemini"):
        asr._make_genai_client(timeout_s=1)
    assert len(_guard_violations) == 1
    _guard_violations.clear()  # provoked on purpose


def test_pcm_to_wav_roundtrip():
    pcm = b"\x01\x00\xff\x7f" * 100
    with wave.open(io.BytesIO(pcm_to_wav(pcm))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24_000)
        assert w.readframes(w.getnframes()) == pcm


def test_pcm_to_wav_rejects_odd_length():
    with pytest.raises(ValueError):
        pcm_to_wav(b"\x00\x00\x00")


def test_zero_budget_is_timeout_and_never_builds_a_client():
    made = []

    def make(timeout_s):
        made.append(timeout_s)
        return FakeClient(response())

    t = GeminiTranscriber(timeout_s=0)
    with patch.object(asr, "_make_genai_client", make):
        with pytest.raises(TranscriptionUnavailable) as exc_info:
            t(b"x", "audio/wav")
    assert exc_info.value.reason == "asr_timeout"
    assert made == []


def test_tiny_budget_rounds_up_to_one_millisecond(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    with patch.object(asr.genai, "Client", fake_client):
        asr._make_genai_client_unguarded(timeout_s=0.0004)
    # 0 would mean "no timeout" to the SDK.
    assert captured["http_options"].timeout == 1


def test_context_manager_closes_and_double_close_is_harmless():
    client = FakeClient(response())
    with patch.object(asr, "_make_genai_client", lambda timeout_s: client):
        with GeminiTranscriber(timeout_s=30) as t:
            t(b"x", "audio/wav")
        assert client.closed
        t.close()
        t.close()


def test_lazy_init_under_threads_builds_exactly_one_client():
    made = []
    barrier = threading.Barrier(4)

    def make(timeout_s):
        time.sleep(0.05)
        made.append(1)
        return FakeClient(response())

    t = GeminiTranscriber(timeout_s=30)
    errors = []

    def worker():
        barrier.wait()
        try:
            t(b"x", "audio/wav")
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    with patch.object(asr, "_make_genai_client", make):
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    assert errors == []
    assert len(made) == 1


def test_missing_api_key_is_asr_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    t = GeminiTranscriber(timeout_s=30)
    unguarded = lambda timeout_s: asr._make_genai_client_unguarded(  # noqa: E731
        timeout_s=timeout_s
    )
    with patch.object(asr, "_make_genai_client", unguarded):
        with pytest.raises(TranscriptionUnavailable) as exc_info:
            t(b"x", "audio/wav")
    assert exc_info.value.reason == "asr_error"


def test_client_construction_valueerror_is_asr_error():
    def make(timeout_s):
        raise ValueError("bad client config")

    t = GeminiTranscriber(timeout_s=30)
    with patch.object(asr, "_make_genai_client", make):
        with pytest.raises(TranscriptionUnavailable) as exc_info:
            t(b"x", "audio/wav")
    assert exc_info.value.reason == "asr_error"


def test_guard_assertion_is_not_converted_to_unavailable(_guard_violations):
    # Under the conftest guard: the AssertionError must reach the test author.
    with pytest.raises(AssertionError, match="real Gemini"):
        GeminiTranscriber()(b"x", "audio/wav")
    assert len(_guard_violations) == 1
    _guard_violations.clear()  # provoked on purpose


def test_thought_parts_are_not_the_transcript():
    resp = response()
    resp.candidates[0].content.parts = [
        types.Part(text="REASONING", thought=True),
        types.Part(text="hello world"),
    ]
    t, _, p = transcriber_with(resp)
    with p:
        out = t(b"x", "audio/wav")
    assert out.text == "hello world"

    only_thought = response()
    only_thought.candidates[0].content.parts = [
        types.Part(text="REASONING", thought=True)
    ]
    t, _, p = transcriber_with(only_thought)
    with p, pytest.raises(TranscriptionUnavailable) as exc_info:
        t(b"x", "audio/wav")
    assert exc_info.value.reason == "asr_empty"


def test_thinking_tokens_are_recorded():
    resp = response()
    resp.usage_metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=100, candidates_token_count=5, thoughts_token_count=77
    )
    t, _, p = transcriber_with(resp)
    with p:
        assert t(b"x", "audio/wav").thinking_tokens == 77
    t, _, p = transcriber_with(response())
    with p:
        assert t(b"x", "audio/wav").thinking_tokens is None


def _blocked(reason):
    resp = response(candidates=False)
    resp.prompt_feedback = types.GenerateContentResponsePromptFeedback(
        block_reason=reason
    )
    return resp


def test_no_candidates_with_block_reason_other_is_asr_blocked():
    t, _, p = transcriber_with(_blocked(types.BlockedReason.OTHER))
    with p, pytest.raises(TranscriptionUnavailable) as exc_info:
        t(b"x", "audio/wav")
    assert exc_info.value.reason == "asr_blocked"
    assert "block_reason=OTHER" in str(exc_info.value)


@pytest.mark.parametrize("raw", ["OTHER", "BlockedReason.OTHER", "other"])
def test_a_string_block_reason_is_compared_by_name(raw):
    resp = response(candidates=False)
    resp.prompt_feedback = SimpleNamespace(block_reason=raw)
    t, _, p = transcriber_with(resp)
    with p, pytest.raises(TranscriptionUnavailable) as exc_info:
        t(b"x", "audio/wav")
    assert exc_info.value.reason == "asr_blocked"


def test_the_retryable_block_reasons_are_only_other():
    assert asr.RETRYABLE_BLOCK_REASONS == frozenset({types.BlockedReason.OTHER})


@pytest.mark.parametrize(
    "reason",
    [
        types.BlockedReason.SAFETY,
        types.BlockedReason.PROHIBITED_CONTENT,
        types.BlockedReason.BLOCKLIST,
        types.BlockedReason.IMAGE_SAFETY,
        types.BlockedReason.MODEL_ARMOR,
        types.BlockedReason.JAILBREAK,
        "SOME_FUTURE_CATEGORY",
    ],
)
def test_no_candidates_with_any_other_block_reason_is_asr_empty(reason):
    resp = response(candidates=False)
    resp.prompt_feedback = SimpleNamespace(block_reason=reason)
    t, _, p = transcriber_with(resp)
    with p, pytest.raises(TranscriptionUnavailable) as exc_info:
        t(b"x", "audio/wav")
    assert exc_info.value.reason == "asr_empty"
    assert f"block_reason={getattr(reason, 'name', reason)}" in str(exc_info.value)


@pytest.mark.parametrize(
    "feedback",
    [
        None,
        types.GenerateContentResponsePromptFeedback(),
        types.GenerateContentResponsePromptFeedback(
            block_reason=types.BlockedReason.BLOCKED_REASON_UNSPECIFIED
        ),
    ],
)
def test_no_candidates_without_a_block_reason_stays_asr_empty(feedback):
    resp = response(candidates=False)
    resp.prompt_feedback = feedback
    t, _, p = transcriber_with(resp)
    with p, pytest.raises(TranscriptionUnavailable) as exc_info:
        t(b"x", "audio/wav")
    assert exc_info.value.reason == "asr_empty"


def test_a_block_reason_with_candidates_present_is_not_asr_blocked():
    # Only "no candidates" is a block: a candidate with a bad finish reason is
    # still asr_incomplete, whatever the feedback says.
    resp = response(finish="SAFETY")
    resp.prompt_feedback = types.GenerateContentResponsePromptFeedback(
        block_reason=types.BlockedReason.OTHER
    )
    t, _, p = transcriber_with(resp)
    with p, pytest.raises(TranscriptionUnavailable) as exc_info:
        t(b"x", "audio/wav")
    assert exc_info.value.reason == "asr_incomplete"


def test_sdk_makes_exactly_one_attempt_on_503(monkeypatch):
    """Real SDK client against a local server: retries are really off."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            hits.append(self.path)
            body = json.dumps(
                {"error": {"code": 503, "message": "x", "status": "UNAVAILABLE"}}
            ).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    # httpx honours proxy env even for loopback; keep the test hermetic.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    t = GeminiTranscriber(timeout_s=10)
    try:
        with patch.object(
            asr,
            "_make_genai_client",
            lambda timeout_s: asr._make_genai_client_unguarded(
                timeout_s=timeout_s, base_url=base_url
            ),
        ):
            with pytest.raises(TranscriptionUnavailable) as exc_info:
                t(b"x", "audio/wav")
    finally:
        t.close()
        server.shutdown()
        server.server_close()
    assert exc_info.value.reason == "asr_error"
    assert len(hits) == 1


def test_policy_string_names_model_prompt_version_and_generation_config():
    t, client, p = transcriber_with(response("hello world"))
    with p:
        t(b"x", "audio/wav")
    config = client.models.calls[0]["config"]
    # The request config and the policy string derive from the same values.
    assert config == asr._generation_config()
    assert config.temperature == asr.ASR_TEMPERATURE
    assert asr.ASR_MODEL in asr.ASR_POLICY
    assert f"prompt-v{asr.ASR_PROMPT_VERSION}" in asr.ASR_POLICY
    assert f"temp{asr.ASR_TEMPERATURE}" in asr.ASR_POLICY


def test_production_default_sends_thinking_level_low_and_policy_says_so():
    t, client, p = transcriber_with(response("hello world"))
    with p:
        t(b"x", "audio/wav")
    config = client.models.calls[0]["config"]
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert config == asr._generation_config("low")
    assert t.thinking == "low"
    assert t.policy == asr.ASR_POLICY
    assert asr.policy_for() == asr.ASR_POLICY
    assert asr.ASR_POLICY.endswith("|thinking-low")


def test_explicit_default_thinking_sends_no_thinking_config():
    """ "default" stays selectable for calibration; it is not the production default."""
    client = FakeClient(response("hello world"))
    t = GeminiTranscriber(timeout_s=30, thinking="default")
    with patch.object(asr, "_make_genai_client", lambda timeout_s: client):
        t(b"x", "audio/wav")
    config = client.models.calls[0]["config"]
    assert config == types.GenerateContentConfig(temperature=0)
    assert config == asr._generation_config("default")
    assert config.thinking_config is None
    assert t.policy == asr.policy_for(thinking="default")
    assert t.policy.endswith("|thinking-default") and t.policy != asr.ASR_POLICY


def test_low_thinking_sends_thinking_level_low():
    client = FakeClient(response("hello world"))
    t = GeminiTranscriber(timeout_s=30, thinking="low")
    with patch.object(asr, "_make_genai_client", lambda timeout_s: client):
        t(b"x", "audio/wav")
    config = client.models.calls[0]["config"]
    assert config.temperature == 0
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert config == asr._generation_config("low")


def test_policy_differs_between_thinking_settings_and_tracks_model():
    default = GeminiTranscriber(thinking="default")
    low = GeminiTranscriber(thinking="low")
    assert default.policy != low.policy
    assert low.policy == asr.ASR_POLICY
    assert default.policy == asr.ASR_POLICY.replace("thinking-low", "thinking-default")
    other = GeminiTranscriber(model="gemini-x", thinking="low")
    assert other.policy.startswith("gemini-x|")


@pytest.mark.parametrize("bad", ["minimal", "", "LOW", None])
def test_unknown_thinking_is_a_value_error(bad):
    with pytest.raises(ValueError):
        GeminiTranscriber(thinking=bad)
    with pytest.raises(ValueError):
        asr._generation_config(bad)


def test_transcription_carries_the_transcribers_policy():
    t, _, p = transcriber_with(response("hello world"))
    with p:
        assert t(b"x", "audio/wav").policy == asr.ASR_POLICY
    default = GeminiTranscriber(timeout_s=30, thinking="default")
    with patch.object(
        asr, "_make_genai_client", lambda timeout_s: FakeClient(response())
    ):
        out = default(b"x", "audio/wav")
    assert out.policy == default.policy and "thinking-default" in out.policy


def test_default_policy_string_is_pinned_and_derived():
    assert asr.ASR_POLICY == "gemini-3.8-flash|prompt-v1|temp0|thinking-low"
    assert asr.ASR_POLICY == asr.policy_for("gemini-3.8-flash", "low")
    assert asr.policy_for(thinking="default") == (
        "gemini-3.8-flash|prompt-v1|temp0|thinking-default"
    )
    assert asr.ASR_PROMPT_VERSION == "1"


# --- usage on a call that answered but was unusable (my-podcasts-9p3.18) ---
#
# A blocked response reports its input in full and no candidates count (the
# 2026-10-03 probe: prompt 1777, total 1777). The raise carries that usage so the
# phase's token totals stay known; a call that never got a response has none.


def _with_usage(resp, **fields):
    resp.usage_metadata = types.GenerateContentResponseUsageMetadata(**fields)
    return resp


def _raised(resp) -> TranscriptionUnavailable:
    t, _, p = transcriber_with(resp)
    with p, pytest.raises(TranscriptionUnavailable) as exc_info:
        t(b"x", "audio/wav")
    return exc_info.value


def test_a_blocked_response_carries_its_input_tokens_and_zero_output():
    resp = _blocked(types.BlockedReason.OTHER)
    _with_usage(resp, prompt_token_count=1777, total_token_count=1777)
    exc = _raised(resp)
    assert exc.reason == "asr_blocked"
    u = exc.usage
    assert u is not None
    assert (u.input_tokens, u.output_tokens, u.thinking_tokens) == (1777, 0, None)
    assert u.elapsed_s >= 0


def test_a_blocked_response_derives_output_net_of_thinking():
    resp = _blocked(types.BlockedReason.OTHER)
    _with_usage(
        resp, prompt_token_count=1777, total_token_count=1800, thoughts_token_count=23
    )
    u = _raised(resp).usage
    assert (u.input_tokens, u.output_tokens, u.thinking_tokens) == (1777, 0, 23)


def test_a_reported_candidates_count_is_used_as_is():
    resp = _blocked(types.BlockedReason.OTHER)
    _with_usage(
        resp,
        prompt_token_count=1777,
        candidates_token_count=9,
        total_token_count=5000,
    )
    assert _raised(resp).usage.output_tokens == 9


@pytest.mark.parametrize(
    "fields",
    [
        {"prompt_token_count": 1777},  # no total
        {"total_token_count": 1777},  # no prompt
        {},
    ],
)
def test_output_stays_unknown_when_it_cannot_be_derived(fields):
    resp = _blocked(types.BlockedReason.OTHER)
    _with_usage(resp, **fields)
    u = _raised(resp).usage
    assert u is not None and u.output_tokens is None
    assert u.input_tokens == fields.get("prompt_token_count")


def test_a_response_with_no_usage_metadata_still_carries_elapsed_with_unknown_tokens():
    resp = _blocked(types.BlockedReason.OTHER)
    resp.usage_metadata = None
    u = _raised(resp).usage
    assert u is not None and u.elapsed_s >= 0
    assert (u.input_tokens, u.output_tokens, u.thinking_tokens) == (None, None, None)


def test_an_empty_no_candidates_response_carries_usage():
    resp = response(candidates=False)
    exc = _raised(resp)
    assert exc.reason == "asr_empty"
    assert (exc.usage.input_tokens, exc.usage.output_tokens) == (100, 5)


def test_an_incomplete_response_carries_usage():
    exc = _raised(response(finish="MAX_TOKENS"))
    assert exc.reason == "asr_incomplete"
    assert exc.usage.finish_reason == "MAX_TOKENS" and exc.usage.transcript_chars == 0
    assert (exc.usage.input_tokens, exc.usage.output_tokens) == (100, 5)


def test_a_transcript_with_no_word_tokens_carries_usage():
    resp = _with_usage(
        response(text="..."),
        prompt_token_count=100,
        candidates_token_count=5,
        thoughts_token_count=3,
    )
    exc = _raised(resp)
    assert exc.reason == "asr_empty"
    u = exc.usage
    assert (u.input_tokens, u.output_tokens, u.thinking_tokens) == (100, 5, 3)
    assert u.finish_reason == "STOP" and u.transcript_chars == len("...")


def test_a_blocked_usage_has_finish_none_and_no_transcript():
    u = _raised(_blocked(types.BlockedReason.OTHER)).usage
    assert u.finish_reason == "NONE" and u.transcript_chars == 0


@pytest.mark.parametrize(
    "bad", ["1777", 1777.0, True, object(), [1]], ids=lambda v: type(v).__name__
)
def test_a_non_int_usage_field_is_unknown_and_never_raises(bad):
    resp = _blocked(types.BlockedReason.OTHER)
    resp.usage_metadata = SimpleNamespace(
        prompt_token_count=bad,
        candidates_token_count=bad,
        thoughts_token_count=bad,
        total_token_count=bad,
    )
    u = _raised(resp).usage
    assert (u.input_tokens, u.output_tokens, u.thinking_tokens) == (None, None, None)


def test_a_non_int_total_does_not_stop_a_valid_derivation_elsewhere():
    resp = _blocked(types.BlockedReason.OTHER)
    resp.usage_metadata = SimpleNamespace(
        prompt_token_count=10, total_token_count="x", thoughts_token_count=None
    )
    u = _raised(resp).usage
    assert (u.input_tokens, u.output_tokens) == (10, None)


def test_reading_usage_never_raises_even_from_a_hostile_response():
    class Hostile:
        @property
        def usage_metadata(self):
            raise RuntimeError("boom")

    u = asr._read_usage(Hostile(), 1.0)
    assert (u.input_tokens, u.output_tokens, u.thinking_tokens) == (None, None, None)
    assert u.elapsed_s == 1.0


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (RuntimeError("boom"), "asr_error"),
        (httpx.ReadTimeout("slow"), "asr_timeout"),
    ],
)
def test_a_call_with_no_response_has_no_usage(result, reason):
    exc = _raised(result)
    assert exc.reason == reason and exc.usage is None


def test_no_budget_and_client_construction_failures_have_no_usage():
    with pytest.raises(TranscriptionUnavailable) as exc_info:
        GeminiTranscriber(timeout_s=0)(b"x", "audio/wav")
    assert exc_info.value.usage is None

    def make(timeout_s):
        raise ValueError("bad client config")

    with patch.object(asr, "_make_genai_client", make):
        with pytest.raises(TranscriptionUnavailable) as exc_info:
            GeminiTranscriber(timeout_s=30)(b"x", "audio/wav")
    assert exc_info.value.usage is None


def test_the_default_exception_has_no_usage():
    assert TranscriptionUnavailable("asr_error", "x").usage is None


def test_success_and_failure_read_usage_through_one_helper():
    # A success derives output the same way a raise does.
    resp = _with_usage(
        response("hello world"), prompt_token_count=10, total_token_count=14
    )
    t, _, p = transcriber_with(resp)
    with p:
        out = t(b"x", "audio/wav")
    assert (out.input_tokens, out.output_tokens, out.thinking_tokens) == (10, 4, None)
