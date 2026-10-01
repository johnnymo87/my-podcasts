"""FEED_VOICES is the single owner of per-feed TTS settings.

The golden tables below are HARD-CODED from the values in force before
call sites were migrated to ``tts.resolve_render_config`` -- they are a
regression net for "production audio does not change", so they must never be
derived from ``FEED_VOICES`` itself.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from pipeline import fp_processor, things_happen_processor
from pipeline.__main__ import cli
from pipeline.blog_sources import BLOG_SOURCES, BlogSource
from pipeline.db import StateStore
from pipeline.presets import DEFAULT_PRESET, PRESETS, NewsletterPreset
from pipeline.processor import process_email_bytes
from pipeline.script_processor import publish_script
from pipeline.tts import (
    FEED_VOICES,
    GeminiConfig,
    OpenAIConfig,
    RenderConfig,
    openai_config,
    resolve_render_config,
)
from pipeline.tts.config import DEFAULT_RENDER_CONFIG


@pytest.fixture(autouse=True)
def _stub_probe_and_feed(monkeypatch):
    monkeypatch.setattr(
        "pipeline.processor.regenerate_and_upload_feed", lambda store, r2_client: None
    )
    monkeypatch.setattr(
        "pipeline.script_processor.regenerate_and_upload_feed", lambda s, r: None
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="60.0\n"),
    )
    monkeypatch.delenv("TTS_MODEL", raising=False)
    monkeypatch.delenv("TTS_VOICE", raising=False)


_EMAIL = b"""\
Date: Mon, 17 Aug 2026 08:00:00 +0000
Subject: Money Stuff: Goat Hedge
Content-Type: text/html; charset="UTF-8"
MIME-Version: 1.0

<html><body><p>A private equity fund bought a herd of goats.</p></body></html>
"""


def _routable_slugs() -> set[str]:
    return {
        *(p.feed_slug for p in PRESETS),
        DEFAULT_PRESET.feed_slug,
        *(s.feed_slug for s in BLOG_SOURCES),
        fp_processor.FEED_SLUG,
        things_happen_processor.FEED_SLUG,
    }


def test_every_routable_feed_slug_has_an_explicit_feed_voices_entry() -> None:
    """A configured feed must never silently fall through to the default."""
    assert _routable_slugs() - set(FEED_VOICES) == set()


def test_presets_and_blog_sources_no_longer_carry_voice_fields() -> None:
    for cls in (NewsletterPreset, BlogSource):
        names = {f.name for f in dataclasses.fields(cls)}
        assert not names & {"tts_voice", "tts_model"}, cls


def _email(tmp_path: Path, route_tag: str | None) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    try:
        process_email_bytes(
            raw_email=_EMAIL,
            source_r2_key="raw/test.eml",
            route_tag=route_tag,
            store=store,
            r2_client=MagicMock(),
            levine_cache_dir=tmp_path / "levine-cache",
        )
    finally:
        store.close()


# (route tag, feed slug the email lands in, voice before this PR)
_EMAIL_GOLDEN = [
    ("levine", "levine", "ash"),
    ("yglesias", "yglesias", "shimmer"),
    ("silver", "silver", "echo"),
    ("the-rundown", "the-rundown", "nova"),
    ("fp-digest", "fp-digest", "onyx"),
    ("aaronson", "aaronson", "fable"),
    ("chinatalk", "chinatalk", "alloy"),
    ("no-such-route-tag", "general", "ash"),  # unknown tag -> general preset
    (None, "general", "ash"),
]


@pytest.mark.parametrize("route_tag,slug,voice", _EMAIL_GOLDEN)
def test_email_path_golden(tmp_path, fake_tts_render, route_tag, slug, voice) -> None:
    _email(tmp_path, route_tag)
    [call] = fake_tts_render
    assert call["config"] == openai_config(model="tts-1-hd", voice=voice)
    assert call["feed_slug"] == slug


def test_email_env_overrides_still_win(tmp_path, fake_tts_render, monkeypatch) -> None:
    """A voice-only override keeps the feed's model (tts-1-hd), not a default."""
    monkeypatch.setenv("TTS_VOICE", "shimmer")
    _email(tmp_path, "levine")
    [call] = fake_tts_render
    assert call["config"] == openai_config(model="tts-1-hd", voice="shimmer")


@pytest.mark.parametrize("var", ["TTS_VOICE", "TTS_MODEL"])
def test_email_path_empty_env_override_is_an_error(
    tmp_path, fake_tts_render, monkeypatch, var
) -> None:
    # Config is resolved before any LLM work: a bad override must fail first.
    def _must_not_run(**kwargs):
        raise AssertionError("transcript rewrite ran before config resolution")

    monkeypatch.setattr("pipeline.processor.maybe_rewrite_transcript", _must_not_run)
    monkeypatch.setenv(var, "")
    with pytest.raises(ValueError):
        _email(tmp_path, "levine")
    assert fake_tts_render == []


