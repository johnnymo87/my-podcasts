"""Tests for the `tts-verify` CLI command. Everything runs offline."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from pipeline.__main__ import cli
from pipeline.tts import asr
from pipeline.tts.asr import Transcription, TranscriptionUnavailable


FIXTURE = Path(__file__).parent / "tts" / "fixtures" / "rundown_2026-09-30_chunks01.txt"
SCRIPT = FIXTURE.read_text()
SEGMENT_PCM_BYTES = 700 * 24_000 * 2  # 700 s of 24 kHz mono s16 -> 3 segments


def as_asr(text: str) -> str:
    """What a clean ASR pass looks like: no punctuation, digits for spelled numbers."""
    text = text.replace("September thirtieth, twenty twenty-six", "September 30th 2026")
    text = text.replace("twelve point nine billion dollars", "$12.9 billion")
    return re.sub(r"[^\w\s$.%-]", "", text)


def drop_words(text: str, start: int, count: int) -> str:
    words = text.split()
    return " ".join(words[:start] + words[start + count :])


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def thirds(text: str) -> list[str]:
    words = text.split()
    n = len(words) // 3
    return [
        " ".join(words[:n]),
        " ".join(words[n : 2 * n]),
        " ".join(words[2 * n :]),
    ]


def transcription(text: str) -> Transcription:
    return Transcription(
        text=text,
        model="fake-model",
        prompt_version="p1",
        finish_reason="STOP",
        elapsed_s=1.5,
        input_tokens=100,
        output_tokens=50,
        thinking_tokens=7,
    )


@pytest.fixture
def script_file(tmp_path: Path) -> Path:
    p = tmp_path / "script.txt"
    p.write_text(SCRIPT)
    return p


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    p = tmp_path / "episode.mp3"
    p.write_bytes(b"fake mp3 bytes")
    return p


@pytest.fixture
def no_asr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to reach Gemini or decode audio fails the test."""

    def boom(*args, **kwargs):
        raise AssertionError("ASR / decode must not be called")

    monkeypatch.setattr(asr, "_make_genai_client", boom)
    monkeypatch.setattr("pipeline.tts.segment.decode_to_pcm", boom)


class FakeTranscriberFactory:
    """Records what the CLI did to the patched ``asr.GeminiTranscriber``."""

    def __init__(self, results: list) -> None:
        self.results = list(results)
        self.mimes: list[str] = []
        self.sizes: list[int] = []
        self.timeouts: list[float] = []
        self.closed = 0

    def transcribe(self, audio: bytes, mime_type: str) -> Transcription:
        self.mimes.append(mime_type)
        self.sizes.append(len(audio))
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def install_fake(monkeypatch, results) -> FakeTranscriberFactory:
    factory = FakeTranscriberFactory(results)

    class Fake:
        def __init__(self, *, timeout_s: float = 0.0, **kwargs) -> None:
            factory.timeouts.append(timeout_s)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> None:
            factory.closed += 1

        def __call__(self, audio: bytes, mime_type: str) -> Transcription:
            return factory.transcribe(audio, mime_type)

    monkeypatch.setattr(asr, "GeminiTranscriber", Fake)
    monkeypatch.setattr(
        "pipeline.tts.segment.decode_to_pcm", lambda path: bytes(SEGMENT_PCM_BYTES)
    )
    return factory


def run(args: list[str]):
    return CliRunner().invoke(cli, ["tts-verify", *args])


def report_of(result) -> dict:
    return json.loads(result.stdout)


# 1
def test_clean_external_transcript_passes(tmp_path, script_file, audio_file, no_asr):
    transcript = tmp_path / "clean.txt"
    transcript.write_text(as_asr(SCRIPT))

    result = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--transcript",
            str(transcript),
        ]
    )

    assert result.exit_code == 0, result.output
    report = report_of(result)
    assert report["status"] == "pass"
    assert report["mode"] == "projected"
    assert report["asr"]["source"] == "external"
    assert report["chunks"]
    assert all(c["recall"] >= 0.99 for c in report["chunks"])
    assert report["audio_sha256"] == sha(audio_file.read_bytes())
    assert report["script_sha256"] == sha(SCRIPT.encode())
    assert report["thresholds"]["anchor_min"] == 3
    assert report["verifier_version"]
    assert report["analysis"]["status"] == "pass"
    # One-line human summary goes to stderr, JSON alone on stdout.
    assert "pass" in result.stderr.lower()


