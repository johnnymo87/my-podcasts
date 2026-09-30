"""Render one episode's text to one mp3: cache lookup, chunked synthesis with
renderer-owned retries, one encode, cache store, per-attempt manifest.

A Gemini primary renders through ``gemini_phase`` (a killable child process that
synthesizes and verifies every chunk); any problem there discards ALL Gemini
audio and the whole episode is rendered in-process with the OpenAI fallback, in
the same voice-consistent way an OpenAI-primary episode always was. The Gemini
modules are imported only when a Gemini primary is rendered, so the OpenAI path
never loads ``google.genai``.

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
import threading
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
from pipeline.tts.providers import GeminiProvider, OpenAIProvider, TTSProviderError


log = logging.getLogger(__name__)

# Sentinel for "use the module default, looked up at call time". ``None`` is
# taken: it means "disabled". Resolving at call time (not at ``def`` time) lets a
# test redirect the defaults by patching the module constants.
_DEFAULT: Any = object()

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2.0, 8.0)  # sleep before attempt 2, before attempt 3
# The fallback alert is best-effort and bounded: ``send_alert`` is per-read (10 s
# timeout), not wall-clock, so it runs in a daemon thread and is abandoned after
# this long rather than holding up an episode that has already been rendered.
ALERT_WAIT_SECONDS = 12.0


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
    # Why the Gemini phase failed and OpenAI rendered instead (a
    # ``gemini_phase.FALLBACK_REASONS`` member); None for an ordinary render.
    # On a cache hit it is what the stored entry recorded.
    fallback_reason: str | None = None


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


def _synthesize_all(
    leaf: OpenAIConfig | GeminiConfig,
    text: str,
    chunk_records: list[dict],
    record: dict,
) -> list[bytes]:
    """Chunk ``text`` for ``leaf``'s provider and synthesize every chunk in-process.

    Appends one record per chunk to ``chunk_records`` as it goes (so a failure
    keeps its history) and sets ``record["chunk_count"]``.
    """
    provider = None
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
        return pcm_parts
    finally:
        if provider is not None:
            _close(provider)


_TOKEN_FIELDS = (
    ("synth_prompt", "synth", "prompt_tokens"),
    ("synth_audio", "synth", "audio_tokens"),
    ("asr_input", "asr", "input_tokens"),
    ("asr_output", "asr", "output_tokens"),
    ("asr_thinking", "asr", "thinking_tokens"),
)


def _phase_totals(phase_chunk_records: list[dict]) -> tuple[dict, float]:
    """Token totals and generated audio seconds across every Gemini attempt.

    A total is ``None`` when ANY contributing count is unknown -- a killed
    request, an errored one, a chunk with no readable progress -- because a sum
    that silently omits a cost is worse than no sum. An attempt that never
    reached ASR contributes nothing to the ASR totals (nothing was requested).
    """
    totals: dict[str, int | None] = {name: 0 for name, _, _ in _TOKEN_FIELDS}
    pcm_bytes = 0
    for rec in phase_chunk_records:
        if rec.get("progress") in ("missing", "unreadable"):
            totals = dict.fromkeys(totals)  # we cannot know what this chunk cost
            continue
        for attempt in rec.get("attempts", []):
            synth = attempt.get("synth") or {}
            if isinstance(synth.get("pcm_bytes"), int):
                pcm_bytes += synth["pcm_bytes"]
            for name, stage, field in _TOKEN_FIELDS:
                part = attempt.get(stage)
                if part is None:
                    continue  # the stage never ran: nothing to count
                value = part.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    if totals[name] is not None:
                        totals[name] += value
                else:
                    totals[name] = None
    return totals, pcm_bytes / PCM_BYTES_PER_SECOND


def _phase_record(outcome, *, budget_s: float, pcm_parts) -> dict:
    """The manifest's ``gemini_phase`` block. Kept apart from ``chunks``, which
    is only the audio that shipped."""
    tokens, generated = _phase_totals(outcome.chunk_records)
    used = sum(len(p) for p in pcm_parts) / PCM_BYTES_PER_SECOND if pcm_parts else 0.0
    return {
        "outcome": "ok" if outcome.ok else "failed",
        "reason": outcome.reason,
        "detail": outcome.detail,
        "failed_chunk": outcome.failed_chunk,
        "budget_s": budget_s,
        "elapsed_s": outcome.elapsed_s,
        "spawn_s": outcome.spawn_s,
        "child_started_s": outcome.child_started_s,
        "child_pid": outcome.child_pid,
        "chunks": outcome.chunk_records,
        "tokens": tokens,
        "audio_seconds_generated": generated,
        "audio_seconds_used": used,
    }


def _shipped_chunk_records(gemini_chunks: list[str], outcome) -> list[dict]:
    """``chunks`` records for a successful Gemini phase (attempts from progress)."""
    by_index = {r.get("index"): r for r in outcome.chunk_records}
    records = []
    for i, chunk in enumerate(gemini_chunks):
        attempts = by_index.get(i, {}).get("attempts", [])
        errors = [
            a["synth"]["error"] for a in attempts if (a.get("synth") or {}).get("error")
        ]
        records.append(
            {
                "index": i,
                "chars": len(chunk),
                "attempts": len(attempts),
                "errors": errors,
                "audio_seconds": len(outcome.pcm_parts[i]) / PCM_BYTES_PER_SECOND,
            }
        )
    return records


def _deliver_alert(text: str) -> bool | str:
    """Send ``text`` without letting delivery hold up the render.

    True/False is ``send_alert``'s own answer (an exception counts as False);
    ``"timeout"`` means it had not finished after ``ALERT_WAIT_SECONDS`` and the
    daemon thread was abandoned. ``send_alert`` is looked up on the
    ``pipeline.alerts`` module at call time so a test can replace it.
    """
    from pipeline import alerts

    result: list[bool] = []

    def run() -> None:
        try:
            result.append(bool(alerts.send_alert(text)))
        except Exception:  # noqa: BLE001 -- reporting must never disturb a render
            result.append(False)

    thread = threading.Thread(target=run, name="tts-fallback-alert", daemon=True)
    thread.start()
    thread.join(ALERT_WAIT_SECONDS)
    if thread.is_alive():
        log.warning(
            "TTS fallback alert still sending after %.0fs; moving on",
            ALERT_WAIT_SECONDS,
        )
        return "timeout"
    return result[0] if result else False


def _fallback_alert_text(
    feed_slug: str,
    episode_id: str,
    primary: GeminiConfig,
    reason: str,
    fallback: OpenAIConfig,
    error: BaseException | None,
) -> str:
    if error is None:
        outcome = "rendered"
    else:
        message = " ".join(str(error).split())[:200]
        outcome = f"FAILED: {message}"
    return (
        f"TTS fallback: {feed_slug} {episode_id}: "
        f"Gemini {primary.model}/{primary.voice} {reason} -> "
        f"OpenAI {fallback.voice} {outcome}"
    )


def _render_gemini_primary(
    text: str,
    config: RenderConfig,
    out_mp3: Path,
    *,
    feed_slug: str,
    episode_id: str,
    notify_fallback: bool,
    record: dict,
    chunk_records: list[dict],
) -> tuple[OpenAIConfig | GeminiConfig, str, str | None]:
    """Render with a Gemini primary; returns ``(leaf, verification, fallback_reason)``.

    Writes ``out_mp3`` or raises. Everything Gemini-specific is imported here.
    """
    from pipeline.tts import gemini_phase

    primary = config.primary
    assert isinstance(primary, GeminiConfig)
    gemini_chunks = chunk_text(text, ceiling=GeminiProvider.max_chars)
    budget_s = gemini_phase.GEMINI_BUDGET_SECONDS
    outcome = gemini_phase.run_gemini_phase(gemini_chunks, primary, budget_s=budget_s)
    record["gemini_phase"] = _phase_record(
        outcome, budget_s=budget_s, pcm_parts=outcome.pcm_parts
    )

    if outcome.ok:
        record["chunk_count"] = len(gemini_chunks)
        chunk_records.extend(_shipped_chunk_records(gemini_chunks, outcome))
        encode_mp3(b"".join(outcome.pcm_parts), out_mp3)
        return primary, "passed", None

    reason = outcome.reason or "unknown"
    if config.fallback is None:
        raise TTSRenderError(f"Gemini phase failed: {reason}")

    # All Gemini audio is discarded: the whole text is re-chunked for OpenAI and
    # rendered from scratch, so no episode ever mixes two voices.
    fallback = config.fallback
    record["fallback_reason"] = reason
    error: Exception | None = None
    try:
        pcm_parts = _synthesize_all(fallback, text, chunk_records, record)
        encode_mp3(b"".join(pcm_parts), out_mp3)
    except Exception as exc:  # noqa: BLE001 -- alerted, then re-raised below
        error = exc
    if notify_fallback:
        # After the attempt, so the alert can say how it ended; also when it failed.
        record["alert_sent"] = _deliver_alert(
            _fallback_alert_text(
                feed_slug, episode_id, primary, reason, fallback, error
            )
        )
    if error is not None:
        if isinstance(error, TTSRenderError):
            raise TTSRenderError(
                f"Gemini phase failed ({reason}) and the OpenAI fallback "
                f"failed: {error}"
            ) from error
        raise error
    return fallback, "not_run_openai", reason


def render_episode(
    text: str,
    config: RenderConfig,
    out_mp3: Path,
    *,
    feed_slug: str,
    episode_id: str,
    manifest_dir: Path | None = _DEFAULT,
    cache_dir: Path | None = _DEFAULT,
    notify_fallback: bool = True,
) -> RenderResult:
    """Render ``text`` to ``out_mp3``.

    Raises ``ValueError`` for empty text, ``TTSRenderError`` when a chunk (or
    the Gemini phase with no fallback, or both the phase and the fallback) fails
    for good, and lets anything else (missing API key, encoder failure)
    propagate unwrapped -- those are not retryable and must be loud. Every
    failure after validation still writes a ``status="failed"`` manifest.
    Cache and manifest problems never fail a render. ``manifest_dir`` /
    ``cache_dir`` default to the module constants (resolved at call time);
    ``None`` disables that side effect.

    ``notify_fallback``: a Gemini render that falls back to OpenAI sends one
    alert (after the fallback attempt, even if that fails). Local tools and dry
    runs pass ``False``. A cache hit never alerts.
    """
    if not text.strip():
        raise ValueError("cannot render empty text")
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
        "gemini_phase": None,  # set for a Gemini primary; see _phase_record
        "fallback_reason": None,
        "alert_sent": None,  # True / False / "timeout"; None = no alert attempted
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
                stored_reason = hit.result.get("fallback_reason")
                stored_reason = (
                    stored_reason if isinstance(stored_reason, str) else None
                )
                record.update(
                    fallback_reason=stored_reason,
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
                    fallback_reason=stored_reason,
                )

    leaf = config.primary  # becomes the fallback leaf if Gemini fails
    verification = "not_run_openai"
    fallback_reason: str | None = None
    try:
        if leaf.provider == "gemini":
            leaf, verification, fallback_reason = _render_gemini_primary(
                text,
                config,
                out_mp3,
                feed_slug=feed_slug,
                episode_id=episode_id,
                notify_fallback=notify_fallback,
                record=record,
                chunk_records=chunk_records,
            )
        else:
            pcm_parts = _synthesize_all(leaf, text, chunk_records, record)
            encode_mp3(b"".join(pcm_parts), out_mp3)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        emit()
        raise

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
                "verification": verification,
                "fallback_reason": fallback_reason,
                "renderer_version": RENDERER_VERSION,
                "rendered_at": datetime.now(UTC).isoformat(),
                "chunks": len(chunk_records),
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
        "TTS render: provider=%s chunks=%d audio=%.1fs wall=%.1fs cached=False"
        " fallback_reason=%s",
        leaf.provider,
        len(chunk_records),
        total_seconds,
        record["wall_seconds"],
        fallback_reason,
    )
    return RenderResult(
        provider=leaf.provider,
        config=config,
        rendered=leaf,
        cached=False,
        chunks=len(chunk_records),
        manifest_path=path,
        fallback_reason=fallback_reason,
    )
