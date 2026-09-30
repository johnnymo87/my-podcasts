from __future__ import annotations

import dataclasses

import pytest

from pipeline.tts.config import DEFAULT_OPENAI_MODEL, RenderConfig, openai_config


def test_openai_config_builds_openai_render_config() -> None:
    config = openai_config(model="tts-1-hd", voice="onyx")
    assert config == RenderConfig(
        provider="openai", openai_model="tts-1-hd", openai_voice="onyx"
    )


def test_default_model_is_tts_1_hd() -> None:
    assert DEFAULT_OPENAI_MODEL == "tts-1-hd"


def test_render_config_is_frozen() -> None:
    config = openai_config(model="tts-1-hd", voice="nova")
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.openai_voice = "ash"  # type: ignore[misc]


@pytest.mark.parametrize("model,voice", [("", "nova"), ("tts-1-hd", "")])
def test_openai_config_rejects_empty_fields(model: str, voice: str) -> None:
    with pytest.raises(ValueError):
        openai_config(model=model, voice=voice)