# 2
def test_skipped_passage_is_an_omission(tmp_path, script_file, audio_file, no_asr):
    transcript = tmp_path / "skipped.txt"
    words = as_asr(SCRIPT).split()
    transcript.write_text(drop_words(as_asr(SCRIPT), len(words) // 2, 40))

    result = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--transcript",
            str(transcript),
        ]
    )

    assert result.exit_code == 1, result.output
    report = report_of(result)
    assert report["status"] == "omission"
    assert "long_unmatched_span" in report["reasons"]


# 3
def test_asr_path_segments_and_saves_replayable_transcript(
    tmp_path, script_file, audio_file, monkeypatch
):
    texts = thirds(as_asr(SCRIPT))
    fake = install_fake(monkeypatch, [transcription(t) for t in texts])
    out = tmp_path / "out.json"

    result = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--save-transcript",
            str(out),
            "--timeout",
            "33",
        ]
    )

    assert result.exit_code == 0, result.output
    report = report_of(result)
    assert report["status"] == "pass"
    assert report["asr"]["source"] == "asr"
    assert report["asr"]["model"] == "fake-model"
    assert report["asr"]["prompt_version"] == "p1"
    assert len(report["asr"]["segments"]) == 3
    seg0 = report["asr"]["segments"][0]
    assert "text" not in seg0
    assert seg0["thinking_tokens"] == 7 and seg0["finish_reason"] == "STOP"
    assert fake.closed == 1
    assert fake.mimes == ["audio/wav"] * 3
    assert fake.timeouts == [33.0]
    assert all(size > 44 for size in fake.sizes)  # a WAV, not bare PCM

    saved = json.loads(out.read_text())
    assert saved["audio_sha256"] == sha(audio_file.read_bytes())
    assert saved["model"] == "fake-model"
    assert saved["prompt_version"] == "p1"
    assert len(saved["segments"]) == 3
    assert "\n".join(s["text"] for s in saved["segments"]) == "\n".join(texts)
    first, second, third = saved["segments"]
    assert first["start_s"] == 0.0 and third["end_s"] == pytest.approx(700.0)
    assert first["end_s"] == second["start_s"] and second["end_s"] == third["start_s"]
    for key in (
        "finish_reason",
        "elapsed_s",
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
    ):
        assert key in first


# 4
def test_unavailable_segment_makes_whole_report_unavailable(
    tmp_path, script_file, audio_file, monkeypatch
):
    texts = thirds(as_asr(SCRIPT))
    fake = install_fake(
        monkeypatch,
        [
            transcription(texts[0]),
            TranscriptionUnavailable("asr_incomplete", "finish_reason=MAX_TOKENS"),
        ],
    )
    out = tmp_path / "out.json"

    result = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--save-transcript",
            str(out),
        ]
    )

    assert result.exit_code == 3, result.output
    report = report_of(result)
    assert report["status"] == "unavailable"
    assert report["reasons"] == ["asr_incomplete"]
    assert "segment 2 of 3" in report["detail"]
    assert report["analysis"] is None
    assert report["mode"] == "projected"
    assert not out.exists()
    assert fake.closed == 1
    assert len(fake.mimes) == 2  # stopped at the failure


def test_decode_failure_is_unavailable_not_omission(
    script_file, audio_file, monkeypatch
):
    def bad_decode(path):
        raise RuntimeError("ffmpeg decode failed: bad")

    monkeypatch.setattr("pipeline.tts.segment.decode_to_pcm", bad_decode)

    result = run(["--audio", str(audio_file), "--script", str(script_file)])

    assert result.exit_code == 3, result.output
    report = report_of(result)
    assert report["status"] == "unavailable"
    assert report["reasons"] == ["audio_decode_error"]
    assert "bad" in report["detail"]


