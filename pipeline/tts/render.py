"""Render one episode's text to one mp3: cache lookup, chunked synthesis with
renderer-owned retries, one encode, cache store, per-attempt manifest.

Deliberately free of call-site knowledge (no feed names, no voices): callers
say what to render with via ``RenderConfig`` and which feed/episode it is for.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.tts import cache as _cache_mod
from pipeline.tts import manifest as _manifest_mod
from pipeline.tts.cache import (
    RENDERER_VERSION,
    cache_key,
    lookup,
    prune,
    store,
)
from pipeline.tts.chunker import chunk_text
from pipeline.tts.config import (
    PCM_BYTES_PER_SECOND,
    GeminiConfig,
    OpenAIConfig,
    RenderConfig,
)
from pipeline.tts.encode import encode_mp3
from pipeline.tts.manifest import prune_manifests, write_manifest
from pipeline.tts.providers import OpenAIProvider, TTSProviderError


log = logging.getLogger(__name__)

# Sentinel for "use the module default, looked up at call time". ``None`` is
# taken: it means "disabled". Resolving at call time (not at ``def`` time) lets a
# test redirect the defaults by patching the module constants.
_DEFAULT: Any = object()

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2.0, 8.0)  # sleep before attempt 2, before attempt 3


def _check_retry_schedule(max_attempts: int, backoff: tuple[float, ...]) -> None:
    """One backoff delay sits between each pair of attempts."""
    if len(backoff) != max_attempts - 1:
        raise RuntimeError(
            f"BACKOFF_SECONDS has {len(backoff)} delays but MAX_ATTEMPTS="
            f"{max_attempts} needs {max_attempts - 1}"
        )


_check_retry_schedule(MAX_ATTEMPTS, BACKOFF_SECONDS)


@dataclass(frozen=True)
class RenderResult:
    provider: (
        str  # provider that RENDERED the audio (not "published": caller's concern)
    )
    config: RenderConfig  # what was requested
    rendered: OpenAIConfig | GeminiConfig  # the leaf that actually produced the audio
    cached: bool
    chunks: int
    manifest_path: Path | None


class TTSRenderError(RuntimeError):
    """A chunk could not be synthesized (non-retryable, or retries exhausted)."""


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _provider_for(leaf: OpenAIConfig | GeminiConfig):
    if leaf.provider == "openai":
        return OpenAIProvider()
    raise ValueError(f"unsupported TTS provider: {leaf.provider!r}")


def _close(provider) -> None:
    try:
        provider.close()
    except Exception:  # noqa: BLE001 -- never discard valid audio over a close
        log.warning("TTS provider close failed", exc_info=True)


def _synthesize_chunk(
    provider, leaf: OpenAIConfig | GeminiConfig, chunk: str, rec: dict, n: int
):
    """One chunk to PCM, retrying retryable provider errors with backoff."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        rec["attempts"] = attempt
        try:
            return provider.synthesize(chunk, leaf)
        except TTSProviderError as exc:
            rec["errors"].append(str(exc))
            if not exc.retryable or attempt == MAX_ATTEMPTS:
                raise TTSRenderError(
                    f"chunk {rec['index'] + 1}/{n} failed after "
                    f"{attempt} attempt(s): {exc}"
                ) from exc
            delay = BACKOFF_SECONDS[attempt - 1]
            log.warning(
                "TTS chunk %d/%d attempt %d/%d failed (%s); retrying in %.1fs",
                rec["index"] + 1,
                n,
                attempt,
                MAX_ATTEMPTS,
                exc,
                delay,
            )
            _sleep(delay)
    raise AssertionError("unreachable: the last attempt returns or raises")


