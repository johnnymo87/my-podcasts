"""Completed-render reuse: a retry after an upload/DB failure costs nothing.

Replaces tts-joinery's per-chunk cache. Only completed renders are stored.
Every function is best-effort and never raises -- a cache problem must never
discard valid audio (design doc, "Completed-render reuse").
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from pipeline.tts.config import GeminiConfig, OpenAIConfig, RenderConfig, leaf_from_dict


log = logging.getLogger(__name__)

# "2": typed primary/fallback config in the key, provenance-checked entries.
RENDERER_VERSION = "2"
DEFAULT_CACHE_DIR = Path("/persist/my-podcasts/tts-cache")
RETENTION_DAYS = 14


@dataclass(frozen=True)
class CachedRender:
    audio: Path
    result: dict
    rendered: OpenAIConfig | GeminiConfig


def _verifier_policy(config: RenderConfig) -> str | None:
    """What a Gemini render was verified against; ``None`` when nothing is.

    Imported lazily: ``verify`` pulls in ``asr`` and so ``google.genai``, which
    the OpenAI path must never load.
    """
    if config.primary.provider != "gemini":
        return None
    from pipeline.tts.verify import VERIFIER_POLICY

    return VERIFIER_POLICY


def cache_key(text: str, config: RenderConfig) -> str:
    payload = json.dumps(
        {
            "text": text,
            "primary": asdict(config.primary),
            "fallback": asdict(config.fallback) if config.fallback else None,
            "renderer_version": RENDERER_VERSION,
            "verifier_policy": _verifier_policy(config),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validated_leaf(result: dict) -> OpenAIConfig | GeminiConfig | None:
    """The leaf that rendered this entry, or ``None`` if its provenance is bad.

    Verification must agree with the renderer: a Gemini render is valid only if
    it ``passed`` verification, an OpenAI render only as ``not_run_openai``.
    """
    if result.get("schema") != 2:
        return None
    try:
        rendered = leaf_from_dict(result.get("rendered"))
    except ValueError:
        return None
    if result.get("provider") != rendered.provider:
        return None
    verification = result.get("verification")
    if verification not in {"passed", "not_run_openai"}:
        return None
    if (verification == "passed") != (rendered.provider == "gemini"):
        return None
    return rendered


def lookup(cache_dir: Path, key: str) -> CachedRender | None:
    try:
        entry = cache_dir / key
        audio = entry / "audio.mp3"
        result = json.loads((entry / "result.json").read_text(encoding="utf-8"))
        if (
            not isinstance(result, dict)
            or not audio.is_file()
            or audio.stat().st_size == 0
        ):
            return None
        rendered = _validated_leaf(result)
        if rendered is None:
            return None
        return CachedRender(audio=audio, result=result, rendered=rendered)
    except Exception:  # noqa: BLE001 -- a cache miss is always safe
        return None


def store(cache_dir: Path, key: str, mp3: Path, result: dict) -> bool:
    tmp: Path | None = None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=cache_dir, prefix=".tmp-"))
        shutil.copyfile(mp3, tmp / "audio.mp3")
        (tmp / "result.json").write_text(
            json.dumps(result, sort_keys=True), encoding="utf-8"
        )
        target = cache_dir / key
        if target.exists():
            if lookup(cache_dir, key) is not None:
                return True  # a valid entry already stands; keep the first
            # Corrupt/empty/half entry: it would miss on every lookup and block
            # the rename forever. Clear it so this fresh render can land.
            shutil.rmtree(target, ignore_errors=True)
        try:
            os.rename(tmp, target)
            tmp = None
        except OSError:
            if lookup(cache_dir, key) is not None:  # lost a race to a valid entry
                return True
            raise
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("TTS cache store failed (%s); reduced retry protection", exc)
        return False
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def prune(
    cache_dir: Path, *, max_age_days: int = RETENTION_DAYS, now: float | None = None
) -> None:
    try:
        cutoff = (now if now is not None else time.time()) - max_age_days * 86400
        for entry in cache_dir.iterdir():
            with contextlib.suppress(OSError):  # one raced entry must not end the pass
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
    except Exception:  # noqa: BLE001
        return