# 5
def test_saved_transcript_replays_and_is_bound_to_its_audio(
    tmp_path, script_file, audio_file, monkeypatch
):
    texts = thirds(as_asr(SCRIPT))
    install_fake(monkeypatch, [transcription(t) for t in texts])
    out = tmp_path / "out.json"
    first = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--save-transcript",
            str(out),
        ]
    )
    assert first.exit_code == 0, first.output

    # Replay: no ASR, no decode.
    def boom(*args, **kwargs):
        raise AssertionError("replay must not call ASR or decode")

    monkeypatch.setattr(asr, "GeminiTranscriber", boom)
    monkeypatch.setattr("pipeline.tts.segment.decode_to_pcm", boom)

    replay = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--transcript",
            str(out),
        ]
    )
    assert replay.exit_code == 0, replay.output
    report = report_of(replay)
    assert report["asr"]["source"] == "saved"
    assert report["asr"]["model"] == "fake-model"
    assert len(report["asr"]["segments"]) == 3
    assert "text" not in report["asr"]["segments"][0]

    other = tmp_path / "other.mp3"
    other.write_bytes(b"a different recording")
    wrong = run(
        [
            "--audio",
            str(other),
            "--script",
            str(script_file),
            "--transcript",
            str(out),
        ]
    )
    assert wrong.exit_code != 0
    assert "audio_sha256" in wrong.output


def test_json_transcript_not_from_save_transcript_is_rejected(
    tmp_path, script_file, audio_file, no_asr
):
    bogus = tmp_path / "bogus.json"
    bogus.write_text(json.dumps({"hello": "world"}))

    result = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--transcript",
            str(bogus),
        ]
    )

    assert result.exit_code == 2
    assert "save-transcript" in result.output


def test_empty_transcript_is_unavailable_not_omission(
    tmp_path, script_file, audio_file, no_asr
):
    transcript = tmp_path / "empty.txt"
    transcript.write_text("  ... \n")

    result = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--transcript",
            str(transcript),
        ]
    )

    assert result.exit_code == 3, result.output
    report = report_of(result)
    assert report["status"] == "unavailable"
    assert report["reasons"] == ["asr_empty"]


# 6
def test_invalid_threshold_is_a_usage_error(tmp_path, script_file, audio_file, no_asr):
    transcript = tmp_path / "clean.txt"
    transcript.write_text(as_asr(SCRIPT))

    result = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--transcript",
            str(transcript),
            "--recall-floor",
            "1.5",
        ]
    )

    assert result.exit_code == 2
    assert "recall_floor" in result.output


def test_threshold_overrides_are_applied_and_echoed(
    tmp_path, script_file, audio_file, no_asr
):
    transcript = tmp_path / "clean.txt"
    transcript.write_text(as_asr(SCRIPT))

    result = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--transcript",
            str(transcript),
            "--anchor-min",
            "4",
            "--min-span-words",
            "20",
            "--max-span-ratio",
            "0.25",
            "--recall-floor",
            "0.9",
        ]
    )

    assert result.exit_code == 0, result.output
    assert report_of(result)["thresholds"] == {
        "anchor_min": 4,
        "min_span_words": 20,
        "max_span_ratio": 0.25,
        "recall_floor": 0.9,
    }


# 7
def test_transcript_and_save_transcript_conflict(
    tmp_path, script_file, audio_file, no_asr
):
    transcript = tmp_path / "x.txt"
    transcript.write_text(as_asr(SCRIPT))

    result = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--transcript",
            str(transcript),
            "--save-transcript",
            str(tmp_path / "y.json"),
        ]
    )

    assert result.exit_code == 2
    assert not (tmp_path / "y.json").exists()


def test_json_option_writes_report_file_and_keeps_stdout_clean(
    tmp_path, script_file, audio_file, no_asr
):
    transcript = tmp_path / "clean.txt"
    transcript.write_text(as_asr(SCRIPT))
    report_path = tmp_path / "report.json"

    result = run(
        [
            "--audio",
            str(audio_file),
            "--script",
            str(script_file),
            "--transcript",
            str(transcript),
            "--json",
            str(report_path),
        ]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == ""
    assert json.loads(report_path.read_text())["status"] == "pass"
    assert "pass" in result.stderr.lower()


def test_help_states_projection_caveat():
    result = CliRunner().invoke(cli, ["tts-verify", "--help"])

    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    assert "projected" in flat.lower()
    assert "whole-episode alignment" in flat
    assert "3 verification unavailable" in flat
