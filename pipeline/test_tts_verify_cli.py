"""Tests for the `tts-verify` CLI command. Everything runs offline."""

from __future__ import annotations

import hashlib
import json
import os
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
    monkeypatch.setattr("pipeline.tts.segment.decode_to_pcm_with_log", boom)


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


def install_fake(monkeypatch, results, decode_log: str = "") -> FakeTranscriberFactory:
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
        "pipeline.tts.segment.decode_to_pcm_with_log",
        lambda path: (bytes(SEGMENT_PCM_BYTES), decode_log),
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

    monkeypatch.setattr("pipeline.tts.segment.decode_to_pcm_with_log", bad_decode)

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
    monkeypatch.setattr("pipeline.tts.segment.decode_to_pcm_with_log", boom)

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
    assert "4 unexpected error" in flat
    assert "estimate" in flat.lower()


def args_for(audio_file, script_file, *extra):
    return ["--audio", str(audio_file), "--script", str(script_file), *map(str, extra)]


def clean_transcript(tmp_path) -> Path:
    p = tmp_path / "clean.txt"
    p.write_text(as_asr(SCRIPT))
    return p


def asr_run(tmp_path, script_file, audio_file, monkeypatch, *extra, decode_log=""):
    texts = thirds(as_asr(SCRIPT))
    fake = install_fake(monkeypatch, [transcription(t) for t in texts], decode_log)
    return fake, run(args_for(audio_file, script_file, *extra))


# --- I1: a crash is never an omission ---------------------------------------


def test_missing_ffmpeg_is_unavailable_not_omission(
    script_file, audio_file, monkeypatch
):
    def no_ffmpeg(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "ffmpeg")

    monkeypatch.setattr("pipeline.tts.segment.subprocess.run", no_ffmpeg)

    result = run(args_for(audio_file, script_file))

    assert result.exit_code == 3, result.output
    report = report_of(result)
    assert report["status"] == "unavailable"
    assert report["reasons"] == ["audio_decode_error"]
    assert "not runnable" in report["detail"]


def test_unexpected_crash_exits_4_with_traceback(
    tmp_path, script_file, audio_file, no_asr, monkeypatch
):
    def boom(*args, **kwargs):
        raise ValueError("kaboom in the aligner")

    monkeypatch.setattr("pipeline.tts.verify.project_chunks", boom)
    transcript = clean_transcript(tmp_path)

    result = run(args_for(audio_file, script_file, "--transcript", transcript))

    assert result.exit_code == 4
    assert "Traceback" in result.stderr and "kaboom in the aligner" in result.stderr


def test_unexpected_asr_crash_exits_4(script_file, audio_file, monkeypatch):
    # Anything other than TranscriptionUnavailable out of the transcriber is a
    # bug, and must not be scored as a finding.
    install_fake(monkeypatch, [KeyError("surprise")])

    result = run(args_for(audio_file, script_file))

    assert result.exit_code == 4
    assert "surprise" in result.stderr


# --- I2: output paths, overwrite, atomic early save -------------------------


def test_missing_output_parent_fails_before_any_work(
    tmp_path, script_file, audio_file, no_asr
):
    for flag in ("--save-transcript", "--json"):
        result = run(
            args_for(audio_file, script_file, flag, tmp_path / "nope" / "out.json")
        )
        assert result.exit_code == 2, (flag, result.output)
        assert "nope" in result.output


def test_existing_save_transcript_needs_force(
    tmp_path, script_file, audio_file, monkeypatch
):
    out = tmp_path / "out.json"
    out.write_text("precious")
    fake, result = asr_run(
        tmp_path, script_file, audio_file, monkeypatch, "--save-transcript", out
    )

    assert result.exit_code == 2
    assert "--force" in result.output
    assert out.read_text() == "precious"
    assert fake.mimes == []  # refused before spending any ASR


