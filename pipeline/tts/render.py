"""Render one episode's text to one mp3: cache lookup, chunked synthesis with
renderer-owned retries, one encode, cache store, per-attempt manifest.

Deliberately free of call-site knowledge (no feed names, no voices): callers
say what to render with via ``RenderConfig`` and which feed/episode it is for.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from pipeline.tts.cache import (
    DEFAULT_CACHE_DIR,
    RENDERER_VERSION,
    cache_key,
    lookup,
    prune,
    store,
)
from pipeline.tts.chunker import chunk_text
from pipeline.tts.config import PCM_BYTES_PER_SECOND, RenderConfig
from pipeline.tts.encode import encode_mp3
from pipeline.tts.manifest import DEFAULT_MANIFEST_DIR, prune_manifests, write_manifest
from pipeline.tts.providers import OpenAIProvider, TTSProviderError


log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2.0, 8.0)  # sleep before attempt 2, before attempt 3


@dataclass(frozen=True)
class RenderResult:
    provider: (
        str  # provider that RENDERED the audio (not "published": caller's concern)
    )
    config: RenderConfig
    cached: bool
    chunks: int
    manifest_path: Path | None


class TTSRenderError(RuntimeError):
    """A chunk could not be synthesized (non-retryable, or retries exhausted)."""


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _provider_for(config: RenderConfig):
    if config.provider == "openai":
        return OpenAIProvider()
    raise ValueError(f"unsupported TTS provider: {config.provider!r}")


def _close(provider) -> None:
    try:
        provider.close()
    except Exception:  # noqa: BLE001 -- never discard valid audio over a close
        log.warning("TTS provider close failed", exc_info=True)


def _cached_stats(result: dict, config: RenderConfig) -> tuple[int, float, str]:
    """Telemetry from a cache entry's result.json; a bad value is not an error."""
    try:
        return (
            int(result.get("chunks", 0)),
            float(result.get("total_audio_seconds", 0.0)),
            str(result.get("provider", config.provider)),
        )
    except (TypeError, ValueError):
        return 0, 0.0, config.provider


def render_episode(
    text: str,
    config: RenderConfig,
    out_mp3: Path,
    *,
    feed_slug: str,
    episode_id: str,
    manifest_dir: Path | None = DEFAULT_MANIFEST_DIR,
    cache_dir: Path | None = DEFAULT_CACHE_DIR,
) -> RenderResult:
    """Render ``text`` to ``out_mp3``.

    Raises ``ValueError`` for empty text, ``TTSRenderError`` when a chunk fails
    for good, and lets anything else (missing API key, encoder failure)
    propagate unwrapped -- those are not retryable and must be loud. Every
    failure after validation still writes a ``status="failed"`` manifest.
    Cache and manifest problems never fail a render.
    """
    if not text.strip():
        raise ValueError("cannot render empty text")

    started_mono = time.monotonic()
    started_at = datetime.now(UTC)
    key = cache_key(text, config)
    chunk_records: list[dict] = []
    record: dict = {
        "renderer_version": RENDERER_VERSION,
        "feed_slug": feed_slug,
        "episode_id": episode_id,
        "started_at": started_at.isoformat(),
        "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "input_chars": len(text),
        "cache_key": key,
        "config": asdict(config),
        "rendered_provider": None,
        "cached": False,
        "chunks": chunk_records,
        "total_audio_seconds": 0.0,
        "cache_stored": False,
        "status": "failed",
        "error": None,
    }

    def emit() -> Path | None:
        record["wall_seconds"] = round(time.monotonic() - started_mono, 3)
        if chunk_records:  # a cache hit carries its stored total instead
            record["total_audio_seconds"] = sum(
                c["audio_seconds"] for c in chunk_records
            )
        path = None
        if manifest_dir is not None:
            path = write_manifest(
                manifest_dir, feed_slug=feed_slug, episode_id=episode_id, record=record
            )
            prune_manifests(manifest_dir)
        return path

    if cache_dir is not None:
        prune(cache_dir)
        hit = lookup(cache_dir, key)
        if hit is not None:
            try:
                shutil.copyfile(hit.audio, out_mp3)
            except OSError as exc:
                log.warning("TTS cache hit unusable (%s); re-rendering", exc)
            else:
                n_chunks, seconds, provider_name = _cached_stats(hit.result, config)
                record.update(
                    status="cache_hit",
                    cached=True,
                    rendered_provider=provider_name,
                    total_audio_seconds=seconds,
                )
                path = emit()
                log.info(
                    "TTS render: provider=%s chunks=%d audio=%.1fs wall=%.1fs "
                    "cached=True",
                    provider_name,
                    n_chunks,
                    seconds,
                    record["wall_seconds"],
                )
                return RenderResult(
                    provider=provider_name,
                    config=config,
                    cached=True,
                    chunks=n_chunks,
                    manifest_path=path,
                )

    provider = None
    try:
        provider = _provider_for(config)
        chunks = chunk_text(text, ceiling=provider.max_chars)
        pcm_parts: list[bytes] = []
        for i, chunk in enumerate(chunks):
            rec = {
                "index": i,
                "chars": len(chunk),
                "attempts": 0,
                "errors": [],
                "audio_seconds": 0.0,
            }
            chunk_records.append(rec)
            for attempt in range(MAX_ATTEMPTS):
                rec["attempts"] = attempt + 1
                try:
                    pcm = provider.synthesize(chunk, config)
                except TTSProviderError as exc:
                    rec["errors"].append(str(exc))
                    if not exc.retryable or attempt == MAX_ATTEMPTS - 1:
                        raise TTSRenderError(
                            f"chunk {i + 1}/{len(chunks)} failed after "
                            f"{rec['attempts']} attempt(s): {exc}"
                        ) from exc
                    _sleep(BACKOFF_SECONDS[attempt])
                else:
                    break
            rec["audio_seconds"] = len(pcm) / PCM_BYTES_PER_SECOND
            pcm_parts.append(pcm)

        encode_mp3(b"".join(pcm_parts), out_mp3)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        emit()
        raise
    finally:
        if provider is not None:
            _close(provider)

    total_seconds = sum(c["audio_seconds"] for c in chunk_records)
    if cache_dir is not None:
        record["cache_stored"] = store(
            cache_dir,
            key,
            out_mp3,
            {
                "provider": config.provider,
                "config": asdict(config),
                "renderer_version": RENDERER_VERSION,
                "rendered_at": datetime.now(UTC).isoformat(),
                "chunks": len(chunks),
                "total_audio_seconds": total_seconds,
            },
        )
    record.update(status="rendered", rendered_provider=config.provider)
    path = emit()
    log.info(
        "TTS render: provider=%s chunks=%d audio=%.1fs wall=%.1fs cached=False",
        config.provider,
        len(chunks),
        total_seconds,
        record["wall_seconds"],
    )
    return RenderResult(
        provider=config.provider,
        config=config,
        cached=False,
        chunks=len(chunks),
        manifest_path=path,
    )
