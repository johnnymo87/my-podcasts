"""What an episode is rendered with.

T1 knows only OpenAI. T3 adds Gemini fields and per-feed resolution; keep this
dataclass the single description of a render so the cache key and manifest
can serialize it with ``dataclasses.asdict``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


DEFAULT_OPENAI_MODEL = "tts-1-hd"


@dataclass(frozen=True)
class RenderConfig:
    provider: Literal["openai"]
    openai_model: str
    openai_voice: str


def openai_config(*, model: str, voice: str) -> RenderConfig:
    if not model or not voice:
        raise ValueError(
            f"OpenAI render needs model and voice, got {model!r}/{voice!r}"
        )
    return RenderConfig(provider="openai", openai_model=model, openai_voice=voice)
