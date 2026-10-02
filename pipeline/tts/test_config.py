from __future__ import annotations

import dataclasses

import pytest

from pipeline.tts import config
from pipeline.tts.config import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_VOICE,
    OPENAI_VOICES,
    GeminiConfig,
    OpenAIConfig,
    RenderConfig,
    leaf_from_dict,
    openai_config,
    resolve_render_config,
)


GEMINI = GeminiConfig(model="gemini-3.8-flash-lite-tts", voice="Kore")


def test_openai_config_builds_openai_render_config() -> None:
    cfg = openai_config(model="tts-1-hd", voice="onyx")
    assert cfg == RenderConfig(OpenAIConfig("tts-1-hd", "onyx"), None)
    assert cfg.primary.provider == "openai"
    assert cfg.fallback is None


def test_defaults() -> None:
    assert DEFAULT_OPENAI_MODEL == "tts-1-hd"
    assert DEFAULT_OPENAI_VOICE == "nova"


@pytest.mark.parametrize("model,voice", [("", "nova"), ("tts-1-hd", "")])
def test_openai_config_rejects_empty_fields(model: str, voice: str) -> None:
    with pytest.raises(ValueError):
        openai_config(model=model, voice=voice)


def test_every_config_dataclass_is_frozen() -> None:
    cfg = openai_config(model="tts-1-hd", voice="nova")
    for obj, attr in [
        (cfg, "primary"),
        (cfg.primary, "voice"),
        (GEMINI, "voice"),
    ]:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, "x")


def test_render_config_rejects_fallback_on_openai_primary() -> None:
    with pytest.raises(ValueError):
        RenderConfig(OpenAIConfig("tts-1-hd", "nova"), OpenAIConfig("tts-1", "ash"))


def test_render_config_rejects_non_openai_fallback() -> None:
    with pytest.raises(ValueError):
        RenderConfig(GEMINI, GEMINI)  # type: ignore[arg-type]


def test_render_config_rejects_wrong_primary_type() -> None:
    with pytest.raises(ValueError):
        RenderConfig("openai")  # type: ignore[arg-type]


def test_render_config_accepts_gemini_with_and_without_fallback() -> None:
    assert RenderConfig(GEMINI, OpenAIConfig("tts-1-hd", "echo")).fallback is not None
    assert RenderConfig(GEMINI, None).fallback is None


@pytest.mark.parametrize("model,voice", [("", "Kore"), ("m", "")])
def test_gemini_config_rejects_empty_fields(model: str, voice: str) -> None:
    with pytest.raises(ValueError):
        GeminiConfig(model=model, voice=voice)


@pytest.mark.parametrize("voice", sorted(OPENAI_VOICES) + ["Nova", "ONYX"])
def test_gemini_config_rejects_openai_voice_names(voice: str) -> None:
    with pytest.raises(ValueError, match="OpenAI voice"):
        GeminiConfig(model="m", voice=voice)


def test_provider_is_a_fixed_tag_not_an_init_argument() -> None:
    with pytest.raises(TypeError):
        OpenAIConfig("m", "v", provider="gemini")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        GeminiConfig("m", "Kore", provider="openai")  # type: ignore[call-arg]
    assert dataclasses.asdict(GEMINI)["provider"] == "gemini"


def test_gemini_style_may_be_empty() -> None:
    assert GeminiConfig(model="m", voice="Kore", style="").style == ""
    assert GeminiConfig(model="m", voice="Kore").style == ""


@pytest.mark.parametrize(
    "slug,voice",
    [
        ("general", "ash"),
        ("levine", "ash"),
        ("yglesias", "shimmer"),
        ("silver", "echo"),
        ("fp-digest", "onyx"),
        ("aaronson", "fable"),
        ("chinatalk", "alloy"),
        ("some-unknown-feed", "nova"),
    ],
)
def test_golden_feed_voices(slug: str, voice: str) -> None:
    assert resolve_render_config(slug) == openai_config(model="tts-1-hd", voice=voice)


# The owner's listening-gate decision (my-podcasts-9p3.6). Re-literaled, never
# derived from FEED_VOICES: this is the regression net for production audio.
RUNDOWN_GEMINI = RenderConfig(
    primary=GeminiConfig(
        model="gemini-3.8-flash-lite-tts",
        voice="Kore",
        style="calm, measured news anchor",
    ),
    fallback=OpenAIConfig(model="tts-1-hd", voice="nova"),
)


