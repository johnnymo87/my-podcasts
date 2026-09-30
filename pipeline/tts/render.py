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
import dataclasses
import hashlib
import json
import logging
import os
import queue
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
# The fallback alert is best-effort and bounded. ``send_alert`` has a per-read
# (10 s), not wall-clock, timeout, so it runs on ONE module-level daemon worker
# and a render waits at most this long for ITS alert; after that it moves on.
ALERT_WAIT_SECONDS = 12.0
# At most this many alerts may wait behind a stuck sender; beyond it they are
# dropped (logged), so repeated fallbacks can never pile up threads or memory.
ALERT_QUEUE_MAX = 8


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


def _exc_text(exc: BaseException) -> str:
    """``str(exc)`` that cannot raise: an exception with a broken ``__str__`` must
    not turn reporting into a second failure."""
    try:
        return str(exc)
    except Exception:  # noqa: BLE001
        return "<unprintable>"


def _stage_completed(stage: str, part: dict) -> bool:
    """Did this request finish and report usage, as opposed to being killed,
    still in flight, or failed before any usage came back?"""
    status = part.get("status")
    if stage == "synth":
        return status == "ok"
    if status in ("pass", "omission"):
        return True
    # ASR "unavailable" is a verdict, not a failed call, when the transcript came
    # back (``asr_empty``): the record then carries the request's elapsed time.
    return status == "unavailable" and part.get("elapsed_s") is not None


def _phase_totals(phase_chunk_records: list) -> tuple[dict, float | None]:
    """Token totals and generated audio seconds across every Gemini attempt.

    A total is ``None`` when ANY contributing count is genuinely unknown -- a
    request that was killed or is still in flight, one that errored without
    usage, a chunk with no readable progress, anything malformed -- because a
    sum that silently omits a cost is worse than no sum. The generated-seconds
    figure is ``None`` in the same cases a chunk's attempts cannot be read at
    all. An attempt that never reached ASR contributes nothing to the ASR totals
    (nothing was requested). The one absence that is NOT unknown: a *completed*
    call with no thinking count means the model reported none, which totals as 0
    (the raw per-attempt value stays as reported, in ``gemini_phase.chunks``).

    The records come from files a child wrote, so nothing about their shape is
    trusted: a non-dict anywhere marks the totals unknown rather than raising.
    """
    totals: dict[str, int | None] = {name: 0 for name, _, _ in _TOKEN_FIELDS}
    pcm_bytes = 0
    seconds_known = True

    def unknown_chunk() -> None:
        nonlocal totals, seconds_known
        totals = dict.fromkeys(totals)  # we cannot know what this chunk cost
        seconds_known = False

    for rec in phase_chunk_records:
        if not isinstance(rec, dict) or rec.get("progress") in (
            "missing",
            "unreadable",
        ):
            unknown_chunk()
            continue
        attempts = rec.get("attempts", [])
        if not isinstance(attempts, list):
            unknown_chunk()
            continue
        for attempt in attempts:
            if not isinstance(attempt, dict):
                unknown_chunk()
                continue
            synth = attempt.get("synth")
            if isinstance(synth, dict) and isinstance(synth.get("pcm_bytes"), int):
                pcm_bytes += synth["pcm_bytes"]
            for name, stage, field in _TOKEN_FIELDS:
                part = attempt.get(stage)
                if part is None:
                    continue  # the stage never ran: nothing to count
                if not isinstance(part, dict):
                    totals[name] = None
                    continue
                value = part.get(field)
                if value is None and field == "thinking_tokens":
                    if _stage_completed(stage, part):
                        value = 0
                if isinstance(value, int) and not isinstance(value, bool):
                    if totals[name] is not None:
                        totals[name] += value
                else:
                    totals[name] = None
    return totals, (pcm_bytes / PCM_BYTES_PER_SECOND if seconds_known else None)


