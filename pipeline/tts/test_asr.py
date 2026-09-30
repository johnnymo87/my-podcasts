import io
import wave
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


def test_guard_blocks_real_client_in_tests():
    with pytest.raises(AssertionError, match="real Gemini"):
        asr._make_genai_client(timeout_s=1)


def test_pcm_to_wav_roundtrip():
    pcm = b"\x01\x00\xff\x7f" * 100
    with wave.open(io.BytesIO(pcm_to_wav(pcm))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24_000)
        assert w.readframes(w.getnframes()) == pcm


def test_pcm_to_wav_rejects_odd_length():
    with pytest.raises(ValueError):
        pcm_to_wav(b"\x00\x00\x00")
