"""In-repo TTS renderer. See docs/plans/2026-09-30-gemini-tts-design.md."""

from pipeline.tts.config import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_VOICE,
    FEED_VOICES,
    GeminiConfig,
    OpenAIConfig,
    RenderConfig,
    openai_config,
    resolve_render_config,
)
from pipeline.tts.render import RenderResult, TTSRenderError, render_episode


__all__ = [
    "DEFAULT_OPENAI_MODEL",
    "DEFAULT_OPENAI_VOICE",
    "FEED_VOICES",
    "GeminiConfig",
    "OpenAIConfig",
    "RenderConfig",
    "RenderResult",
    "TTSRenderError",
    "openai_config",
    "render_episode",
    "resolve_render_config",
]