def _phase_record(outcome, *, budget_s: float) -> dict:
    """The manifest's ``gemini_phase`` block. Kept apart from ``chunks``, which
    is only the audio that shipped.

    Telemetry must never cost a render: if building the full record fails for
    any reason, degrade to a minimal one (tokens unknown, a ``telemetry_error``
    note) and carry on.
    """
    unknown_tokens = dict.fromkeys(name for name, _, _ in _TOKEN_FIELDS)
    base = {
        "outcome": "ok" if getattr(outcome, "ok", False) else "failed",
        "reason": getattr(outcome, "reason", None),
        "detail": getattr(outcome, "detail", ""),
        "failed_chunk": getattr(outcome, "failed_chunk", None),
        "budget_s": budget_s,
        "elapsed_s": getattr(outcome, "elapsed_s", None),
        "spawn_s": getattr(outcome, "spawn_s", None),
        "child_started_s": getattr(outcome, "child_started_s", None),
        "child_pid": getattr(outcome, "child_pid", None),
    }
    try:
        records = outcome.chunk_records
        if records:
            tokens, generated = _phase_totals(records)
        else:  # e.g. the runner itself failed: what the phase did or cost is unknown
            tokens, generated = unknown_tokens, None
        pcm_parts = outcome.pcm_parts if outcome.ok else None
        used = (
            sum(len(p) for p in pcm_parts) / PCM_BYTES_PER_SECOND if pcm_parts else 0.0
        )
        json.dumps(records, default=str)  # the manifest must be able to hold it
        return {
            **base,
            "chunks": records,
            "tokens": tokens,
            "audio_seconds_generated": generated,
            "audio_seconds_used": used,
        }
    except Exception as exc:  # noqa: BLE001 -- telemetry must never cost the render
        log.warning("Gemini phase telemetry failed (%r); using a minimal record", exc)
        return {
            **base,
            "chunks": [],
            "tokens": unknown_tokens,
            "audio_seconds_generated": None,
            "audio_seconds_used": None,
            "telemetry_error": f"{type(exc).__name__}: {_exc_text(exc)}"[:300],
        }


def _attempt_errors(attempts: list) -> list[str]:
    errors = []
    for a in attempts:
        synth = a.get("synth") if isinstance(a, dict) else None
        if isinstance(synth, dict) and synth.get("error"):
            errors.append(str(synth["error"]))
    return errors


def _shipped_chunk_records(gemini_chunks: list[str], outcome) -> list[dict]:
    """``chunks`` records for a successful Gemini phase (attempts from progress).

    The audio and its per-chunk seconds come from validated PCM; the attempt
    counts and errors come from child-written progress, which is telemetry and so
    is read defensively: a chunk whose progress cannot be read reports
    ``attempts: None`` instead of failing the render.
    """
    by_index: dict = {}
    try:
        by_index = {
            r.get("index"): r for r in outcome.chunk_records if isinstance(r, dict)
        }
    except Exception:  # noqa: BLE001
        by_index = {}
    records = []
    for i, chunk in enumerate(gemini_chunks):
        attempts_n: int | None
        errors: list[str]
        try:
            attempts = by_index.get(i, {}).get("attempts", [])
            attempts_n = len(attempts)
            errors = _attempt_errors(attempts)
        except Exception:  # noqa: BLE001
            attempts_n, errors = None, []
        records.append(
            {
                "index": i,
                "chars": len(chunk),
                "attempts": attempts_n,
                "errors": errors,
                "audio_seconds": len(outcome.pcm_parts[i]) / PCM_BYTES_PER_SECOND,
            }
        )
    return records


_STOP = object()  # tells the alert worker to exit
_alert_lock = threading.Lock()
_alert_queue: queue.Queue = queue.Queue()
_alert_worker: threading.Thread | None = None


