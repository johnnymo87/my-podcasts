"""The email path hands the renderer the right config and episode identity."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pipeline.db import StateStore
from pipeline.processor import process_email_bytes
from pipeline.tts import GeminiConfig, OpenAIConfig, RenderConfig, openai_config


_EMAIL = b"""\
Date: Mon, 17 Aug 2026 08:00:00 +0000
Subject: Money Stuff: Goat Hedge
Content-Type: text/html; charset="UTF-8"
MIME-Version: 1.0

<html><body><p>A private equity fund bought a herd of goats.</p></body></html>
"""


@pytest.fixture(autouse=True)
def _stub_probe_and_feed(monkeypatch):
    monkeypatch.setattr(
        "pipeline.processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="60.0\n"),
    )
    monkeypatch.delenv("TTS_MODEL", raising=False)
    monkeypatch.delenv("TTS_VOICE", raising=False)


def _run(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    try:
        process_email_bytes(
            raw_email=_EMAIL,
            source_r2_key="raw/test.eml",
            route_tag="levine",
            store=store,
            r2_client=MagicMock(),
            levine_cache_dir=tmp_path / "levine-cache",
        )
    finally:
        store.close()


def test_renders_with_preset_model_and_voice(tmp_path, fake_tts_render) -> None:
    _run(tmp_path)

    [call] = fake_tts_render
    # T7 (owner gate my-podcasts-9p3.6): Levine is Gemini Enceladus, with its
    # pre-Gemini voice (ash) as the whole-episode fallback.
    assert call["config"] == RenderConfig(
        primary=GeminiConfig(
            model="gemini-3.8-flash-lite-tts",
            voice="Enceladus",
            style="calm, measured news anchor",
        ),
        fallback=OpenAIConfig(model="tts-1-hd", voice="ash"),
    )
    assert call["feed_slug"] == "levine"
    assert call["episode_id"] == "2026-08-17-Money-Stuff-Goat-Hedge"


def test_env_overrides_preset_voice_and_model(
    tmp_path, fake_tts_render, monkeypatch
) -> None:
    monkeypatch.setenv("TTS_VOICE", "shimmer")
    monkeypatch.setenv("TTS_MODEL", "tts-1")

    _run(tmp_path)

    [call] = fake_tts_render
    assert call["config"] == openai_config(model="tts-1", voice="shimmer")
