"""Tests for pipeline.podcast_transcribe (no network; fake transcriber)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from functools import lru_cache
from types import SimpleNamespace

import pytest

from pipeline import podcast_transcribe as pt


@lru_cache(maxsize=4)
def _pcm(seconds: int) -> bytes:
    # 24 kHz s16 mono; 1 s blocks, loud except every 7th (quiet) so split_pcm
    # finds cuts. Built with join: repeated `+=` on 72 MB is quadratic.
    loud = b"\x00\x40" * 24000
    quiet = b"\x00\x00" * 24000
    return b"".join(loud if i % 7 else quiet for i in range(seconds))


class FakeTranscriber:
    def __init__(
        self,
        words_per_piece: int = 2000,
        fail_at: int | None = None,
        finish: str = "STOP",
    ):
        self.calls: list[str] = []
        self.audio: list[bytes] = []
        self.words = words_per_piece
        self.fail_at = fail_at
        self.finish = finish

    def __call__(self, audio: bytes, prompt: str) -> pt.PieceResult:
        i = len(self.calls)
        self.calls.append(prompt)
        self.audio.append(audio)
        if i == self.fail_at:
            raise pt.TranscriptionError("asr_error: boom")
        text = "\n".join(
            f"Jerusalem Demsas: piece{i} " + "word " * 19
            for _ in range(self.words // 20)
        )
        return pt.PieceResult(
            text=text, finish_reason=self.finish, input_tokens=1, output_tokens=2
        )


@pytest.fixture
def audio(tmp_path, monkeypatch):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"fake-mp3")
    monkeypatch.setattr(pt, "decode_to_pcm_with_log", lambda p: (_pcm(1500), "warn"))
    monkeypatch.setattr(pt, "encode_piece", lambda pcm: b"mp3" + pcm[:16])
    return path


def _run(audio, tmp_path, fake, **kw):
    kw.setdefault("rss_duration_s", 1500)
    kw.setdefault("description", "")
    kw.setdefault("hosts", ())
    return pt.transcribe_episode(audio, tmp_path, transcriber=fake, **kw)


def _manifest(tmp_path):
    return json.loads((tmp_path / "transcript.json").read_text())


def test_pieces_cover_audio_in_order(audio, tmp_path) -> None:
    fake = FakeTranscriber()
    text = _run(
        audio,
        tmp_path,
        fake,
        description="d",
        hosts=("Jerusalem Demsas", "Matt Yglesias"),
    )
    pieces = _manifest(tmp_path)["pieces"]
    assert len(pieces) == len(fake.calls) >= 2
    assert pieces[0]["start_s"] == 0
    for a, b in zip(pieces, pieces[1:], strict=False):
        assert a["end_s"] == b["start_s"]
    assert pieces[-1]["end_s"] == pytest.approx(1500, abs=0.01)
    assert text.index("piece0") < text.index("piece1")
    assert (tmp_path / "transcript.txt").read_text() == text


def test_manifest_records_evidence(audio, tmp_path) -> None:
    _run(audio, tmp_path, FakeTranscriber())
    m = _manifest(tmp_path)
    assert m["audio_sha256"] == hashlib.sha256(b"fake-mp3").hexdigest()
    assert m["rss_duration_s"] == 1500
    assert m["decoded_duration_s"] == pytest.approx(1500)
    assert m["duration_check_skipped"] is False
    assert m["ffmpeg_warnings"] == "warn"
    assert m["prompt_version"] == pt.PIECE_PROMPT_VERSION
    assert m["carry_context"] is True
    assert "model" in m
    p = m["pieces"][0]
    assert p["index"] == 0
    assert p["mp3_sha256"] == hashlib.sha256(b"mp3" + _pcm(1500)[:16]).hexdigest()
    assert p["bytes"] == 3 + 16
    assert p["finish_reason"] == "STOP"
    assert (p["input_tokens"], p["output_tokens"]) == (1, 2)
    assert p["words"] == len(p["text"].split())


def test_rss_none_skips_duration_check_and_records_it(audio, tmp_path) -> None:
    _run(audio, tmp_path, FakeTranscriber(), rss_duration_s=None)
    m = _manifest(tmp_path)
    assert m["rss_duration_s"] is None
    assert m["duration_check_skipped"] is True


def test_duration_tolerance_boundary(audio, tmp_path) -> None:
    # decoded 1500; allowed = max(120, 0.08 * rss). rss=1620 -> diff 120: ok.
    _run(audio, tmp_path, FakeTranscriber(), rss_duration_s=1620)
    # rss=1621 -> diff 121 > max(120, 129.68)? no, 129.68 > 121: still ok.
    (tmp_path / "transcript.txt").unlink()
    _run(audio, tmp_path, FakeTranscriber(), rss_duration_s=1621)
    # rss=1700 -> diff 200 > max(120, 136): refused.
    (tmp_path / "transcript.txt").unlink()
    with pytest.raises(pt.TranscriptionError, match="duration"):
        _run(audio, tmp_path, FakeTranscriber(), rss_duration_s=1700)


def test_one_failed_piece_fails_all(audio, tmp_path) -> None:
    with pytest.raises(pt.TranscriptionError):
        _run(audio, tmp_path, FakeTranscriber(fail_at=1))
    assert not (tmp_path / "transcript.txt").exists()
    assert not (tmp_path / "transcript.json").exists()


def test_failed_rerun_does_not_leave_stale_transcript(audio, tmp_path) -> None:
    _run(audio, tmp_path, FakeTranscriber(), carry_context=True)
    with pytest.raises(pt.TranscriptionError):
        # different carry_context -> not reusable -> re-transcribes -> fails
        _run(audio, tmp_path, FakeTranscriber(fail_at=0), carry_context=False)
    assert not (tmp_path / "transcript.txt").exists()
    assert not (tmp_path / "transcript.json").exists()


def test_word_rate_floor(audio, tmp_path) -> None:
    with pytest.raises(pt.TranscriptionError, match="words"):
        _run(audio, tmp_path, FakeTranscriber(words_per_piece=100))
    assert not (tmp_path / "transcript.txt").exists()


def test_non_stop_result_refused(audio, tmp_path) -> None:
    with pytest.raises(pt.TranscriptionError, match="MAX_TOKENS"):
        _run(audio, tmp_path, FakeTranscriber(finish="MAX_TOKENS"))


def test_truncated_audio_refused_by_duration(audio, tmp_path) -> None:
    with pytest.raises(pt.TranscriptionError, match="duration"):
        _run(audio, tmp_path, FakeTranscriber(), rss_duration_s=4800)


def test_decode_failure_is_transcription_error(audio, tmp_path, monkeypatch) -> None:
    def boom(path):
        raise RuntimeError("ffmpeg decode failed")

    monkeypatch.setattr(pt, "decode_to_pcm_with_log", boom)
    with pytest.raises(pt.TranscriptionError, match="decode"):
        _run(audio, tmp_path, FakeTranscriber())


def test_empty_decode_refused(audio, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pt, "decode_to_pcm_with_log", lambda p: (b"", ""))
    with pytest.raises(pt.TranscriptionError):
        _run(audio, tmp_path, FakeTranscriber(), rss_duration_s=None)


def test_reuse_skips_asr(audio, tmp_path) -> None:
    first = _run(audio, tmp_path, FakeTranscriber())
    again = FakeTranscriber()
    second = _run(audio, tmp_path, again)
    assert again.calls == []
    assert second == first


@pytest.mark.parametrize(
    "mutate",
    [
        "audio_changed",
        "prompt_version",
        "model",
        "carry_context",
        "manifest_missing",
        "transcript_missing",
        "transcript_tampered",
    ],
)
def test_reuse_requires_both_files_and_matching_identity(
    audio, tmp_path, mutate
) -> None:
    """Reuse needs transcript.txt AND transcript.json, with a matching
    audio_sha256, prompt version, model and carry_context. Anything else
    re-transcribes (a mismatch is never silently reused)."""
    _run(audio, tmp_path, FakeTranscriber())
    manifest_path = tmp_path / "transcript.json"
    m = _manifest(tmp_path)
    if mutate == "audio_changed":
        audio.write_bytes(b"other-mp3")
    elif mutate in ("prompt_version", "model"):
        m[mutate] = "different"
        manifest_path.write_text(json.dumps(m))
    elif mutate == "carry_context":
        m["carry_context"] = False
        manifest_path.write_text(json.dumps(m))
    elif mutate == "manifest_missing":
        manifest_path.unlink()
    elif mutate == "transcript_missing":
        (tmp_path / "transcript.txt").unlink()
    elif mutate == "transcript_tampered":
        (tmp_path / "transcript.txt").write_text("half a transc")
    again = FakeTranscriber()
    _run(audio, tmp_path, again)
    assert len(again.calls) >= 2


def test_corrupt_manifest_retranscribes(audio, tmp_path) -> None:
    _run(audio, tmp_path, FakeTranscriber())
    (tmp_path / "transcript.json").write_text("{not json")
    again = FakeTranscriber()
    _run(audio, tmp_path, again)
    assert len(again.calls) >= 2


def test_no_tmp_files_left(audio, tmp_path) -> None:
    _run(audio, tmp_path, FakeTranscriber())
    assert sorted(p.name for p in tmp_path.glob("transcript*")) == [
        "transcript.json",
        "transcript.txt",
    ]


def test_context_carried_into_later_prompts(audio, tmp_path) -> None:
    fake = FakeTranscriber()
    _run(audio, tmp_path, fake, hosts=("Jerusalem Demsas",))
    assert "piece0" not in fake.calls[0]
    assert "piece0" in fake.calls[1]
    assert "Speakers identified so far: Jerusalem Demsas" in fake.calls[1]


def test_context_not_carried_when_disabled(audio, tmp_path) -> None:
    fake = FakeTranscriber()
    _run(audio, tmp_path, fake, carry_context=False)
    assert all("piece0" not in c for c in fake.calls)
    assert _manifest(tmp_path)["carry_context"] is False


def test_piece_size_ceiling(audio, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pt, "encode_piece", lambda pcm: b"x" * (pt.MAX_PIECE_BYTES + 1))
    fake = FakeTranscriber()
    with pytest.raises(pt.TranscriptionError, match="bytes"):
        _run(audio, tmp_path, fake)
    assert fake.calls == []


def test_encode_failure_is_transcription_error(audio, tmp_path, monkeypatch) -> None:
    def boom(pcm):
        raise pt.TranscriptionError("encode failed")

    monkeypatch.setattr(pt, "encode_piece", boom)
    with pytest.raises(pt.TranscriptionError):
        _run(audio, tmp_path, FakeTranscriber())


# --- encode_piece -----------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.CalledProcessError(1, "ffmpeg", stderr=b"bad"),
        subprocess.TimeoutExpired("ffmpeg", 120),
        FileNotFoundError(2, "no ffmpeg"),
    ],
)
def test_encode_piece_wraps_ffmpeg_failures(monkeypatch, exc) -> None:
    def run(*a, **kw):
        raise exc

    monkeypatch.setattr(pt.subprocess, "run", run)
    with pytest.raises(pt.TranscriptionError, match="encode"):
        pt.encode_piece(b"\x00\x00" * 100)


def test_encode_piece_refuses_empty_output(monkeypatch) -> None:
    monkeypatch.setattr(
        pt.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, b"", b""),
    )
    with pytest.raises(pt.TranscriptionError, match="encode"):
        pt.encode_piece(b"\x00\x00" * 100)


def test_encode_piece_command_and_timeout(monkeypatch) -> None:
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, b"mp3data", b"")

    monkeypatch.setattr(pt.subprocess, "run", run)
    assert pt.encode_piece(b"\x01\x00" * 10) == b"mp3data"
    cmd = seen["cmd"]
    assert cmd[0] == "ffmpeg"
    for token in ("s16le", "24000", "64k", "mp3", "pipe:0", "pipe:1"):
        assert token in cmd
    assert seen["kw"]["timeout"] == 120
    assert seen["kw"]["input"] == b"\x01\x00" * 10


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_encode_piece_real_ffmpeg() -> None:
    out = pt.encode_piece(b"\x00\x00" * 24000 * 2)  # 2 s of silence
    assert out[:3] == b"ID3" or (out[0] == 0xFF and out[1] & 0xE0 == 0xE0)
    assert 0 < len(out) < pt.MAX_PIECE_BYTES


# --- prompt -----------------------------------------------------------------


def test_prompt_content_rules() -> None:
    p = pt.build_piece_prompt(
        index=0,
        count=3,
        hosts=("Jerusalem Demsas", "Matt Yglesias"),
        description="Guest: Jordan Schneider",
        context=None,
    )
    assert "piece 1 of 3" in p
    assert "Jerusalem Demsas, Matt Yglesias" in p
    assert "Jordan Schneider" in p
    assert "Unknown speaker A" in p and "Unknown speaker B" in p
    assert "[SPONSOR]" in p and "[/SPONSOR]" in p
    assert "Context from the end" not in p


def test_context_block_last_20_lines_and_speakers_exclude_unknown(
    audio, tmp_path
) -> None:
    class Seq:
        def __init__(self):
            self.prompts = []

        def __call__(self, audio_bytes, prompt):
            i = len(self.prompts)
            self.prompts.append(prompt)
            lines = [f"Matt Yglesias: line{i}-{n} " + "w " * 40 for n in range(40)]
            lines.append("Unknown speaker A: mystery " + "w " * 40)
            lines.append("[SPONSOR]")
            lines.append("Brand: buy now " + "w " * 40)
            lines.append("[/SPONSOR]")
            return pt.PieceResult("\n".join(lines * 20), "STOP", None, None)

    seq = Seq()
    _run(audio, tmp_path, seq)
    ctx = seq.prompts[1]
    assert "Speakers identified so far: Matt Yglesias" in ctx
    tail = ctx.split("consistent):\n")[1].split("\nSpeakers identified so far:")[0]
    assert len(tail.splitlines()) == 20
    assert "line0-0" not in ctx  # an early line is not in the last 20
    speakers = ctx.split("Speakers identified so far:")[1]
    assert "Unknown speaker" not in speakers
    assert "Brand" not in speakers  # sponsor-read speakers are not speakers


# --- GeminiPieceTranscriber -------------------------------------------------


class _Cand:
    def __init__(self, name):
        self.finish_reason = type("F", (), {"name": name})()


def _resp(
    name="STOP", text="Jerusalem Demsas: hi", usage=None, cands=True, feedback=None
):
    return SimpleNamespace(
        candidates=[_Cand(name)] if cands else [],
        prompt_feedback=feedback,
        text=text,
        usage_metadata=usage,
    )


def _client_returning(resp, seen=None):
    class Models:
        def generate_content(self, **kw):
            if seen is not None:
                seen.update(kw)
            if isinstance(resp, Exception):
                raise resp
            return resp

    class Client:
        models = Models()

        def close(self):
            pass

    return Client()


def test_gemini_transcriber_refuses_non_stop(monkeypatch) -> None:
    client = _client_returning(_resp("MAX_TOKENS"))
    monkeypatch.setattr("pipeline.tts.asr._make_genai_client", lambda timeout_s: client)
    with pytest.raises(pt.TranscriptionError, match="MAX_TOKENS"):
        pt.GeminiPieceTranscriber()(b"mp3", "prompt")


def test_gemini_transcriber_success_and_call_shape(monkeypatch) -> None:
    usage = type("U", (), {"prompt_token_count": 11, "candidates_token_count": 7})()
    seen: dict = {}
    client = _client_returning(_resp(usage=usage), seen)
    monkeypatch.setattr("pipeline.tts.asr._make_genai_client", lambda timeout_s: client)
    t = pt.GeminiPieceTranscriber()
    r = t(b"mp3", "prompt")
    assert r == pt.PieceResult("Jerusalem Demsas: hi", "STOP", 11, 7)
    assert seen["model"] == pt.ASR_MODEL == t.model
    cfg = seen["config"]
    assert cfg.temperature == 0
    assert cfg.max_output_tokens == 16384
    assert cfg.thinking_config.thinking_level.name == "LOW"
    assert seen["contents"][1] == "prompt"


def test_gemini_transcriber_usage_absent_is_none(monkeypatch) -> None:
    client = _client_returning(_resp(usage=None))
    monkeypatch.setattr("pipeline.tts.asr._make_genai_client", lambda timeout_s: client)
    r = pt.GeminiPieceTranscriber()(b"mp3", "prompt")
    assert (r.input_tokens, r.output_tokens) == (None, None)


def test_gemini_transcriber_no_candidates(monkeypatch) -> None:
    resp = _resp(
        cands=False,
        feedback=SimpleNamespace(block_reason=SimpleNamespace(name="OTHER")),
    )
    client = _client_returning(resp)
    monkeypatch.setattr("pipeline.tts.asr._make_genai_client", lambda timeout_s: client)
    with pytest.raises(pt.TranscriptionError, match="asr_empty.*OTHER"):
        pt.GeminiPieceTranscriber()(b"mp3", "prompt")


def test_gemini_transcriber_wraps_sdk_errors(monkeypatch) -> None:
    client = _client_returning(ConnectionError("reset"))
    monkeypatch.setattr("pipeline.tts.asr._make_genai_client", lambda timeout_s: client)
    with pytest.raises(pt.TranscriptionError, match="asr_error"):
        pt.GeminiPieceTranscriber()(b"mp3", "prompt")


def test_gemini_transcriber_empty_text(monkeypatch) -> None:
    client = _client_returning(_resp(text="  "))
    monkeypatch.setattr("pipeline.tts.asr._make_genai_client", lambda timeout_s: client)
    with pytest.raises(pt.TranscriptionError, match="asr_empty"):
        pt.GeminiPieceTranscriber()(b"mp3", "prompt")


def test_gemini_transcriber_missing_api_key(monkeypatch) -> None:
    def no_key(timeout_s):
        raise KeyError("GEMINI_API_KEY")

    monkeypatch.setattr("pipeline.tts.asr._make_genai_client", no_key)
    with pytest.raises(pt.TranscriptionError, match="asr_error"):
        pt.GeminiPieceTranscriber()(b"mp3", "prompt")