class _AlertJob:
    __slots__ = ("done", "result", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self.done = threading.Event()
        self.result = False


def _alert_worker_loop() -> None:
    while True:
        job = _alert_queue.get()
        if job is _STOP:
            return
        try:
            # Imported inside the try: a failing import must cost this alert,
            # not kill the worker and strand every queued job.
            from pipeline import alerts

            # Looked up on the module at call time so a test can replace it.
            job.result = bool(alerts.send_alert(job.text))
        except Exception:  # noqa: BLE001 -- reporting must never disturb anything
            job.result = False
        finally:
            job.done.set()


def _enqueue_alert(text: str) -> _AlertJob | None:
    """Queue ``text`` for the single worker; None if the queue is full.

    Starts the worker on first use, and again if it has died -- never a second
    one alongside a live (even stuck) worker. A full queue means the worker is
    stuck behind earlier alerts: do not add a thread, drop this one.
    """
    global _alert_worker
    with _alert_lock:
        if _alert_worker is None or not _alert_worker.is_alive():
            worker = threading.Thread(
                target=_alert_worker_loop, name="tts-alert-worker", daemon=True
            )
            worker.start()  # may raise; the global is only set on success
            _alert_worker = worker
        if _alert_queue.qsize() >= ALERT_QUEUE_MAX:
            return None
        job = _AlertJob(text)
        _alert_queue.put(job)
        return job


def _stop_alert_worker(timeout: float = 5.0) -> None:
    """Drop queued alerts and stop the worker (tests; never needed in production)."""
    global _alert_worker
    with _alert_lock:
        worker, _alert_worker = _alert_worker, None
        while True:
            try:
                _alert_queue.get_nowait()
            except queue.Empty:
                break
        if worker is not None and worker.is_alive():
            _alert_queue.put(_STOP)
    if worker is not None:
        worker.join(timeout)


def _deliver_alert(
    feed_slug, episode_id, primary, reason, fallback, error
) -> bool | str:
    """Alert that a render fell back, without letting anything about it reach the
    caller: an episode that has been rendered must not be lost to its own report.

    The alert goes to one module-level daemon worker through a bounded queue, and
    this waits at most ``ALERT_WAIT_SECONDS`` for THIS alert. Returns True/False
    (``send_alert``'s own answer; any exception, including failing to build the
    text or start the worker, is False); ``"timeout"`` (queued, but not finished
    in time: delivery UNKNOWN, it may still go out late); or ``"dropped"`` (the
    queue was full behind a stuck sender: NOT sent). Timed-out and dropped alerts
    are logged in full, so the text is never lost.
    """
    text = None
    try:
        text = _fallback_alert_text(
            feed_slug, episode_id, primary, reason, fallback, error
        )
        job = _enqueue_alert(text)
        if job is None:
            log.warning("TTS fallback alert dropped (queue full); text was: %s", text)
            return "dropped"
        if not job.done.wait(ALERT_WAIT_SECONDS):
            log.warning(
                "TTS fallback alert not delivered within %.0fs; text was: %s",
                ALERT_WAIT_SECONDS,
                text,
            )
            return "timeout"
        return job.result
    except Exception:  # noqa: BLE001
        log.warning(
            "TTS fallback alert could not be sent; text was: %s", text, exc_info=True
        )
        return False


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
        try:
            detail = str(error)
        except Exception:  # noqa: BLE001 -- a broken __str__: name the type instead
            detail = ""
        message = " ".join(detail.split())[:200] if detail else type(error).__name__
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
    runner_exc: Exception | None = None
    started = time.monotonic()
    try:
        outcome = gemini_phase.run_gemini_phase(
            gemini_chunks, primary, budget_s=budget_s
        )
    except Exception as exc:  # noqa: BLE001
        # A bug in the runner must not cost the episode: a Gemini problem of any
        # kind costs an OpenAI episode. (KeyboardInterrupt/SystemExit are not
        # Exceptions and propagate; the runner has already cleaned up after them.)
        log.exception("Gemini phase runner raised; falling back")
        runner_exc = exc
        outcome = gemini_phase.runner_error_outcome(
            exc, elapsed_s=time.monotonic() - started
        )
    if outcome.ok:
        # The runner validates before it says ok; this is the last line of
        # defence, and what makes a wrong-shaped "ok" a fallback, not a bad episode.
        parts = outcome.pcm_parts
        if (
            not isinstance(parts, list)
            or len(parts) != len(gemini_chunks)
            or not all(isinstance(p, bytes) and p for p in parts)
        ):
            outcome = dataclasses.replace(
                outcome,
                ok=False,
                reason=gemini_phase.REASON_INVALID_RESULT,
                detail=(
                    f"phase reported ok but returned "
                    f"{len(parts) if isinstance(parts, list) else parts!r} audio "
                    f"part(s) for {len(gemini_chunks)} chunk(s)"
                ),
                pcm_parts=None,
            )
    record["gemini_phase"] = _phase_record(outcome, budget_s=budget_s)

    if outcome.ok:
        record["chunk_count"] = len(gemini_chunks)
        chunk_records.extend(_shipped_chunk_records(gemini_chunks, outcome))
        encode_mp3(b"".join(outcome.pcm_parts), out_mp3)
        return primary, "passed", None

    reason = outcome.reason or "unknown"
    if config.fallback is None:
        raise TTSRenderError(f"Gemini phase failed: {reason}") from runner_exc

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
            feed_slug, episode_id, primary, reason, fallback, error
        )
    if error is not None:
        if isinstance(error, TTSRenderError):
            raise TTSRenderError(
                f"Gemini phase failed ({reason}) and the OpenAI fallback "
                f"failed: {_exc_text(error)}"
            ) from error
        error.add_note(f"after Gemini {reason}")
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
    key: str | None = None  # computed below, inside the guard that writes manifests
    chunk_records: list[dict] = []
    record: dict = {
        "renderer_version": RENDERER_VERSION,
        "feed_slug": feed_slug,
        "episode_id": episode_id,
        "started_at": started_at.isoformat(),
        "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "input_chars": len(text),
        "cache_key": None,
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

    try:
        # For a Gemini primary this imports ``verify`` (and so ``google.genai``);
        # a broken import there is a failed render with a manifest, not a bare
        # traceback. (It is deliberately not a fallback: nothing has run yet, and
        # a broken install needs to be loud.)
        key = cache_key(text, config)
    except BaseException as exc:
        record["error"] = f"{type(exc).__name__}: {_exc_text(exc)}"
        emit()
        raise
    record["cache_key"] = key

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
    except BaseException as exc:
        # BaseException: a KeyboardInterrupt/SystemExit must still leave the
        # failed manifest behind -- and still propagate, never becoming a fallback.
        record["error"] = f"{type(exc).__name__}: {_exc_text(exc)}"
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