def test_golden_the_rundown_is_gemini_with_its_old_voice_as_fallback() -> None:
    assert resolve_render_config("the-rundown") == RUNDOWN_GEMINI


def test_golden_only_the_rundown_is_gemini() -> None:
    gemini = {
        slug
        for slug, entry in config.FEED_VOICES.items()
        if isinstance(entry.primary, GeminiConfig)
    }
    assert gemini == {"the-rundown"}


def test_override_on_the_rundown_forces_openai_from_its_fallback() -> None:
    assert resolve_render_config("the-rundown", voice_override="onyx") == (
        openai_config(model="tts-1-hd", voice="onyx")
    )
    assert resolve_render_config("the-rundown", model_override="tts-1") == (
        openai_config(model="tts-1", voice="nova")
    )


def test_voice_override_forces_openai() -> None:
    assert resolve_render_config("levine", voice_override="shimmer") == openai_config(
        model="tts-1-hd", voice="shimmer"
    )


def test_model_override_keeps_feed_voice() -> None:
    assert resolve_render_config("levine", model_override="tts-1") == openai_config(
        model="tts-1", voice="ash"
    )


def test_both_overrides() -> None:
    assert resolve_render_config(
        "levine", voice_override="echo", model_override="tts-1"
    ) == openai_config(model="tts-1", voice="echo")


@pytest.mark.parametrize("kw", [{"voice_override": ""}, {"model_override": ""}])
def test_empty_override_is_an_error(kw: dict) -> None:
    with pytest.raises(ValueError):
        resolve_render_config("levine", **kw)


def test_override_on_gemini_feed_takes_missing_field_from_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        config,
        "FEED_VOICES",
        {"g": RenderConfig(GEMINI, OpenAIConfig("tts-1-hd", "echo"))},
    )
    assert resolve_render_config("g", model_override="tts-1") == openai_config(
        model="tts-1", voice="echo"
    )
    assert resolve_render_config("g", voice_override="onyx") == openai_config(
        model="tts-1-hd", voice="onyx"
    )


def test_override_on_gemini_feed_without_fallback_uses_defaults(monkeypatch) -> None:
    monkeypatch.setattr(config, "FEED_VOICES", {"g": RenderConfig(GEMINI, None)})
    assert resolve_render_config("g", model_override="tts-1") == openai_config(
        model="tts-1", voice="nova"
    )
    assert resolve_render_config("g", voice_override="onyx") == openai_config(
        model="tts-1-hd", voice="onyx"
    )


def test_resolve_without_override_returns_gemini_entry_unchanged(monkeypatch) -> None:
    entry = RenderConfig(GEMINI, OpenAIConfig("tts-1-hd", "echo"))
    monkeypatch.setattr(config, "FEED_VOICES", {"g": entry})
    assert resolve_render_config("g") == entry


def test_asdict_serializes_the_tree() -> None:
    assert dataclasses.asdict(openai_config(model="tts-1-hd", voice="onyx")) == {
        "primary": {"provider": "openai", "model": "tts-1-hd", "voice": "onyx"},
        "fallback": None,
    }


def test_leaf_from_dict_round_trips() -> None:
    o = OpenAIConfig("tts-1-hd", "onyx")
    g = GeminiConfig("m", "Kore", "calm")
    assert leaf_from_dict(dataclasses.asdict(o)) == o
    assert leaf_from_dict(dataclasses.asdict(g)) == g


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "openai",
        {},
        {"provider": "nope", "model": "m", "voice": "v"},
        {"model": "m", "voice": "v"},  # provider missing
        {"provider": None, "model": "m", "voice": "v"},
        {"provider": "openai", "model": "m"},
        {"provider": "openai", "model": "m", "voice": "v", "extra": 1},
        {"provider": "openai", "model": "", "voice": "v"},
        {"provider": "gemini", "model": "m", "voice": "nova", "style": ""},
        {"provider": "openai", "model": 1, "voice": "v"},
    ],
)
def test_leaf_from_dict_rejects_garbage(bad: object) -> None:
    with pytest.raises(ValueError):
        leaf_from_dict(bad)  # type: ignore[arg-type]
