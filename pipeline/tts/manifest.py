"""Per-attempt render manifests: the diagnostic trail for "what did this
episode actually get rendered with, and what went wrong".

Best-effort by design. A manifest problem must never fail or discard a render,
so nothing here raises.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path


log = logging.getLogger(__name__)

DEFAULT_MANIFEST_DIR = Path("/persist/my-podcasts/tts-renders")
RETENTION_DAYS = 60
_MAX_FILENAME = 200
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_component(value: str) -> str:
    """One path component: no separators, no leading dots, never empty."""
    return _UNSAFE.sub("-", value).lstrip(".") or "unnamed"


OMISSION_AUDIO_DIRNAME = "omission-audio"


def utc_stamp() -> str:
    """The timestamp in manifest (and omission clip) file names, microseconds
    included so two attempts in one second never collide."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def omission_audio_dir(manifest_dir: Path, feed_slug: str) -> Path:
    """Where a feed's kept omission clips live: next to its manifests, in a
    subdirectory (so the ``*/*.json`` manifest glob never sees them)."""
    return manifest_dir / _safe_component(feed_slug) / OMISSION_AUDIO_DIRNAME


def omission_clip_path(
    omission_dir: Path, episode_id: str, stamp: str, *, chunk: int, attempt: int
) -> Path:
    """``<id>-<stamp>-c<chunk>-a<attempt>.mp3``, the id sanitized and truncated
    exactly as a manifest's is."""
    suffix = f"-{stamp}-c{chunk:04d}-a{attempt}.mp3"
    safe_id = _safe_component(episode_id)[: _MAX_FILENAME - len(suffix)]
    return omission_dir / f"{safe_id}{suffix}"


def write_manifest(
    manifest_dir: Path, *, feed_slug: str, episode_id: str, record: dict
) -> Path | None:
    """Write ``record`` atomically to ``manifest_dir/feed_slug/<id>-<utc>.json``.

    ``feed_slug`` and ``episode_id`` are sanitized to ``[A-Za-z0-9._-]`` (the
    id is also truncated) so neither can escape ``manifest_dir``.

    The timestamp carries microseconds so two attempts in one second do not
    overwrite each other. Returns ``None`` (after a warning) on any failure.
    """
    tmp: Path | None = None
    try:
        stamp = utc_stamp()
        suffix = f"-{stamp}.json"
        safe_id = _safe_component(episode_id)[: _MAX_FILENAME - len(suffix)]
        path = manifest_dir / _safe_component(feed_slug) / f"{safe_id}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(record, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        tmp = None
        return path
    except Exception as exc:  # noqa: BLE001
        log.warning("TTS manifest write failed (%s); continuing without it", exc)
        return None
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()


def prune_manifests(manifest_dir: Path, *, max_age_days: int = RETENTION_DAYS) -> None:
    """Remove manifests, and kept omission clips (``*/omission-audio/*.mp3``),
    older than ``max_age_days`` (by mtime). Never raises."""
    try:
        cutoff = time.time() - max_age_days * 86400
        for pattern in ("*/*.json", f"*/{OMISSION_AUDIO_DIRNAME}/*.mp3"):
            for path in manifest_dir.glob(pattern):
                with contextlib.suppress(OSError):
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
    except Exception:  # noqa: BLE001
        return
