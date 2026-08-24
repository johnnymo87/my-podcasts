from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from pipeline.__main__ import cli


@pytest.fixture
def captured_tts_input(monkeypatch) -> list[str]:
    """Stub ttsjoin; return the list its captured input file lands in.

    Reads ``--input-file`` before the CLI's tempdir is torn down, and looks
    up flags by name rather than position, matching the fixture pattern in
    ``pipeline/test_processor_prelude.py``.
    """
    captured: list[str] = []

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            input_file = Path(cmd[cmd.index("--input-file") + 1])
            captured.append(input_file.read_text(encoding="utf-8"))
            output_file = Path(cmd[cmd.index("--output-file") + 1])
            output_file.write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    return captured


def test_dry_run_tts_input_opens_with_title(
    tmp_path, captured_tts_input: list[str]
) -> None:
    """publish-script --dry-run applies the same prelude as a real publish."""
    script_file = tmp_path / "script.md"
    script_file.write_text("This is the episode body.", encoding="utf-8")

    res = CliRunner().invoke(
        cli,
        [
            "publish-script",
            "--script-file",
            str(script_file),
            "--title",
            "Great Interview",
            "--feed-slug",
            "deep-dives",
            "--dry-run",
        ],
    )

    assert res.exit_code == 0, res.output
    assert len(captured_tts_input) == 1
    assert captured_tts_input[0] == "Great Interview.\n\nThis is the episode body."


def test_dry_run_skips_prelude_for_daily_digests(
    tmp_path, captured_tts_input: list[str]
) -> None:
    """The dry-run branch honors the same daily-digest guard as publish_script."""
    script_file = tmp_path / "script.md"
    body = "Good morning. It is Friday, and this is your daily briefing."
    script_file.write_text(body, encoding="utf-8")

    res = CliRunner().invoke(
        cli,
        [
            "publish-script",
            "--script-file",
            str(script_file),
            "--title",
            "2026-08-21 - The Rundown",
            "--feed-slug",
            "the-rundown",
            "--dry-run",
        ],
    )

    assert res.exit_code == 0, res.output
    assert len(captured_tts_input) == 1
    assert captured_tts_input[0] == body
