"""Completed-render reuse: a retry after an upload/DB failure costs nothing.

Replaces tts-joinery's per-chunk cache. Only completed renders are stored.
Every function is best-effort and never raises -- a cache problem must never
discard valid audio (design doc, "Completed-render reuse").
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pipeline.tts.config import RenderConfig

log = logging.getLogger(__name__)

RENDERER_VERSION = "1"
DEFAULT_CACHE_DIR = Path("/persist/my-podcasts/tts-cache")
RETENTION_DAYS = 14


@dataclass(frozen=True)
class CachedRender:
    audio: Path
    result: dict


def cache_key(text: str, config: RenderConfig) -> str:
    payload = json.dumps(
        {
            "text": text,
            "primary": asdict(config),
            "fallback": None,
            "renderer_version": RENDERER_VERSION,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        return CachedRender(audio=audio, result=result)
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
            return True
        try:
            os.rename(tmp, target)
            tmp = None
        except OSError:
            if target.exists():  # lost a race; the first writer's entry stands
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
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
    except Exception:  # noqa: BLE001
        return