def test_pinned_slugs_are_what_we_think() -> None:
    assert things_happen_processor.FEED_SLUG == "the-rundown"
    assert fp_processor.FEED_SLUG == "fp-digest"
    assert BLOG_SOURCES[0].feed_slug == "aaronson"


def _publish(tmp_path: Path, feed_slug: str, **kwargs) -> None:
    script_file = tmp_path / "script.md"
    script_file.write_text("The episode body.", encoding="utf-8")
    store = StateStore(tmp_path / "test.sqlite3")
    try:
        publish_script(
            script_file=script_file,
            title="Some Title",
            feed_slug=feed_slug,
            store=store,
            r2_client=MagicMock(),
            date_str="2026-03-13",
            **kwargs,
        )
    finally:
        store.close()


def _publish_cli(tmp_path: Path, feed_slug: str, *extra: str):
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
            feed_slug,
            "--dry-run",
            *extra,
        ],
    )


def test_publish_script_default_voice_follows_feed_voices(
    tmp_path, fake_tts_render
) -> None:
    """A manual publish renders what the consumer would: the feed's config."""
    _publish(tmp_path, "fp-digest")
    [call] = fake_tts_render
    # Compared against the table, not a re-literaled voice, so this survives a
    # feed's voice (or provider) changing.
    assert call["config"] == resolve_render_config("fp-digest")
    assert call["config"] == FEED_VOICES["fp-digest"]
    assert call["feed_slug"] == "fp-digest"


def test_publish_script_dry_run_default_voice_follows_feed_voices(
    tmp_path, fake_tts_render
) -> None:
    res = _publish_cli(tmp_path, "fp-digest")
    assert res.exit_code == 0, res.output
    [call] = fake_tts_render
    assert call["config"] == resolve_render_config("fp-digest")


def test_publish_script_unknown_slug_still_gets_the_default_voice(
    tmp_path, fake_tts_render
) -> None:
    _publish(tmp_path, "no-such-feed")
    [call] = fake_tts_render
    assert call["config"] == DEFAULT_RENDER_CONFIG


def test_publish_script_explicit_voice_still_forces_openai(
    tmp_path, fake_tts_render
) -> None:
    _publish(tmp_path, "fp-digest", voice="nova")
    [call] = fake_tts_render
    assert call["config"] == openai_config(model="tts-1-hd", voice="nova")


def test_publish_script_dry_run_explicit_voice_still_forces_openai(
    tmp_path, fake_tts_render
) -> None:
    res = _publish_cli(tmp_path, "fp-digest", "--voice", "nova")
    assert res.exit_code == 0, res.output
    [call] = fake_tts_render
    assert call["config"] == openai_config(model="tts-1-hd", voice="nova")


_GEMINI_CFG = RenderConfig(
    primary=GeminiConfig(model="gemini-3.8-flash-lite-tts", voice="Kore"),
    fallback=OpenAIConfig(model="tts-1-hd", voice="onyx"),
)


def test_publish_script_default_falls_through_to_a_gemini_primary(
    tmp_path, fake_tts_render, monkeypatch
) -> None:
    """The T6 case: once a feed is Gemini, the consumer-down recovery path
    must render Gemini (with its OpenAI fallback), not a silent OpenAI voice."""
    monkeypatch.setitem(FEED_VOICES, "gemini-feed", _GEMINI_CFG)
    _publish(tmp_path, "gemini-feed")
    [call] = fake_tts_render
    assert call["config"] is _GEMINI_CFG


def test_publish_script_dry_run_falls_through_to_a_gemini_primary(
    tmp_path, fake_tts_render, monkeypatch
) -> None:
    monkeypatch.setitem(FEED_VOICES, "gemini-feed", _GEMINI_CFG)
    res = _publish_cli(tmp_path, "gemini-feed")
    assert res.exit_code == 0, res.output
    [call] = fake_tts_render
    assert call["config"] is _GEMINI_CFG
    # The dry-run echo describes what will actually render.
    assert "gemini gemini-3.8-flash-lite-tts/Kore" in res.output


def test_publish_script_dry_run_echo_describes_the_resolved_config(
    tmp_path, fake_tts_render
) -> None:
    res = _publish_cli(tmp_path, "fp-digest")
    assert res.exit_code == 0, res.output
    assert "Running TTS (dry run, openai tts-1-hd/onyx)..." in res.output
    res = _publish_cli(tmp_path, "fp-digest", "--voice", "ash")
    assert "Running TTS (dry run, openai tts-1-hd/ash)..." in res.output
