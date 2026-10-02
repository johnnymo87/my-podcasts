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
    chunk_key,
    lookup,
    prune,
    spool_discard,
    spool_lookup,
    spool_store,
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


def _spool_get(cache_dir: Path | None, key: str | None) -> bytes | None:
    """This chunk's spooled PCM, or None. A spool problem is a miss, never a failure."""
    if cache_dir is None or key is None:
        return None
    try:
        return spool_lookup(cache_dir, key)
    except Exception:  # noqa: BLE001
        log.warning("TTS chunk spool lookup failed; synthesizing", exc_info=True)
        return None


def _spool_put(
    cache_dir: Path | None,
    key: str | None,
    leaf: OpenAIConfig | GeminiConfig,
    pcm: bytes,
) -> None:
    if cache_dir is None or key is None:
        return
    try:
        spool_store(cache_dir, key, leaf, pcm)
    except Exception:  # noqa: BLE001 -- never discard bought audio over the spool
        log.warning("TTS chunk spool store failed", exc_info=True)


def _spool_clear(cache_dir: Path, chunk_records: list[dict]) -> None:
    """Drop the spool files of a render whose completed entry is now stored."""
    try:
        spool_discard(
            cache_dir, [r["spool_key"] for r in chunk_records if r.get("spool_key")]
        )
    except Exception:  # noqa: BLE001 -- the render already succeeded
        log.warning("TTS chunk spool cleanup failed", exc_info=True)