def test_force_overwrites_saved_transcript(
    tmp_path, script_file, audio_file, monkeypatch
):
    out = tmp_path / "out.json"
    out.write_text("old")
    _, result = asr_run(
        tmp_path,
        script_file,
        audio_file,
        monkeypatch,
        "--save-transcript",
        out,
        "--force",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["audio_sha256"] == sha(audio_file.read_bytes())


def test_transcript_saved_before_analysis_and_atomically(
    tmp_path, script_file, audio_file, monkeypatch
):
    def boom(*args, **kwargs):
        raise RuntimeError("analysis died")

    monkeypatch.setattr("pipeline.tts.verify.project_chunks", boom)
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr("pipeline.__main__.os.replace", spy)
    out = tmp_path / "out.json"
    _, result = asr_run(
        tmp_path, script_file, audio_file, monkeypatch, "--save-transcript", out
    )

    assert result.exit_code == 4
    # The paid-for transcript survived the crash, and arrived via tmp + replace.
    assert len(json.loads(out.read_text())["segments"]) == 3
    assert replaced == [(str(out) + ".tmp", str(out))]
    assert not Path(str(out) + ".tmp").exists()


# --- replay integrity --------------------------------------------------------


def saved_transcript(tmp_path, script_file, audio_file, monkeypatch) -> Path:
    out = tmp_path / "out.json"
    _, result = asr_run(
        tmp_path, script_file, audio_file, monkeypatch, "--save-transcript", out
    )
    assert result.exit_code == 0, result.output
    return out


def test_saved_transcript_records_script_sha(
    tmp_path, script_file, audio_file, monkeypatch
):
    out = saved_transcript(tmp_path, script_file, audio_file, monkeypatch)
    assert json.loads(out.read_text())["script_sha256"] == sha(SCRIPT.encode())


def test_replay_against_same_script_matches(
    tmp_path, script_file, audio_file, monkeypatch
):
    out = saved_transcript(tmp_path, script_file, audio_file, monkeypatch)

    result = run(args_for(audio_file, script_file, "--transcript", out))

    assert result.exit_code == 0, result.output
    asr_block = report_of(result)["asr"]
    assert asr_block["saved_script_sha256"] == sha(SCRIPT.encode())
    assert asr_block["script_matches_saved"] is True
    assert "WARNING" not in result.stderr


def test_replay_against_edited_script_warns_but_runs(
    tmp_path, script_file, audio_file, monkeypatch
):
    out = saved_transcript(tmp_path, script_file, audio_file, monkeypatch)
    edited = tmp_path / "edited.txt"
    edited.write_text(SCRIPT + " Extra closing line here.")

    result = run(args_for(audio_file, edited, "--transcript", out))

    assert result.exit_code == 0, result.output
    asr_block = report_of(result)["asr"]
    assert asr_block["script_matches_saved"] is False
    assert asr_block["saved_script_sha256"] == sha(SCRIPT.encode())
    assert "WARNING" in result.stderr and "script" in result.stderr


def test_replay_of_older_file_without_script_sha_is_null(
    tmp_path, script_file, audio_file, monkeypatch
):
    out = saved_transcript(tmp_path, script_file, audio_file, monkeypatch)
    data = json.loads(out.read_text())
    del data["script_sha256"]
    out.write_text(json.dumps(data))

    result = run(args_for(audio_file, script_file, "--transcript", out))

    assert result.exit_code == 0, result.output
    asr_block = report_of(result)["asr"]
    assert asr_block["script_matches_saved"] is None
    assert asr_block["saved_script_sha256"] is None
    assert "WARNING" not in result.stderr


def test_saved_json_is_recognised_under_any_extension(
    tmp_path, script_file, audio_file, monkeypatch
):
    out = saved_transcript(tmp_path, script_file, audio_file, monkeypatch)
    renamed = tmp_path / "transcript.txt"
    renamed.write_text(out.read_text())

    result = run(args_for(audio_file, script_file, "--transcript", renamed))
    assert result.exit_code == 0, result.output
    assert report_of(result)["asr"]["source"] == "saved"

    other = tmp_path / "other.mp3"
    other.write_bytes(b"not the same audio")
    wrong = run(args_for(other, script_file, "--transcript", renamed))
    assert wrong.exit_code == 2 and "audio_sha256" in wrong.output


def test_plain_text_that_is_not_saved_json_stays_external(
    tmp_path, script_file, audio_file, no_asr
):
    lookalike = tmp_path / "t.txt"
    lookalike.write_text('{"not": "a saved transcript"}')

    result = run(args_for(audio_file, script_file, "--transcript", lookalike))

    assert report_of(result)["asr"]["source"] == "external"


# --- report contents ---------------------------------------------------------


def test_asr_report_carries_provenance_and_chunk_estimates(
    tmp_path, script_file, audio_file, monkeypatch
):
    _, result = asr_run(
        tmp_path,
        script_file,
        audio_file,
        monkeypatch,
        decode_log="Header missing",
    )

    assert result.exit_code == 0, result.output
    report = report_of(result)
    assert report["audio_path"] == str(audio_file)
    assert report["script_path"] == str(script_file)
    assert report["audio_seconds"] == pytest.approx(700.0)
    assert report["decode_warnings"] == "Header missing"
    texts = thirds(as_asr(SCRIPT))
    assert report["transcript_sha256"] == sha("\n".join(texts).encode())
    starts = [c["est_start_s"] for c in report["chunks"]]
    assert starts[0] == 0.0
    assert starts == sorted(starts) and starts[-1] < 700.0


def test_external_transcript_has_no_audio_seconds_or_estimates(
    tmp_path, script_file, audio_file, no_asr
):
    transcript = clean_transcript(tmp_path)

    result = run(args_for(audio_file, script_file, "--transcript", transcript))

    report = report_of(result)
    assert report["audio_seconds"] is None
    assert all(c["est_start_s"] is None for c in report["chunks"])
    assert report["decode_warnings"] == ""
    assert report["transcript_sha256"] == sha(as_asr(SCRIPT).encode())


def test_replay_reports_audio_seconds_from_saved_segments(
    tmp_path, script_file, audio_file, monkeypatch
):
    out = saved_transcript(tmp_path, script_file, audio_file, monkeypatch)

    result = run(args_for(audio_file, script_file, "--transcript", out))

    report = report_of(result)
    assert report["audio_seconds"] == pytest.approx(700.0)
    assert report["chunks"][0]["est_start_s"] == 0.0


# --- encoding and option validation -----------------------------------------


def test_non_utf8_transcript_is_a_usage_error(
    tmp_path, script_file, audio_file, no_asr
):
    bad = tmp_path / "latin1.txt"
    bad.write_bytes("caf\xe9 au lait".encode("latin-1"))

    result = run(args_for(audio_file, script_file, "--transcript", bad))

    assert result.exit_code == 2
    assert "utf-8" in result.output.lower()


def test_non_utf8_script_is_a_usage_error(tmp_path, audio_file, no_asr):
    bad = tmp_path / "script.txt"
    bad.write_bytes(b"\xff\xfe broken")

    result = run(args_for(audio_file, bad, "--transcript", clean_transcript(tmp_path)))

    assert result.exit_code == 2
    assert "utf-8" in result.output.lower()


@pytest.mark.parametrize("value", ["0", "0.5", "-3"])
def test_timeout_must_be_at_least_one_second(value, script_file, audio_file, no_asr):
    result = run(args_for(audio_file, script_file, "--timeout", value))

    assert result.exit_code == 2