def _copy_atomic(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` so ``dst`` is never partial and never clobbered
    on failure."""
    fd, tmp_name = tempfile.mkstemp(
        dir=dst.parent, prefix=dst.name + ".", suffix=".tmp"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _size_or_none(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _cached_stats(result: dict) -> tuple[int, float]:
    """Telemetry from a cache entry's result.json; a bad value is not an error.

    Provenance (provider, rendered config) is not telemetry: ``cache.lookup``
    has already validated it and returns it on the ``CachedRender``.
    """
    try:
        return (
            int(result.get("chunks", 0)),
            float(result.get("total_audio_seconds", 0.0)),
        )
    except (TypeError, ValueError):
        return 0, 0.0


def _hit_matches_request(
    rendered: OpenAIConfig | GeminiConfig, config: RenderConfig
) -> bool:
    """A cache entry may only serve a request whose primary or fallback rendered it.

    The key already hashes both, so a mismatch means a corrupt or hand-placed
    entry; serving it would hand back audio in a voice nobody asked for.
    """
    return rendered == config.primary or (
        config.fallback is not None and rendered == config.fallback
    )


def render_episode(
    text: str,
    config: RenderConfig,
    out_mp3: Path,
    *,
    feed_slug: str,
    episode_id: str,
    manifest_dir: Path | None = _DEFAULT,
    cache_dir: Path | None = _DEFAULT,
) -> RenderResult:
    """Render ``text`` to ``out_mp3``.

    Raises ``ValueError`` for empty text or a Gemini primary (not wired until
    T3b), ``TTSRenderError`` when a chunk fails
    for good, and lets anything else (missing API key, encoder failure)
    propagate unwrapped -- those are not retryable and must be loud. Every
    failure after validation still writes a ``status="failed"`` manifest.
    Cache and manifest problems never fail a render. ``manifest_dir`` /
    ``cache_dir`` default to the module constants (resolved at call time);
    ``None`` disables that side effect.
    """
    if not text.strip():
        raise ValueError("cannot render empty text")
    if config.primary.provider != "openai":
        # Checked before any cache/manifest work so a refused render leaves no trace.
        raise ValueError("Gemini rendering is not wired yet (T3b, my-podcasts-9p3.11)")
    if manifest_dir is _DEFAULT:
        manifest_dir = _manifest_mod.DEFAULT_MANIFEST_DIR
    if cache_dir is _DEFAULT:
        cache_dir = _cache_mod.DEFAULT_CACHE_DIR

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
        "rendered_config": None,
        "rendered_provider": None,
        "cached": False,
        "chunk_count": None,
        "chunks": chunk_records,
        "total_audio_seconds": 0.0,
        "out_bytes": None,
        "cache_stored": False,
        "status": "failed",
        "error": None,
    }

    def emit() -> Path | None:
        record["wall_seconds"] = round(time.monotonic() - started_mono, 3)
        record["finished_at"] = datetime.now(UTC).isoformat()
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
        if hit is not None and not _hit_matches_request(hit.rendered, config):
            log.warning(
                "TTS cache entry %s was rendered by %r, not the request's "
                "primary/fallback; re-rendering",
                key,
                hit.rendered,
            )
            # store() keeps an existing structurally-valid entry, so without
            # this the bad one would shadow every fresh render of this key.
            shutil.rmtree(cache_dir / key, ignore_errors=True)
            hit = None
        if hit is not None:
            try:
                _copy_atomic(hit.audio, out_mp3)
            except OSError as exc:
                log.warning("TTS cache hit unusable (%s); re-rendering", exc)
            else:
                n_chunks, seconds = _cached_stats(hit.result)
                provider_name = hit.rendered.provider
                record.update(
                    status="cache_hit",
                    cached=True,
                    rendered_config=asdict(hit.rendered),
                    rendered_provider=provider_name,
                    total_audio_seconds=seconds,
                    chunk_count=n_chunks,
                    out_bytes=_size_or_none(out_mp3),
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
                    rendered=hit.rendered,
                    cached=True,
                    chunks=n_chunks,
                    manifest_path=path,
                )

    leaf = config.primary
    provider = None
    chunks: list[str] = []
    try:
        provider = _provider_for(leaf)
        chunks = chunk_text(text, ceiling=provider.max_chars)
        record["chunk_count"] = len(chunks)
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
            pcm = _synthesize_chunk(provider, leaf, chunk, rec, len(chunks))
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
                "schema": 2,
                "provider": leaf.provider,
                "requested": asdict(config),
                "rendered": asdict(leaf),
                "verification": "not_run_openai",
                "fallback_reason": None,
                "renderer_version": RENDERER_VERSION,
                "rendered_at": datetime.now(UTC).isoformat(),
                "chunks": len(chunks),
                "total_audio_seconds": total_seconds,
            },
        )
    record.update(
        status="rendered",
        rendered_config=asdict(leaf),
        rendered_provider=leaf.provider,
        out_bytes=_size_or_none(out_mp3),
    )
    path = emit()
    log.info(
        "TTS render: provider=%s chunks=%d audio=%.1fs wall=%.1fs cached=False",
        leaf.provider,
        len(chunks),
        total_seconds,
        record["wall_seconds"],
    )
    return RenderResult(
        provider=leaf.provider,
        config=config,
        rendered=leaf,
        cached=False,
        chunks=len(chunks),
        manifest_path=path,
    )