def _synthesize_all(
    leaf: OpenAIConfig | GeminiConfig,
    text: str,
    chunk_records: list[dict],
    record: dict,
    cache_dir: Path | None = None,
) -> list[bytes]:
    """Chunk ``text`` for ``leaf``'s provider and synthesize every chunk in-process.

    Appends one record per chunk to ``chunk_records`` as it goes (so a failure
    keeps its history) and sets ``record["chunk_count"]``.

    With a ``cache_dir``, each chunk's PCM is spooled (``cache.spool_store``) as
    soon as it is bought, and a chunk already in the spool is reused instead of
    synthesized (``rec["spooled"]`` True, ``attempts`` 0). A render that fails
    partway therefore re-buys only the failed and later chunks on retry. Each
    record names its spool file stem as ``rec["spool_key"]`` (None with no
    ``cache_dir``); the caller discards those files once the completed render is
    stored, so the spool holds only chunks of renders that did not complete. This
    covers the OpenAI in-process path only -- an OpenAI primary and the OpenAI
    fallback after a failed Gemini phase. The Gemini phase itself runs in a
    killable child with per-chunk ASR verification and spools nothing, so a
    retry still re-runs it. ``cache_dir=None`` (dry runs, audition) never spools.
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
                "spooled": False,
                "spool_key": chunk_key(leaf, chunk) if cache_dir is not None else None,
            }
            chunk_records.append(rec)
            pcm = _spool_get(cache_dir, rec["spool_key"])
            if pcm is not None:
                rec["spooled"] = True
            else:
                pcm = _synthesize_chunk(provider, leaf, chunk, rec, len(chunks))
                _spool_put(cache_dir, rec["spool_key"], leaf, pcm)
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
    feed_slug,
    episode_id,
    primary,
    reason,
    fallback,
    error,
    detail="",
    failed_chunk=None,
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
            feed_slug,
            episode_id,
            primary,
            reason,
            fallback,
            error,
            detail=detail,
            failed_chunk=failed_chunk,
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
    detail: object = "",
    failed_chunk: int | None = None,
) -> str:
    if error is None:
        outcome = "rendered"
    else:
        try:
            error_text = str(error)
        except Exception:  # noqa: BLE001 -- a broken __str__: name the type instead
            error_text = ""
        message = (
            " ".join(error_text.split())[:200] if error_text else type(error).__name__
        )
        outcome = f"FAILED: {message}"
    return (
        f"TTS fallback: {feed_slug} {episode_id}: "
        f"Gemini {primary.model}/{primary.voice} {reason}"
        f"{_omission_note(reason, detail, failed_chunk)} -> "
        f"OpenAI {fallback.voice} {outcome}"
    )


# Room for the full omission summary: boilerplate plus two 12-token excerpts is
# about 200-300 characters with long words, and a cut would end the alert
# mid-quote exactly where it says what was dropped.
ALERT_DETAIL_CHARS = 320


def _omission_note(reason: str, detail: object, failed_chunk: int | None) -> str:
    """`` (chunk index <n>: <detail>)`` for a ``second_omission`` only, else "".

    ``<n>`` is the 0-based chunk index, the same number as the manifest's
    ``gemini_phase.failed_chunk``.

    Only that reason's detail is a one-line summary of what the model dropped;
    the others are tracebacks or SDK errors, and the manifest has them. Diagnostic
    text must never cost the alert: anything wrong degrades to today's text.
    """
    if reason != "second_omission":
        return ""
    try:
        text = " ".join(str(detail).split())[:ALERT_DETAIL_CHARS]
    except Exception:  # noqa: BLE001 -- a broken __str__
        return ""
    if not text:
        return ""
    if isinstance(failed_chunk, int) and not isinstance(failed_chunk, bool):
        return f" (chunk index {failed_chunk}: {text})"
    return f" ({text})"


# Clips are encoded on the render's critical path (the manifest and cache store
# wait for them), so they are bounded hard: each encode has its own short
# timeout, and the whole save has a total budget after which the rest are skipped.
OMISSION_CLIP_TIMEOUT_SECONDS = 30.0
OMISSION_CLIPS_BUDGET_SECONDS = 60.0


def _save_omission_audio(
    outcome, omission_dir: Path | None, episode_id: str, record: dict
) -> None:
    """Keep the audio the verifier rejected, as mp3s in ``omission_dir``.

    Only ever called after the episode audio was produced or has failed, never
    before. A human has to listen to settle whether an omission was real (the
    ASR that flagged it is itself fallible). Saved paths go on the matching
    attempt as ``omission_audio_file`` and in ``gemini_phase.omission_audio_files``
    (so a degraded phase record, which has no attempts, still names them).

    Bounded: each encode gets ``OMISSION_CLIP_TIMEOUT_SECONDS`` (less if the
    total is nearly spent) and the whole save ``OMISSION_CLIPS_BUDGET_SECONDS``;
    once that is spent the remaining clips are skipped. A clip that fails (or
    times out) leaves ``gemini_phase.omission_audio_error`` (the first failure,
    capped, plus a count of clips skipped for budget) and the others are still
    tried. Does nothing without an ``omission_dir`` (dry runs keep nothing).
    Telemetry: nothing in here may raise.
    """
    try:
        clips = getattr(outcome, "omission_audio", ()) or ()
        if omission_dir is None or not clips:
            return
        phase = record.get("gemini_phase")
        error: str | None = None
        saved: list[dict] = []
        skipped = 0
        stamp = _manifest_mod.utc_stamp()
        started = time.monotonic()
        for index, n, pcm in clips:
            left = OMISSION_CLIPS_BUDGET_SECONDS - (time.monotonic() - started)
            if left <= 0:
                skipped += 1
                continue
            try:
                omission_dir.mkdir(parents=True, exist_ok=True)
                path = _manifest_mod.omission_clip_path(
                    omission_dir, episode_id, stamp, chunk=index, attempt=n
                )
                encode_mp3(pcm, path, timeout=min(OMISSION_CLIP_TIMEOUT_SECONDS, left))
                abspath = os.path.abspath(path)
                saved.append({"chunk": index, "attempt": n, "file": abspath})
                _attach_clip(phase, index, n, abspath)
            except Exception as exc:  # noqa: BLE001 -- one clip costs only itself
                log.warning("omission clip c%s a%s not kept: %r", index, n, exc)
                if error is None:
                    error = f"{type(exc).__name__}: {_exc_text(exc)}"[:300]
        if skipped:
            note = (
                f"{skipped} clip(s) skipped: the "
                f"{OMISSION_CLIPS_BUDGET_SECONDS:.0f}s clip budget was spent"
            )
            error = f"{error}; {note}" if error else note
        if isinstance(phase, dict):
            if saved:
                phase["omission_audio_files"] = saved
            if error is not None:
                phase["omission_audio_error"] = error[:400]
    except Exception:  # noqa: BLE001 -- diagnostics never cost the render
        log.warning("keeping omission audio failed", exc_info=True)


def _attach_clip(phase: Any, index: int, n: int, path: str) -> None:
    """Record ``path`` on attempt ``n`` of chunk ``index`` in a ``gemini_phase``
    record, if that record still holds the attempt (a degraded one may not)."""
    if not isinstance(phase, dict):
        return
    for chunk in phase.get("chunks") or ():
        if isinstance(chunk, dict) and chunk.get("index") == index:
            for attempt in chunk.get("attempts") or ():
                if isinstance(attempt, dict) and attempt.get("n") == n:
                    attempt["omission_audio_file"] = path


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
    omission_dir: Path | None = None,
    cache_dir: Path | None = None,
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

    def finish() -> tuple[OpenAIConfig | GeminiConfig, str, str | None]:
        if outcome.ok:
            record["chunk_count"] = len(gemini_chunks)
            chunk_records.extend(_shipped_chunk_records(gemini_chunks, outcome))
            encode_mp3(b"".join(outcome.pcm_parts), out_mp3)
            return primary, "passed", None

        reason = outcome.reason or "unknown"
        if config.fallback is None:
            raise TTSRenderError(f"Gemini phase failed: {reason}") from runner_exc

        # All Gemini audio is discarded: the whole text is re-chunked for OpenAI
        # and rendered from scratch, so no episode ever mixes two voices.
        fallback = config.fallback
        record["fallback_reason"] = reason
        error: Exception | None = None
        try:
            pcm_parts = _synthesize_all(
                fallback, text, chunk_records, record, cache_dir
            )
            encode_mp3(b"".join(pcm_parts), out_mp3)
        except Exception as exc:  # noqa: BLE001 -- alerted, then re-raised below
            error = exc
        if notify_fallback:
            # After the attempt, so the alert can say how it ended; also when it
            # failed.
            record["alert_sent"] = _deliver_alert(
                feed_slug,
                episode_id,
                primary,
                reason,
                fallback,
                error,
                detail=outcome.detail,
                failed_chunk=outcome.failed_chunk,
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

    # Rejected-audio clips are saved only once the episode audio is produced or
    # has failed (an Exception), so they never delay it. A KeyboardInterrupt or
    # SystemExit propagates at once without spending time on ffmpeg.
    try:
        result = finish()
    except Exception:
        _save_omission_audio(outcome, omission_dir, episode_id, record)
        raise
    _save_omission_audio(outcome, omission_dir, episode_id, record)
    return result


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

    # Kept rejected audio lives next to the manifests; a dry run (no manifest
    # dir) keeps nothing.
    omission_dir = (
        _manifest_mod.omission_audio_dir(manifest_dir, feed_slug)
        if manifest_dir is not None
        else None
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
                omission_dir=omission_dir,
                cache_dir=cache_dir,
            )
        else:
            pcm_parts = _synthesize_all(leaf, text, chunk_records, record, cache_dir)
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
        if record["cache_stored"]:
            # Only a stored completed entry makes the spool redundant. Kept on a
            # failed store: the next attempt would otherwise re-buy every chunk.
            _spool_clear(cache_dir, chunk_records)
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
