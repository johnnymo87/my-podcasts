"""Render reuse: a retry after an upload/DB failure costs nothing.

Two layers, both under one ``cache_dir``:

* **Completed renders** (``<cache_dir>/<key>/``): the finished mp3, keyed by the
  whole text + config.
* **The OpenAI chunk spool** (``<cache_dir>/chunks/``): the raw PCM of each
  OpenAI chunk, so a render that fails partway does not re-buy the chunks it had
  already paid for on the next retry. It holds only chunks of renders that did
  NOT complete: a render that stores its completed entry discards its spool
  files (:func:`spool_discard`). OpenAI leaves only: the Gemini phase runs in a
  killable child with per-chunk verification and spools nothing.

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
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from pipeline.tts.config import GeminiConfig, OpenAIConfig, RenderConfig, leaf_from_dict


log = logging.getLogger(__name__)

# "2": typed primary/fallback config in the key, provenance-checked entries.
RENDERER_VERSION = "2"
DEFAULT_CACHE_DIR = Path("/persist/my-podcasts/tts-cache")
RETENTION_DAYS = 14
# Hex digests name completed entries, so this cannot collide with one; only
# ``prune`` treats the name specially (it must not age the directory out whole).
SPOOL_DIRNAME = "chunks"


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
            if entry.name == SPOOL_DIRNAME:
                continue  # pruned file by file below, never as one old entry
            with contextlib.suppress(OSError):  # one raced entry must not end the pass
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
        _prune_spool(cache_dir, cutoff)
    except Exception:  # noqa: BLE001
        return


def _prune_spool(cache_dir: Path, cutoff: float) -> None:
    """Delete spool files (stale ``.tmp-`` leftovers too) older than ``cutoff``."""
    try:
        files = list((cache_dir / SPOOL_DIRNAME).iterdir())
    except OSError:
        return
    for f in files:
        with contextlib.suppress(OSError):  # one raced file must not end the pass
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()


# --- per-chunk OpenAI PCM spool ----------------------------------------------


def chunk_key(leaf: OpenAIConfig | GeminiConfig, chunk: str) -> str:
    """Identity of one chunk's PCM: what was said, by whom, under which renderer."""
    payload = json.dumps(
        {
            "kind": "openai-chunk",
            "renderer_version": RENDERER_VERSION,
            "leaf": asdict(leaf),
            "text": chunk,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _spool_paths(cache_dir: Path, key: str) -> tuple[Path, Path]:
    base = cache_dir / SPOOL_DIRNAME
    return base / f"{key}.pcm", base / f"{key}.json"


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def spool_lookup(cache_dir: Path, key: str) -> bytes | None:
    """The spooled PCM for ``key``, or ``None`` (a miss) for anything but an entry
    whose sidecar agrees with its PCM. A damaged entry is deleted. Never raises."""
    try:
        pcm_path, side_path = _spool_paths(cache_dir, key)
        if not pcm_path.exists() and not side_path.exists():
            return None
        try:
            side = json.loads(side_path.read_text(encoding="utf-8"))
            pcm = pcm_path.read_bytes()
            valid = (
                isinstance(side, dict)
                and len(pcm) > 0
                and len(pcm) % 2 == 0
                and side.get("bytes") == len(pcm)
                and side.get("sha256") == hashlib.sha256(pcm).hexdigest()
            )
        except (OSError, ValueError):
            valid = False
        if valid:
            return pcm
        log.warning("TTS chunk spool entry %s is damaged; discarding it", key)
        for p in (pcm_path, side_path):
            with contextlib.suppress(OSError):
                p.unlink()
        return None
    except Exception:  # noqa: BLE001 -- a spool miss is always safe
        return None


def spool_store(
    cache_dir: Path, key: str, leaf: OpenAIConfig | GeminiConfig, pcm: bytes
) -> bool:
    """Spool ``pcm`` for ``key``; True if stored. Never raises.

    The sidecar is written last, so a crash between the two leaves an entry that
    :func:`spool_lookup` rejects.
    """
    try:
        if not pcm or len(pcm) % 2:
            log.warning(
                "TTS chunk spool: refusing to spool unusable PCM (%d bytes)", len(pcm)
            )
            return False
        pcm_path, side_path = _spool_paths(cache_dir, key)
        pcm_path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(pcm_path, pcm)
        sidecar = {
            "bytes": len(pcm),
            "sha256": hashlib.sha256(pcm).hexdigest(),
            "created": time.time(),
            "leaf": asdict(leaf),
        }
        _write_atomic(side_path, json.dumps(sidecar, sort_keys=True).encode("utf-8"))
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("TTS chunk spool store failed (%s); reduced retry protection", exc)
        return False


def spool_discard(cache_dir: Path, keys: Iterable[str]) -> None:
    """Delete the spool files (``.pcm`` and ``.json``) of every key. Never raises."""
    try:
        for key in set(keys):
            for path in _spool_paths(cache_dir, key):
                with contextlib.suppress(OSError):
                    path.unlink()
    except Exception as exc:  # noqa: BLE001
        log.warning("TTS chunk spool cleanup failed (%s)", exc)
