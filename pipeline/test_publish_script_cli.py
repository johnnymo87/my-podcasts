from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from pipeline.__main__ import cli
from pipeline.tts import (
    FEED_VOICES,
    GeminiConfig,
    OpenAIConfig,
    RenderConfig,
    openai_config,
)


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
    assert call["notify_fallback"] is False  # a dry run pages nobody


def _dry_run_with(monkeypatch, tmp_path, config, *, rendered, fallback_reason=None):
    """Run ``publish-script --dry-run`` against a fake ``render_episode`` that
    reports ``rendered`` (and why, if it fell back)."""
    from pipeline.tts import RenderResult

    def fake(text, cfg, out_mp3, **kwargs):
        Path(out_mp3).write_bytes(b"\xff\xfb\x90\x00" * 10)
        return RenderResult(
            provider=rendered.provider,
            config=cfg,
            rendered=rendered,
            cached=False,
            chunks=1,
            manifest_path=None,
            fallback_reason=fallback_reason,
        )

    monkeypatch.setattr("pipeline.tts.render_episode", fake)
    monkeypatch.setitem(FEED_VOICES, "gemini-feed", config)
    script_file = tmp_path / "script.md"
    script_file.write_text("The episode body.", encoding="utf-8")
    return CliRunner().invoke(
        cli,
        [
            "publish-script",
            "--script-file",
            str(script_file),
            "--title",
            "Some Title",
            "--feed-slug",
            "gemini-feed",
            "--dry-run",
        ],
    )


_GEMINI = GeminiConfig(model="gemini-3.8-flash-lite-tts", voice="Kore")
_OPENAI = OpenAIConfig(model="tts-1-hd", voice="onyx")
_GEMINI_WITH_FALLBACK = RenderConfig(primary=_GEMINI, fallback=_OPENAI)


def test_dry_run_echoes_what_rendered_when_gemini_passed(tmp_path, monkeypatch):
    res = _dry_run_with(monkeypatch, tmp_path, _GEMINI_WITH_FALLBACK, rendered=_GEMINI)
    assert res.exit_code == 0, res.output
    assert "Rendered: gemini gemini-3.8-flash-lite-tts/Kore\n" in res.output
    assert "fell back" not in res.output


def test_dry_run_echoes_the_fallback_and_why(tmp_path, monkeypatch):
    res = _dry_run_with(
        monkeypatch,
        tmp_path,
        _GEMINI_WITH_FALLBACK,
        rendered=_OPENAI,
        fallback_reason="second_omission",
    )
    assert res.exit_code == 0, res.output
    assert (
        "Rendered: openai tts-1-hd/onyx (Gemini fell back: second_omission)\n"
        in res.output
    )
    # The "what will render" line before it still describes the request.
    assert (
        "Running TTS (dry run, gemini gemini-3.8-flash-lite-tts/Kore)..." in res.output
    )
