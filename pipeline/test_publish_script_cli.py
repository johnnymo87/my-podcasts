from __future__ import annotations

from click.testing import CliRunner

from pipeline.__main__ import cli
from pipeline.tts import openai_config


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


def test_dry_run_renders_with_voice_and_touches_no_state(
    tmp_path, fake_tts_render
) -> None:
    """--voice reaches the renderer; a dry run writes no manifest or cache."""
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
            "--voice",
            "ash",
            "--dry-run",
        ],
    )

    assert res.exit_code == 0, res.output
    [call] = fake_tts_render
    assert call["config"] == openai_config(model="tts-1-hd", voice="ash")
    assert call["feed_slug"] == "deep-dives"
    assert call["episode_id"] == "dry-run"
    assert call["manifest_dir"] is None
    assert call["cache_dir"] is None
