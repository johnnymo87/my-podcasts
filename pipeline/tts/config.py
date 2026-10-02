"""What an episode is rendered with.

A render is described by a ``RenderConfig``: a provider-specific *primary* leaf
plus an optional OpenAI *fallback*. Every dataclass is frozen and serializes
with ``dataclasses.asdict`` (the cache key and manifest rely on that).

``FEED_VOICES`` is the one owner of per-feed TTS settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


DEFAULT_OPENAI_MODEL = "tts-1-hd"
DEFAULT_OPENAI_VOICE = "nova"

# Raw PCM every provider hands the renderer: 24 kHz, mono, signed 16-bit LE.
PCM_SAMPLE_RATE = 24_000
PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * 2  # mono, 16-bit

# An OpenAI voice name must never reach Gemini (and vice versa): a stale feed
# entry would otherwise fail at the provider, mid-render.
OPENAI_VOICES = frozenset(
    {
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "fable",
        "nova",
        "onyx",
        "sage",
        "shimmer",
        "verse",
    }
)


def _require_nonempty(kind: str, model: object, voice: object) -> None:
    if not isinstance(model, str) or not isinstance(voice, str):
        raise ValueError(f"{kind} needs string model and voice")
    if not model or not voice:
        raise ValueError(f"{kind} needs model and voice, got {model!r}/{voice!r}")


@dataclass(frozen=True)
class OpenAIConfig:
    model: str
    voice: str
    # A fixed tag, not a parameter: it is in ``asdict`` output but not in __init__.
    provider: Literal["openai"] = field(default="openai", init=False)

    def __post_init__(self) -> None:
        _require_nonempty("OpenAI render", self.model, self.voice)


@dataclass(frozen=True)
class GeminiConfig:
    model: str
    voice: str
    style: str = ""
    provider: Literal["gemini"] = field(default="gemini", init=False)

    def __post_init__(self) -> None:
        _require_nonempty("Gemini render", self.model, self.voice)
        if not isinstance(self.style, str):
            raise ValueError("Gemini style must be a string")
        if self.voice.lower() in OPENAI_VOICES:
            raise ValueError(
                f"{self.voice!r} is an OpenAI voice name, not a Gemini voice"
            )


@dataclass(frozen=True)
class RenderConfig:
    primary: OpenAIConfig | GeminiConfig
    fallback: OpenAIConfig | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.primary, (OpenAIConfig, GeminiConfig)):
            raise ValueError(
                f"primary must be an OpenAI/Gemini config: {self.primary!r}"
            )
        if self.fallback is not None and not isinstance(self.fallback, OpenAIConfig):
            raise ValueError(f"fallback must be an OpenAIConfig: {self.fallback!r}")
        if isinstance(self.primary, OpenAIConfig) and self.fallback is not None:
            raise ValueError("an OpenAI primary takes no fallback")


def leaf_from_dict(d: object) -> OpenAIConfig | GeminiConfig:
    """Rebuild a leaf config from ``asdict`` output; ``ValueError`` on anything else."""
    if not isinstance(d, dict):
        raise ValueError(f"not a config dict: {d!r}")
    provider = d.get("provider")
    if provider == "openai":
        if set(d) != {"provider", "model", "voice"}:
            raise ValueError(f"unexpected OpenAI config keys: {sorted(d)}")
        return OpenAIConfig(model=d["model"], voice=d["voice"])
    if provider == "gemini":
        if set(d) != {"provider", "model", "voice", "style"}:
            raise ValueError(f"unexpected Gemini config keys: {sorted(d)}")
        return GeminiConfig(model=d["model"], voice=d["voice"], style=d["style"])
    raise ValueError(f"unknown provider in config: {provider!r}")


def openai_config(*, model: str, voice: str) -> RenderConfig:
    return RenderConfig(OpenAIConfig(model=model, voice=voice), None)


def _openai(voice: str) -> RenderConfig:
    return RenderConfig(OpenAIConfig(model=DEFAULT_OPENAI_MODEL, voice=voice))


GEMINI_TTS_MODEL = "gemini-3.8-flash-lite-tts"
GEMINI_STYLE = "calm, measured news anchor"


def _gemini(voice: str, fallback_voice: str) -> RenderConfig:
    """A Gemini feed: Flash-Lite primary, the feed's pre-Gemini OpenAI voice as
    the whole-episode fallback."""
    return RenderConfig(
        primary=GeminiConfig(model=GEMINI_TTS_MODEL, voice=voice, style=GEMINI_STYLE),
        fallback=OpenAIConfig(model=DEFAULT_OPENAI_MODEL, voice=fallback_voice),
    )


# The ONE owner of per-feed TTS settings. Feed slugs are literals because
# pipeline.tts must not import call-site modules (cycle); a test in
# pipeline/test_feed_voices.py pins that every routable feed slug is a key.
# The owner's listening-gate decision (my-podcasts-9p3.6, 2026-10-01): Gemini
# Flash-Lite on the-rundown (T6), fp-digest and levine (T7). Each Gemini feed's
# fallback is its own pre-Gemini OpenAI voice, so a fallback episode sounds like
# the old feed. Rollback per feed: see pipeline/AGENTS.md ("TTS Renderer").
FEED_VOICES: dict[str, RenderConfig] = {
    "general": _openai("ash"),
    "levine": _gemini("Enceladus", fallback_voice="ash"),
    "yglesias": _openai("shimmer"),
    "silver": _openai("echo"),
    "the-rundown": _gemini("Kore", fallback_voice="nova"),
    "fp-digest": _gemini("Alnilam", fallback_voice="onyx"),
    "aaronson": _openai("fable"),
    "chinatalk": _openai("alloy"),
}
DEFAULT_RENDER_CONFIG = _openai(DEFAULT_OPENAI_VOICE)


def resolve_render_config(
    feed_slug: str,
    *,
    voice_override: str | None = None,
    model_override: str | None = None,
) -> RenderConfig:
    """The render config for ``feed_slug``.

    Either override forces OpenAI; a field the override leaves unspecified comes
    from the feed's OpenAI config (its primary if OpenAI, else its fallback, else
    the default). An explicit empty-string override is an error, never "unset".
    """
    for name, value in (("voice", voice_override), ("model", model_override)):
        if value is not None and not value:
            raise ValueError(f"empty TTS {name} override")
    entry = FEED_VOICES.get(feed_slug, DEFAULT_RENDER_CONFIG)
    if voice_override is None and model_override is None:
        return entry
    # RenderConfig guarantees ``fallback`` is an OpenAIConfig or None, and
    # DEFAULT_RENDER_CONFIG's primary is OpenAI, so ``base`` is always OpenAI.
    if isinstance(entry.primary, OpenAIConfig):
        base = entry.primary
    else:
        base = entry.fallback or DEFAULT_RENDER_CONFIG.primary
    return openai_config(
        model=model_override if model_override is not None else base.model,
        voice=voice_override if voice_override is not None else base.voice,
    )
