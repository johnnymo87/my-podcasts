"""Tests for pipeline.podcast_transcribe (no network; fake transcriber)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from functools import lru_cache

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from pipeline import podcast_transcribe as pt


NAMES_LABEL = (
    "Names already used for voices in earlier pieces "
    "(match by voice, not by this list alone)"
)


@lru_cache(maxsize=4)
def _pcm(seconds: int) -> bytes:
    # 24 kHz s16 mono; 1 s blocks, loud except every 7th (quiet) so split_pcm
    # finds cuts. Built with join: repeated `+=` on 72 MB is quadratic.
    loud = b"\x00\x40" * 24000
    quiet = b"\x00\x00" * 24000
    return b"".join(loud if i % 7 else quiet for i in range(seconds))


class FakeTranscriber:
    """Piece index = number of calls that have succeeded so far, so a retry of
    piece N produces piece N again (not piece N+1)."""

    def __init__(
        self,
        words_per_piece: int = 1500,
        fail_piece: int | None = None,
        transient: int | None = None,
        exc: Exception | None = None,
        finish: str = "STOP",
        tag: str = "",
        model: str | None = None,
    ):
        self.calls: list[str] = []
        self.audio: list[bytes] = []
        self.words = words_per_piece
        self.fail_piece = fail_piece
        self.transient = transient  # fail this many times, then succeed
        self.exc = exc
        self.finish = finish
        self.tag = tag
        self.accepted = 0
        self.failed = 0
        if model is not None:
            self.model = model

    def __call__(self, audio: bytes, prompt: str) -> pt.PieceResult:
        i = self.accepted
        self.calls.append(prompt)
        self.audio.append(audio)
        if i == self.fail_piece and (
            self.transient is None or self.failed < self.transient
        ):
            self.failed += 1
            raise self.exc or pt.TranscriptionError("asr_error: boom")
        self.accepted += 1
        text = "\n".join(
            f"Jerusalem Demsas: piece{i}{self.tag} " + "word " * 19
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
    kw.setdefault("sleep", lambda s: None)
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
        _run(audio, tmp_path, FakeTranscriber(fail_piece=1))
    assert not (tmp_path / "transcript.txt").exists()
    assert not (tmp_path / "transcript.json").exists()


def test_failed_rerun_does_not_leave_stale_transcript(audio, tmp_path) -> None:
    _run(audio, tmp_path, FakeTranscriber(), carry_context=True)
    with pytest.raises(pt.TranscriptionError):
        # different carry_context -> not reusable -> re-transcribes -> fails
        _run(audio, tmp_path, FakeTranscriber(fail_piece=1), carry_context=False)
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
        "prompt_inputs",
        "policy",
        "description_changed",
        "hosts_changed",
        "manifest_missing",
        "transcript_missing",
        "transcript_tampered",
    ],
)
def test_reuse_requires_both_files_and_matching_identity(
    audio, tmp_path, mutate
) -> None:
    """Reuse needs transcript.txt AND transcript.json, with a matching
    audio_sha256, prompt version, model, carry_context and prompt inputs
    (hosts, description, template, piece sizes, ...). Anything else
    re-transcribes (a mismatch is never silently reused). Piece checkpoints are
    removed first so this isolates the whole-transcript identity."""
    _run(audio, tmp_path, FakeTranscriber())
    shutil.rmtree(tmp_path / "pieces")
    manifest_path = tmp_path / "transcript.json"
    m = _manifest(tmp_path)
    kw: dict = {}
    if mutate == "description_changed":
        kw["description"] = "a new guest"
    elif mutate == "hosts_changed":
        kw["hosts"] = ("Someone Else",)
    if mutate == "audio_changed":
        audio.write_bytes(b"other-mp3")
    elif mutate in ("prompt_version", "model", "prompt_inputs", "policy"):
        key = {"prompt_inputs": "prompt_inputs_sha256", "policy": "policy_sha256"}
        m[key.get(mutate, mutate)] = "different"
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
    _run(audio, tmp_path, again, **kw)
    assert len(again.calls) >= 2


def test_cached_transcript_is_rechecked_against_the_current_duration(
    audio, tmp_path, monkeypatch
) -> None:
    """Reuse used to return before any validation: a 180 s transcript cached
    under a matching RSS duration was served for a 4800 s episode."""
    monkeypatch.setattr(pt, "decode_to_pcm_with_log", lambda p: (_pcm(180), ""))
    _run(audio, tmp_path, FakeTranscriber(words_per_piece=400), rss_duration_s=180)
    again = FakeTranscriber(words_per_piece=400)
    with pytest.raises(pt.TranscriptionError, match="duration"):
        _run(audio, tmp_path, again, rss_duration_s=4800)
    assert again.calls == []
    assert not (tmp_path / "transcript.txt").exists()  # stale cache dropped


def test_cached_transcript_failing_current_word_rate_rules_is_recomputed(
    audio, tmp_path, monkeypatch
) -> None:
    _run(audio, tmp_path, FakeTranscriber())  # 150 wpm pieces
    shutil.rmtree(tmp_path / "pieces")
    monkeypatch.setattr(pt, "MIN_WORDS_PER_MIN", 200)  # now too sparse
    again = FakeTranscriber()
    with pytest.raises(pt.TranscriptionError, match="words/min"):
        _run(audio, tmp_path, again)
    assert len(again.calls) == 1  # not served from cache; recomputed, then refused


def test_cached_transcript_failing_a_lowered_ceiling_is_recomputed(
    audio, tmp_path, monkeypatch
) -> None:
    _run(audio, tmp_path, FakeTranscriber())
    shutil.rmtree(tmp_path / "pieces")
    monkeypatch.setattr(pt, "MAX_WORDS_PER_MIN", 100)
    again = FakeTranscriber()
    with pytest.raises(pt.TranscriptionError, match="words/min"):
        _run(audio, tmp_path, again)
    assert len(again.calls) == 1


def test_still_valid_cached_transcript_is_reused_after_checks(audio, tmp_path) -> None:
    _run(audio, tmp_path, FakeTranscriber())
    again = FakeTranscriber()
    _run(audio, tmp_path, again, rss_duration_s=1500)
    assert again.calls == []


def test_corrupt_manifest_retranscribes(audio, tmp_path) -> None:
    _run(audio, tmp_path, FakeTranscriber())
    shutil.rmtree(tmp_path / "pieces")
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
    assert f"{NAMES_LABEL}: Jerusalem Demsas" in fake.calls[1]


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


def test_prompt_attribution_rules_do_not_lean_on_a_carried_list() -> None:
    p = pt.build_piece_prompt(
        index=1, count=3, hosts=("Matt Yglesias",), description="", context="ctx"
    )
    flat = " ".join(p.split())
    assert "speaker list carried" not in flat
    for rule in (
        "self-introduction",
        "addressed by name",
        "clearly the same voice as a named speaker in the context lines",
    ):
        assert rule in flat
    # Hosts are possible participants, never a licence to attribute.
    assert "Hosts: Matt Yglesias" in p


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
            return pt.PieceResult("\n".join(lines), "STOP", None, None)

    seq = Seq()
    _run(audio, tmp_path, seq)
    ctx = seq.prompts[1]
    assert f"{NAMES_LABEL}: Matt Yglesias" in ctx
    tail = ctx.split("consistent):\n")[1].split(f"\n{NAMES_LABEL}:")[0]
    assert len(tail.splitlines()) == 20
    assert "line0-0" not in ctx  # an early line is not in the last 20
    speakers = ctx.split(f"{NAMES_LABEL}:")[1]
    assert "Unknown speaker" not in speakers
    assert "Brand" not in speakers  # sponsor-read speakers are not speakers
    assert "Unknown speaker" not in tail
    assert "[unidentified speaker]:" in tail


# --- tail floor and word-rate ceiling -----------------------------------------


def _short_tail_pcm_len() -> int:
    total = 1250
    ranges = pt.split_pcm(
        _pcm(total), target_s=pt.PIECE_TARGET_S, search_s=pt.PIECE_SEARCH_S
    )
    last_s = (ranges[-1][1] - ranges[-1][0]) / 48000
    assert last_s < pt.MIN_CHECKED_PIECE_S, last_s  # precondition of the tests
    return total


def test_short_tail_piece_exempt_from_word_floor(audio, tmp_path, monkeypatch) -> None:
    total = _short_tail_pcm_len()
    monkeypatch.setattr(pt, "decode_to_pcm_with_log", lambda p: (_pcm(total), ""))
    inner = FakeTranscriber()
    n_pieces = len(
        pt.split_pcm(
            _pcm(total), target_s=pt.PIECE_TARGET_S, search_s=pt.PIECE_SEARCH_S
        )
    )

    def fake(audio_bytes, prompt):
        if f"piece {n_pieces} of {n_pieces}" in prompt:  # the short outro
            return pt.PieceResult(
                "Jerusalem Demsas: Thanks for listening.", "STOP", None, None
            )
        return inner(audio_bytes, prompt)

    _run(audio, tmp_path, fake, rss_duration_s=total)
    last = _manifest(tmp_path)["pieces"][-1]
    assert last["words"] == 5
    assert last["end_s"] - last["start_s"] < pt.MIN_CHECKED_PIECE_S


def _tail_run(audio, tmp_path, monkeypatch, tail_text: str):
    total = _short_tail_pcm_len()
    monkeypatch.setattr(pt, "decode_to_pcm_with_log", lambda p: (_pcm(total), ""))
    inner = FakeTranscriber()
    n_pieces = len(
        pt.split_pcm(
            _pcm(total), target_s=pt.PIECE_TARGET_S, search_s=pt.PIECE_SEARCH_S
        )
    )

    def fake(audio_bytes, prompt):
        if f"piece {n_pieces} of {n_pieces}" in prompt:
            return pt.PieceResult(tail_text, "STOP", None, None)
        return inner(audio_bytes, prompt)

    return _run(audio, tmp_path, fake, rss_duration_s=total)


def test_short_tail_piece_is_not_exempt_from_ceiling(
    audio, tmp_path, monkeypatch
) -> None:
    runaway = "Jerusalem Demsas: " + "loop " * 10_000  # ~88 s tail
    with pytest.raises(pt.TranscriptionError, match="words/min") as ei:
        _tail_run(audio, tmp_path, monkeypatch, runaway)
    assert ei.value.reason == "asr_runaway"
    assert not (tmp_path / "transcript.txt").exists()


def test_normal_short_tail_is_accepted(audio, tmp_path, monkeypatch) -> None:
    _tail_run(audio, tmp_path, monkeypatch, "Jerusalem Demsas: " + "word " * 28)
    last = _manifest(tmp_path)["pieces"][-1]
    assert last["words"] == 30
    assert last["end_s"] - last["start_s"] < pt.MIN_CHECKED_PIECE_S


def test_ceiling_has_an_absolute_cushion_for_short_pieces() -> None:
    # 400 wpm * minutes + 60 words: 30 words in 20 s is fine (193 allowed) ...
    assert pt._piece_rate_error(0, 30, 20.0) is None
    # ... the cushion itself is accepted, one word more is not.
    assert pt._piece_rate_error(0, 60 + 400, 60.0) is None
    err = pt._piece_rate_error(0, 60 + 400 + 1, 60.0)
    assert err is not None and err.reason == "asr_runaway"
    # a very short piece cannot be flagged by the rate alone
    assert pt._piece_rate_error(0, 40, 1.0) is None
    # the floor still has its own 120 s exemption
    assert pt._piece_rate_error(0, 0, 119.0) is None
    floor = pt._piece_rate_error(0, 10, 120.0)
    assert floor is not None and floor.reason == "asr_incomplete"


def test_word_rate_ceiling(audio, tmp_path) -> None:
    fake = FakeTranscriber(words_per_piece=20000)
    with pytest.raises(pt.TranscriptionError, match="words/min") as ei:
        _run(audio, tmp_path, fake)
    assert ei.value.reason == "asr_runaway"
    assert len(fake.calls) == 1  # not retried
    assert not (tmp_path / "transcript.txt").exists()


def test_words_per_min_recorded(audio, tmp_path) -> None:
    _run(audio, tmp_path, FakeTranscriber())
    p = _manifest(tmp_path)["pieces"][0]
    minutes = (p["end_s"] - p["start_s"]) / 60
    assert p["words_per_min"] == pytest.approx(p["words"] / minutes)


def test_floor_failure_not_retried(audio, tmp_path) -> None:
    fake = FakeTranscriber(words_per_piece=100)
    with pytest.raises(pt.TranscriptionError, match="words"):
        _run(audio, tmp_path, fake)
    assert len(fake.calls) == 1


# --- retry ----------------------------------------------------------------------


def _err(reason, code=None):
    return pt.TranscriptionError(f"{reason}: x", reason=reason, code=code)


def test_error_carries_structured_reason() -> None:
    e = pt.TranscriptionError("asr_error: boom")
    assert e.reason == "asr_error" and e.code is None
    assert pt.TranscriptionError("other").reason == "error"
    assert _err("asr_timeout").reason == "asr_timeout"


@pytest.mark.parametrize(
    "exc",
    [
        _err("asr_timeout"),
        _err("asr_blocked"),
        _err("asr_error"),  # no code: network
        _err("asr_error", 429),
        _err("asr_error", 500),
        _err("asr_error", 503),
    ],
)
def test_retryable_errors_retry_then_succeed(audio, tmp_path, exc) -> None:
    sleeps: list[float] = []
    fake = FakeTranscriber(fail_piece=1, transient=2, exc=exc)
    _run(audio, tmp_path, fake, sleep=sleeps.append)
    assert sleeps == [5.0, 20.0]
    pieces = _manifest(tmp_path)["pieces"]
    assert [p["attempts"] for p in pieces] == [1, 3, 1]
    assert len(fake.calls) == 5


def test_retry_gives_up_after_three_attempts(audio, tmp_path) -> None:
    sleeps: list[float] = []
    fake = FakeTranscriber(fail_piece=0, exc=_err("asr_timeout"))
    with pytest.raises(pt.TranscriptionError, match="3 attempts") as ei:
        _run(audio, tmp_path, fake, sleep=sleeps.append)
    assert len(fake.calls) == 3 and sleeps == [5.0, 20.0]
    assert ei.value.reason == "asr_timeout"


@pytest.mark.parametrize(
    "exc",
    [
        _err("asr_incomplete"),
        _err("asr_empty"),
        _err("asr_error", 400),
        _err("asr_error", 403),
        _err("asr_error", 404),
    ],
)
def test_non_retryable_errors_fail_at_once(audio, tmp_path, exc) -> None:
    sleeps: list[float] = []
    fake = FakeTranscriber(fail_piece=0, exc=exc)
    with pytest.raises(pt.TranscriptionError) as ei:
        _run(audio, tmp_path, fake, sleep=sleeps.append)
    assert len(fake.calls) == 1 and sleeps == []
    assert ei.value.reason == exc.reason and ei.value.code == exc.code


# --- piece checkpoints ----------------------------------------------------------


def test_checkpoints_resume_after_failed_piece(audio, tmp_path) -> None:
    first = FakeTranscriber(fail_piece=2, exc=_err("asr_error", 400))
    with pytest.raises(pt.TranscriptionError):
        _run(audio, tmp_path, first)
    assert not (tmp_path / "transcript.txt").exists()
    assert sorted(p.name for p in (tmp_path / "pieces").glob("*.json")) == [
        "0000.json",
        "0001.json",
    ]
    second = FakeTranscriber()
    second.accepted = 2  # so its text is "piece2", as an uninterrupted run's
    text = _run(audio, tmp_path, second)
    assert len(second.calls) == 1  # pieces 0 and 1 were not re-called
    assert text.index("piece0") < text.index("piece1") < text.index("piece2")
    cp = json.loads((tmp_path / "pieces" / "0000.json").read_text())
    assert {"key", "text", "finish_reason", "attempts"} <= cp.keys()
    assert _manifest(tmp_path)["pieces"][0]["attempts"] == 1


def test_checkpoint_invalidated_when_upstream_text_changes(audio, tmp_path) -> None:
    with pytest.raises(pt.TranscriptionError):
        _run(audio, tmp_path, FakeTranscriber(fail_piece=2, exc=_err("asr_error", 400)))
    (tmp_path / "pieces" / "0000.json").unlink()  # piece 0 must be re-done
    again = FakeTranscriber(tag="X")
    text = _run(audio, tmp_path, again)
    # piece 0 redone with different text -> piece 1's prompt differs -> redone.
    assert len(again.calls) == 3
    assert "piece0X" in text


def test_checkpoint_invalidated_by_model_or_audio(audio, tmp_path) -> None:
    _run(audio, tmp_path, FakeTranscriber(model="m1"))
    (tmp_path / "transcript.txt").unlink()
    other_model = FakeTranscriber(model="m2")
    _run(audio, tmp_path, other_model)
    assert len(other_model.calls) == 3
    (tmp_path / "transcript.txt").unlink()
    audio.write_bytes(b"different-audio")
    other_audio = FakeTranscriber(model="m2")
    _run(audio, tmp_path, other_audio)
    assert len(other_audio.calls) == 3


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PIECE_PROMPT_VERSION", "2"),
        ("MAX_OUTPUT_TOKENS", 8192),
        ("THINKING_LEVEL", "HIGH"),
        ("TEMPERATURE", 0.5),
        ("ENCODE_BITRATE", "96k"),
        ("PIECE_TARGET_S", 590.0),
    ],
)
def test_policy_change_invalidates_checkpoints_and_transcript(
    audio, tmp_path, monkeypatch, name, value
) -> None:
    """Every generation/encoding/splitting knob is in one digest that keys both
    the checkpoints and the whole-transcript reuse: change one and nothing
    already paid for is silently reused."""
    _run(audio, tmp_path, FakeTranscriber())
    monkeypatch.setattr(pt, name, value)
    again = FakeTranscriber()
    _run(audio, tmp_path, again)
    pieces = _manifest(tmp_path)["pieces"]
    assert len(again.calls) == len(pieces) >= 2  # every piece paid for again
    assert not any(p["from_checkpoint"] for p in pieces)


def test_policy_digest_covers_the_generation_settings(monkeypatch) -> None:
    base = pt.transcription_policy_digest("m")
    assert pt.transcription_policy_digest("m") == base
    assert pt.transcription_policy_digest("other") != base
    for name, value in [
        ("PIECE_PROMPT_VERSION", "2"),
        ("MAX_OUTPUT_TOKENS", 1),
        ("THINKING_LEVEL", "HIGH"),
        ("TEMPERATURE", 1),
        ("ENCODE_BITRATE", "32k"),
        ("ENCODE_CHANNELS", 2),
        ("PIECE_TARGET_S", 1.0),
        ("PIECE_SEARCH_S", 1.0),
        ("CONTEXT_LINES", 1),
        ("_PIECE_PROMPT", pt._PIECE_PROMPT + " changed"),
    ]:
        with monkeypatch.context() as mp:
            mp.setattr(pt, name, value)
            assert pt.transcription_policy_digest("m") != base, name


def test_description_change_retranscribes_despite_checkpoints(audio, tmp_path) -> None:
    _run(audio, tmp_path, FakeTranscriber())
    again = FakeTranscriber()
    _run(audio, tmp_path, again, description="new guest")
    assert len(again.calls) == 3  # the prompt text changed, so every key did


def test_checkpoint_failing_the_current_word_rate_floor_is_a_miss(
    audio, tmp_path, monkeypatch
) -> None:
    _run(audio, tmp_path, FakeTranscriber())
    (tmp_path / "transcript.txt").unlink()
    (tmp_path / "transcript.json").unlink()
    monkeypatch.setattr(pt, "MIN_WORDS_PER_MIN", 200)  # cached 150 wpm now fails
    again = FakeTranscriber()
    with pytest.raises(pt.TranscriptionError, match="words/min"):
        _run(audio, tmp_path, again)
    assert len(again.calls) == 1  # piece 0 was re-asked, not served from disk


def test_corrupt_checkpoint_is_a_miss(audio, tmp_path) -> None:
    with pytest.raises(pt.TranscriptionError):
        _run(audio, tmp_path, FakeTranscriber(fail_piece=2, exc=_err("asr_error", 400)))
    (tmp_path / "pieces" / "0001.json").write_text("{trunc")
    again = FakeTranscriber()
    again.accepted = 1
    _run(audio, tmp_path, again)
    assert len(again.calls) == 2  # piece 1 (corrupt) and piece 2


def test_checkpoint_not_written_for_refused_piece(audio, tmp_path) -> None:
    with pytest.raises(pt.TranscriptionError):
        _run(audio, tmp_path, FakeTranscriber(words_per_piece=100))
    assert not list((tmp_path / "pieces").glob("*.json"))


# --- speaker list ---------------------------------------------------------------


def test_speakers_strip_markdown_labels() -> None:
    names, _ = pt._speakers_in("**Matt Yglesias:** hi\n__Jerusalem Demsas__: yo", False)
    assert names == ["Matt Yglesias", "Jerusalem Demsas"]


def test_speakers_reject_continuation_lines() -> None:
    text = (
        "And here's the thing: it matters\n"
        "However the data show: nothing\n"
        "Note that this is: odd\n"
        "Ludwig van Beethoven: hello\n"
        "Someone Else Entirely Wrong Here: nope\n"
    )
    names, _ = pt._speakers_in(text, False)
    assert names == ["Ludwig van Beethoven"]


def test_speakers_sponsor_state_crosses_pieces() -> None:
    _, open_after = pt._speakers_in("Matt Yglesias: hi\n[SPONSOR]\nBrand: buy", False)
    assert open_after is True
    names, open_after = pt._speakers_in(
        "Acme Corp: more ad\n[/SPONSOR]\nJerusalem Demsas: hi", True
    )
    assert names == ["Jerusalem Demsas"] and open_after is False


def test_hosts_are_not_presented_as_identified_speakers(audio, tmp_path) -> None:
    """Hosts are possible participants (the "Hosts:" line), not voices already
    matched: a piece that only produced Unknown speaker turns identified nobody,
    so the next prompt must carry no names list at all."""

    class OnlyUnknown:
        def __init__(self):
            self.prompts = []

        def __call__(self, a, prompt):
            self.prompts.append(prompt)
            lines = [f"Unknown speaker A: {'mumble ' * 40}" for _ in range(40)]
            return pt.PieceResult("\n".join(lines), "STOP", None, None)

    fake = OnlyUnknown()
    _run(audio, tmp_path, fake, hosts=("Matt Yglesias", "Jerusalem Demsas"))
    assert "Hosts: Matt Yglesias, Jerusalem Demsas." in fake.prompts[1]
    assert "Context from the end" in fake.prompts[1]
    assert NAMES_LABEL not in fake.prompts[1]
    assert "Names already used" not in fake.prompts[1]
    context = fake.prompts[1].split("Context from the end")[1]
    assert "Matt Yglesias" not in context and "Jerusalem Demsas" not in context


def test_names_list_holds_only_names_seen_in_earlier_output(audio, tmp_path) -> None:
    class Labelled:
        def __init__(self):
            self.prompts = []

        def __call__(self, a, prompt):
            i = len(self.prompts)
            self.prompts.append(prompt)
            who = "Ezra Klein" if i == 0 else "Unknown speaker A"
            return pt.PieceResult(f"{who}: {'word ' * 1500}", "STOP", None, None)

    fake = Labelled()
    _run(audio, tmp_path, fake, hosts=("Matt Yglesias",))
    assert f"{NAMES_LABEL}: Ezra Klein" in fake.prompts[1]
    assert "Matt Yglesias" not in fake.prompts[1].split(NAMES_LABEL)[1]
    assert "Ezra Klein, Ezra Klein" not in fake.prompts[2]


def test_prompt_mentions_cut_off_words() -> None:
    p = pt.build_piece_prompt(index=0, count=1, hosts=(), description="", context=None)
    flat = " ".join(p.split())
    assert "may be cut off mid-word; transcribe only what is audible" in flat
    assert flat.index("Never guess") < flat.index("cut off")


# --- end to end with real ffmpeg ---------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_end_to_end_real_ffmpeg(tmp_path, monkeypatch) -> None:
    mp3 = tmp_path / "tones.mp3"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=90",
            "-af", "volume='if(lt(mod(t,7),6),1,0)':eval=frame",
            "-ac", "1", str(mp3),
        ],
        check=True,
    )  # fmt: skip
    monkeypatch.setattr(pt, "PIECE_TARGET_S", 30.0)
    monkeypatch.setattr(pt, "PIECE_SEARCH_S", 5.0)
    fake = FakeTranscriber(words_per_piece=100)
    text = pt.transcribe_episode(
        mp3,
        tmp_path / "work",
        rss_duration_s=90,
        description="",
        hosts=("Jerusalem Demsas",),
        transcriber=fake,
        sleep=lambda s: None,
    )
    m = json.loads((tmp_path / "work" / "transcript.json").read_text())
    pieces = m["pieces"]
    assert len(pieces) == len(fake.calls) == 3
    assert m["decoded_duration_s"] == pytest.approx(90, abs=1.5)
    assert pieces[0]["start_s"] == 0
    for a, b in zip(pieces, pieces[1:], strict=False):
        assert a["end_s"] == b["start_s"]
    assert pieces[-1]["end_s"] == pytest.approx(m["decoded_duration_s"], abs=0.01)
    for audio_bytes, p in zip(fake.audio, pieces, strict=True):
        assert audio_bytes[:3] == b"ID3" or (
            audio_bytes[0] == 0xFF and audio_bytes[1] & 0xE0 == 0xE0
        )
        assert len(audio_bytes) == p["bytes"] < pt.MAX_PIECE_BYTES
    assert text.index("piece0") < text.index("piece2")


# --- GeminiPieceTranscriber -------------------------------------------------
# Responses are real google.genai types, not look-alikes.


def _resp(
    *parts: types.Part,
    finish: types.FinishReason | None = types.FinishReason.STOP,
    usage: tuple[int, int] | None = None,
    block: types.BlockedReason | None = None,
    no_candidates: bool = False,
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=None
        if no_candidates
        else [
            types.Candidate(
                finish_reason=finish,
                content=types.Content(parts=list(parts)) if parts else None,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=usage[0], candidates_token_count=usage[1]
        )
        if usage
        else None,
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=block)
        if block
        else None,
    )


def _text(t: str, thought: bool = False) -> types.Part:
    return types.Part(text=t, thought=thought or None)


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


def _call(monkeypatch, resp, seen=None) -> pt.PieceResult:
    client = _client_returning(resp, seen)
    monkeypatch.setattr("pipeline.tts.asr._make_genai_client", lambda timeout_s: client)
    return pt.GeminiPieceTranscriber()(b"mp3", "prompt")


def test_gemini_transcriber_refuses_non_stop(monkeypatch) -> None:
    resp = _resp(_text("Jerusalem Demsas: hi"), finish=types.FinishReason.MAX_TOKENS)
    with pytest.raises(pt.TranscriptionError, match="MAX_TOKENS") as ei:
        _call(monkeypatch, resp)
    assert ei.value.reason == "asr_incomplete"


def test_gemini_transcriber_missing_finish_reason_is_incomplete(monkeypatch) -> None:
    with pytest.raises(pt.TranscriptionError, match="asr_incomplete") as ei:
        _call(monkeypatch, _resp(_text("Jerusalem Demsas: hi"), finish=None))
    assert ei.value.reason == "asr_incomplete"


def test_gemini_transcriber_success_and_call_shape(monkeypatch) -> None:
    seen: dict = {}
    r = _call(monkeypatch, _resp(_text("Jerusalem Demsas: hi"), usage=(11, 7)), seen)
    assert r == pt.PieceResult("Jerusalem Demsas: hi", "STOP", 11, 7)
    assert seen["model"] == pt.ASR_MODEL
    cfg = seen["config"]
    assert cfg.temperature == 0
    assert cfg.max_output_tokens == 16384
    assert cfg.thinking_config.thinking_level.name == "LOW"
    assert seen["contents"][1] == "prompt"


def test_gemini_config_is_derived_from_the_policy_constants(monkeypatch) -> None:
    """The SDK config and the policy digest read the same constants, so a knob
    cannot change what is sent without also changing what is cached under."""
    monkeypatch.setattr(pt, "TEMPERATURE", 0.25)
    monkeypatch.setattr(pt, "MAX_OUTPUT_TOKENS", 1234)
    monkeypatch.setattr(pt, "THINKING_LEVEL", "HIGH")
    seen: dict = {}
    _call(monkeypatch, _resp(_text("Jerusalem Demsas: hi")), seen)
    cfg = seen["config"]
    assert cfg.temperature == 0.25
    assert cfg.max_output_tokens == 1234
    assert cfg.thinking_config.thinking_level.name == "HIGH"


def test_gemini_transcriber_usage_absent_is_none(monkeypatch) -> None:
    r = _call(monkeypatch, _resp(_text("Jerusalem Demsas: hi")))
    assert (r.input_tokens, r.output_tokens) == (None, None)


def test_gemini_transcriber_excludes_thought_parts(monkeypatch) -> None:
    resp = _resp(_text("I should label speakers.", thought=True), _text("Matt: hi"))
    assert _call(monkeypatch, resp).text == "Matt: hi"


@pytest.mark.parametrize(
    "resp",
    [
        _resp(),  # candidate with no content/parts: .text is None
        _resp(_text("only thinking", thought=True)),
        _resp(_text("  ")),
    ],
)
def test_gemini_transcriber_no_text_is_asr_empty(monkeypatch, resp) -> None:
    with pytest.raises(pt.TranscriptionError, match="asr_empty") as ei:
        _call(monkeypatch, resp)
    assert ei.value.reason == "asr_empty"


def test_gemini_transcriber_block_other_is_blocked(monkeypatch) -> None:
    resp = _resp(no_candidates=True, block=types.BlockedReason.OTHER)
    with pytest.raises(pt.TranscriptionError, match="asr_blocked.*OTHER") as ei:
        _call(monkeypatch, resp)
    assert ei.value.reason == "asr_blocked"


def test_gemini_transcriber_other_block_reasons_are_empty(monkeypatch) -> None:
    resp = _resp(no_candidates=True, block=types.BlockedReason.SAFETY)
    with pytest.raises(pt.TranscriptionError, match="asr_empty.*SAFETY") as ei:
        _call(monkeypatch, resp)
    assert ei.value.reason == "asr_empty"


def test_gemini_transcriber_no_candidates_no_feedback(monkeypatch) -> None:
    with pytest.raises(pt.TranscriptionError) as ei:
        _call(monkeypatch, _resp(no_candidates=True))
    assert ei.value.reason == "asr_empty"


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), TimeoutError("slow")],
)
def test_gemini_transcriber_timeouts(monkeypatch, exc) -> None:
    with pytest.raises(pt.TranscriptionError, match="asr_timeout") as ei:
        _call(monkeypatch, exc)
    assert ei.value.reason == "asr_timeout"


def test_gemini_transcriber_api_error_carries_code_and_status(monkeypatch) -> None:
    exc = genai_errors.APIError(
        429, {"error": {"message": "slow down", "status": "RESOURCE_EXHAUSTED"}}
    )
    with pytest.raises(pt.TranscriptionError) as ei:
        _call(monkeypatch, exc)
    assert ei.value.reason == "asr_error"
    assert ei.value.code == 429
    msg = str(ei.value)
    assert "429" in msg and "RESOURCE_EXHAUSTED" in msg and "APIError" in msg


def test_gemini_transcriber_other_errors_name_the_type(monkeypatch) -> None:
    with pytest.raises(pt.TranscriptionError) as ei:
        _call(monkeypatch, ConnectionError("reset by peer"))
    assert ei.value.reason == "asr_error" and ei.value.code is None
    assert "ConnectionError: reset by peer" in str(ei.value)


def test_gemini_transcriber_missing_api_key(monkeypatch) -> None:
    def no_key(timeout_s):
        raise KeyError("GEMINI_API_KEY")

    monkeypatch.setattr("pipeline.tts.asr._make_genai_client", no_key)
    with pytest.raises(pt.TranscriptionError, match="asr_error"):
        pt.GeminiPieceTranscriber()(b"mp3", "prompt")


def test_logs_one_info_line_per_piece(audio, tmp_path, caplog) -> None:
    first = FakeTranscriber(fail_piece=1, exc=_err("asr_error", 400))
    with caplog.at_level("INFO", logger=pt.logger.name):
        with pytest.raises(pt.TranscriptionError):
            _run(audio, tmp_path, first)
        second = FakeTranscriber()
        second.accepted = 1
        caplog.clear()
        _run(audio, tmp_path, second)
    msgs = [r.getMessage() for r in caplog.records if "piece " in r.getMessage()]
    count = len(_manifest(tmp_path)["pieces"])
    assert len(msgs) == count
    assert msgs[0].startswith(f"piece 1/{count}:") and "checkpoint" in msgs[0]
    assert msgs[1].startswith(f"piece 2/{count}:") and "attempt" in msgs[1]
    assert all("words" in m for m in msgs)


def test_logs_transcript_reuse(audio, tmp_path, caplog) -> None:
    _run(audio, tmp_path, FakeTranscriber())
    with caplog.at_level("INFO", logger=pt.logger.name):
        _run(audio, tmp_path, FakeTranscriber())
    assert any("reusing" in r.getMessage() for r in caplog.records)
