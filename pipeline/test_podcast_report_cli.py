from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pipeline import __main__ as main_mod
from pipeline import podcast_report
from pipeline.report_engine import ReportOutput


FIXTURE = Path(__file__).parent / "fixtures" / "argument_feed.xml"
GUID = "audioboom-01a13c3d-ae49-7326-a550-f82c9eff9be1"


class FakeTranscriber:
    instances: list[FakeTranscriber] = []

    def __init__(self) -> None:
        self.closed = False
        FakeTranscriber.instances.append(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def calls(monkeypatch, tmp_path):
    record: dict = {"download": [], "transcribe": [], "write": []}
    FakeTranscriber.instances = []

    def boom(*a, **k):
        raise AssertionError("DB / R2 must not be touched by a dry run")

    for target in (
        "pipeline.__main__.StateStore",
        "pipeline.__main__.R2Client",
        "pipeline.db.StateStore",
        "pipeline.r2.R2Client",
    ):
        monkeypatch.setattr(target, boom)

    def fake_download(url, dest, **kw):
        record["download"].append((url, dest))
        dest.write_bytes(b"mp3")
        return 3

    def fake_transcribe(audio_path, work_dir, **kw):
        record["transcribe"].append(kw)
        (work_dir / "transcript.txt").write_text("A: hi\nB: hello")
        (work_dir / "transcript.json").write_text(
            json.dumps(
                {
                    "pieces": [
                        {"input_tokens": 100, "output_tokens": 10},
                        {"input_tokens": 50, "output_tokens": None},
                    ]
                }
            )
        )
        return "A: hi\nB: hello"

    def fake_write(**kw):
        record["write"].append(kw)
        return ReportOutput(script="one two three four", summary="short summary")

    monkeypatch.setattr(podcast_report, "download_enclosure", fake_download)
    monkeypatch.setattr(podcast_report, "transcribe_episode", fake_transcribe)
    monkeypatch.setattr(podcast_report, "write_report", fake_write)
    monkeypatch.setattr(podcast_report, "GeminiPieceTranscriber", FakeTranscriber)
    return record


def _run(tmp_path, *extra, guid=GUID):
    return CliRunner().invoke(
        main_mod.cli,
        [
            "podcast-report",
            "dry-run",
            "--source",
            "argument",
            "--guid",
            guid,
            "--rss-file",
            str(FIXTURE),
            "--work-dir",
            str(tmp_path / "work"),
            *extra,
        ],
    )


def test_happy_path(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    result = _run(tmp_path)
    assert result.exit_code == 0, result.output
    work = tmp_path / "work"

    item = json.loads((work / "item.json").read_text())
    assert item["source"] == "argument"
    assert item["guid"] == GUID
    assert item["pub_date"].startswith("2026-10-15T07:00:00")
    assert (work / "script.txt").read_text() == "one two three four"
    assert (work / "summary.txt").read_text() == "short summary"
    assert not list(work.glob("*.tmp"))

    ((url, dest),) = calls["download"]
    assert dest == work / "audio.mp3"
    assert "8964415" in url
    t = calls["transcribe"][0]
    assert t["hosts"] == ("Jerusalem Demsas", "Matt Yglesias")
    assert t["rss_duration_s"] == 4807
    assert t["carry_context"] is True
    assert isinstance(t["transcriber"], FakeTranscriber)
    assert t["transcriber"].closed
    assert calls["write"][0]["title"] == "Canceling (and un-canceling) historic heroes"

    out = result.output
    assert str(work) in out
    assert str(work / "transcript.txt") in out
    assert str(work / "script.txt") in out
    assert "Script words: 4" in out
    assert "Pieces: 2, tokens in/out: 150/10" in out
    assert "download" in out and "transcribe" in out and "write" in out


def test_no_carry_context(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    result = _run(tmp_path, "--no-carry-context")
    assert result.exit_code == 0, result.output
    assert calls["transcribe"][0]["carry_context"] is False


def test_unknown_guid_fails(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    result = _run(tmp_path, guid="nope")
    assert result.exit_code != 0
    assert "not found" in result.output
    assert "3 items" in result.output
    assert calls["download"] == []
    assert not (tmp_path / "work").exists()


def test_missing_api_key_fails_before_download(tmp_path, calls):
    result = _run(tmp_path)
    assert result.exit_code != 0
    assert "GEMINI_API_KEY" in result.output
    assert calls["download"] == []
    assert not (tmp_path / "work").exists()


def test_unknown_source_fails(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    result = CliRunner().invoke(
        main_mod.cli,
        ["podcast-report", "dry-run", "--source", "nope", "--guid", GUID],
    )
    assert result.exit_code != 0
    assert "unknown podcast source" in result.output


def test_stage_failure_is_clean_error(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")

    def bad_write(**kw):
        raise RuntimeError("refused: too short")

    monkeypatch.setattr(podcast_report, "write_report", bad_write)
    result = _run(tmp_path)
    assert result.exit_code != 0
    assert "refused: too short" in result.output
    assert not (tmp_path / "work" / "script.txt").exists()
    assert FakeTranscriber.instances[0].closed


def test_default_work_dir_is_hash_of_guid():
    d = podcast_report.default_work_dir(GUID)
    assert d.parent == Path("/persist/my-podcasts/podcast-work/dry-run")
    assert len(d.name) == 16
