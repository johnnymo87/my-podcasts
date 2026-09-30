"""The bounded Gemini phase: synthesize + verify chunks in a killable child.

``render_episode`` (T3b, Task 4) runs a Gemini primary as one *phase*: a spawned
child process synthesizes every chunk and verifies it by ASR, under a hard
deadline the parent enforces by ``kill()``. Any problem discards all Gemini audio
and the parent renders the whole episode with the OpenAI fallback. The child is
the only place a request can hang the process for longer than its per-read
timeout, which is why it is a process and not a thread.

This module has two halves. The **child** half (this file, part 1) is plain
functions the tests call in-process: ``_render_chunk`` (one chunk's state
machine), ``_run_child`` (executor + terminal result) and ``_child_main`` (the
spawn target: bootstrap, watchdog, ``_run_child``, then ``os._exit``). The
**parent** half (``run_gemini_phase``) is added in Task 3.

Files, all in the parent-created scratch directory and all written atomically
(temp file in the same directory + ``os.replace``):

``chunk-NNNN.pcm``
    A *verified* chunk's raw PCM (24 kHz mono s16le). Only verified audio is
    ever written here.
``progress-NNNN.json``
    ``{"schema": 1, "index": i, "attempts": [attempt, ...]}``, rewritten before
    each stage starts so a kill leaves the record of what was in flight. An
    attempt is one TTS call::

        {"n": 1,
         "synth": {"status": "started"|"ok"|"error", "kind": None|"content"|
                   "infra"|"fatal", "error": None|str, "elapsed_s": None|float,
                   "finish_reason": None|str, "prompt_tokens": None|int,
                   "audio_tokens": None|int, "pcm_bytes": None|int},
         "asr": None | {"status": "started"|"pass"|"omission"|"unavailable",
                        "reasons": [str], "detail": str, "recall": None|float,
                        "elapsed_s": None|float, "input_tokens": None|int,
                        "output_tokens": None|int, "thinking_tokens": None|int},
         "outcome": None|"transient_error"|"fatal"|"omission"|
                    "asr_unavailable"|"verified"}

    ``None`` tokens mean *unknown* (the request was killed, or failed before
    reporting usage), never zero. Synth and ASR tokens are separate, and a
    discarded omission attempt keeps its own.
``result.json``
    The done marker. ``{"schema": 1, "status": "ok"|"failed", "reason": None|
    <reason>, "detail": str, "failed_chunk": None|int}`` plus, on ``ok`` only,
    ``"chunks": [{"index", "file", "bytes", "sha256"}, ...]`` in index order.
    The parent validates it against the files before trusting any audio.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.tts.asr import GeminiTranscriber, pcm_to_wav
from pipeline.tts.providers import GeminiProvider, TTSProviderError
from pipeline.tts.verify import verify_audio


if TYPE_CHECKING:
    from collections.abc import Callable

    from pipeline.tts.config import GeminiConfig


# --- budgets -------------------------------------------------------------------

GEMINI_BUDGET_SECONDS = 360.0
REAP_TIMEOUT_SECONDS = 5.0  # parent: join() after kill()
REQUEST_TIMEOUT_CAP_SECONDS = 90.0  # per synth / per ASR request
MAX_TTS_CALLS = 3  # one counter: transient retries AND the omission re-render
BACKOFF_SECONDS = (2.0, 8.0)  # before TTS call 2, before TTS call 3
MAX_WORKERS = 4
WATCHDOG_POLL_SECONDS = 0.25
# The parent kills at the deadline anyway. The grace only lets the main thread
# write a terminal ``deadline`` result first, so the watchdog is a backstop and
# not a race with the normal path.
WATCHDOG_GRACE_SECONDS = 1.0

# --- fallback reasons: a closed set --------------------------------------------

REASON_FATAL = "fatal"
REASON_EXHAUSTED = "exhausted"
REASON_DEADLINE = "deadline"
REASON_ASR_UNAVAILABLE = "asr_unavailable"
REASON_SECOND_OMISSION = "second_omission"
REASON_CHILD_ERROR = "child_error"
REASON_CHILD_NO_RESULT = "child_no_result"
REASON_INVALID_RESULT = "invalid_result"
REASON_SPAWN_FAILED = "spawn_failed"
FALLBACK_REASONS = frozenset(
    {
        REASON_FATAL,
        REASON_EXHAUSTED,
        REASON_DEADLINE,
        REASON_ASR_UNAVAILABLE,
        REASON_SECOND_OMISSION,
        REASON_CHILD_ERROR,
        REASON_CHILD_NO_RESULT,
        REASON_INVALID_RESULT,
        REASON_SPAWN_FAILED,
    }
)

# --- files ---------------------------------------------------------------------

SCHEMA = 1
RESULT_NAME = "result.json"


def chunk_name(i: int) -> str:
    return f"chunk-{i:04d}.pcm"


def progress_name(i: int) -> str:
    return f"progress-{i:04d}.json"


# A child calls these (module attributes, so in-process tests can replace them).
_hard_exit = os._exit


class _ChunkFailure(Exception):
    """One chunk cannot be rendered: the whole phase fails with ``reason``."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class _ChunkAborted(Exception):
    """Another chunk failed (or the phase is over): stop quietly."""


# --- atomic writes -------------------------------------------------------------


def _atomic_write(path: Path | str, data: bytes) -> None:
    """Write ``data`` so a reader sees the old file or the whole new one.

    The temp file sits in the same directory (``os.replace`` is only atomic
    within a filesystem) and is removed if anything fails.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path | str, obj: Any) -> None:
    _atomic_write(path, json.dumps(obj, indent=2, sort_keys=True).encode("utf-8"))


# --- one chunk -----------------------------------------------------------------


def _abortable_wait(abort: threading.Event, seconds: float) -> bool:
    """Sleep ``seconds`` or until ``abort`` is set; True if it was set."""
    return abort.wait(seconds)


def _new_attempt(n: int) -> dict[str, Any]:
    return {
        "n": n,
        "synth": {
            "status": "started",
            "kind": None,
            "error": None,
            "elapsed_s": None,
            "finish_reason": None,
            "prompt_tokens": None,
            "audio_tokens": None,
            "pcm_bytes": None,
        },
        "asr": None,
        "outcome": None,
    }


def _asr_record(verdict) -> dict[str, Any]:
    info = verdict.asr
    return {
        "status": verdict.status,
        "reasons": list(verdict.reasons),
        "detail": verdict.detail,
        "recall": verdict.recall,
        "elapsed_s": info.elapsed_s if info else None,
        "input_tokens": info.input_tokens if info else None,
        "output_tokens": info.output_tokens if info else None,
        "thinking_tokens": info.thinking_tokens if info else None,
    }


def _render_chunk(
    i: int,
    chunk: str,
    leaf: GeminiConfig,
    deadline: float,
    scratch: Path | str,
    provider: Any,
    make_transcriber: Callable[[float], Any],
    abort: threading.Event,
    sleep: Callable[[threading.Event, float], bool] | None = None,
) -> dict[str, Any]:
    """Synthesize and verify one chunk; write its PCM; return its result record.

    One counter of ``MAX_TTS_CALLS`` covers transient retries and the single
    omission re-render, so a chunk makes at most 3 TTS calls and 2 ASR calls.
    Raises ``_ChunkFailure`` (and sets ``abort``, so siblings stop) when the
    chunk cannot be produced, or ``_ChunkAborted`` when a sibling already failed.

    ``sleep(abort, seconds) -> aborted`` is injectable; the default waits on the
    abort event, so a backoff ends as soon as another chunk fails.
    """
    try:
        return _render_chunk_inner(
            i,
            chunk,
            leaf,
            deadline,
            Path(scratch),
            provider,
            make_transcriber,
            abort,
            sleep or _abortable_wait,
        )
    except _ChunkFailure:
        abort.set()
        raise


def _render_chunk_inner(
    i, chunk, leaf, deadline, scratch, provider, make_transcriber, abort, sleep
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []

    def save() -> None:
        _atomic_write_json(
            scratch / progress_name(i),
            {"schema": SCHEMA, "index": i, "attempts": attempts},
        )

    def remaining() -> float:
        return deadline - time.monotonic()

    def checkpoint() -> None:
        if abort.is_set():
            raise _ChunkAborted()
        if remaining() <= 0:
            raise _ChunkFailure(REASON_DEADLINE, "the phase budget ran out")

    tts_calls = 0
    omissions = 0
    backoff_due = False
    last_problem = "no attempt made"

    while True:
        if tts_calls >= MAX_TTS_CALLS:
            raise _ChunkFailure(
                REASON_EXHAUSTED,
                f"{MAX_TTS_CALLS} TTS calls used up; last: {last_problem}",
            )
        if backoff_due:
            delay = BACKOFF_SECONDS[tts_calls - 1]
            if remaining() < delay:
                raise _ChunkFailure(
                    REASON_DEADLINE,
                    f"{remaining():.1f}s left, shorter than the {delay:.0f}s "
                    f"backoff; last: {last_problem}",
                )
            if sleep(abort, delay):
                raise _ChunkAborted()
            backoff_due = False
        checkpoint()

        tts_calls += 1
        attempt = _new_attempt(tts_calls)
        attempts.append(attempt)
        save()  # "started", before the request
        synth = attempt["synth"]
        try:
            result = provider.synthesize_detailed(
                chunk, leaf, timeout=min(remaining(), REQUEST_TIMEOUT_CAP_SECONDS)
            )
        except TTSProviderError as exc:
            synth.update(status="error", kind=exc.kind, error=str(exc))
            last_problem = str(exc)
            if exc.kind == "fatal":
                attempt["outcome"] = "fatal"
                save()
                raise _ChunkFailure(REASON_FATAL, str(exc)) from exc
            attempt["outcome"] = "transient_error"
            save()
            backoff_due = True
            continue
        pcm = result.pcm
        synth.update(
            status="ok",
            elapsed_s=result.elapsed_s,
            finish_reason=result.finish_reason,
            prompt_tokens=result.prompt_tokens,
            audio_tokens=result.audio_tokens,
            pcm_bytes=len(pcm),
        )
        save()
        checkpoint()

        attempt["asr"] = {
            "status": "started",
            "reasons": [],
            "detail": "",
            "recall": None,
            "elapsed_s": None,
            "input_tokens": None,
            "output_tokens": None,
            "thinking_tokens": None,
        }
        save()  # "started", before the request
        transcriber = make_transcriber(min(remaining(), REQUEST_TIMEOUT_CAP_SECONDS))
        try:
            verdict = verify_audio(
                pcm_to_wav(pcm), "audio/wav", chunk, transcriber=transcriber
            )
        finally:
            close = getattr(transcriber, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 -- closing must never raise
                    pass
        attempt["asr"] = _asr_record(verdict)

        if verdict.status == "pass":
            attempt["outcome"] = "verified"
            _atomic_write(scratch / chunk_name(i), pcm)
            save()
            return {
                "index": i,
                "file": chunk_name(i),
                "bytes": len(pcm),
                "sha256": hashlib.sha256(pcm).hexdigest(),
            }
        if verdict.status == "unavailable":
            attempt["outcome"] = "asr_unavailable"
            save()
            raise _ChunkFailure(
                REASON_ASR_UNAVAILABLE,
                f"{','.join(verdict.reasons)}: {verdict.detail}",
            )
        # omission: the audio is discarded; re-render once if the counter allows
        attempt["outcome"] = "omission"
        save()
        omissions += 1
        last_problem = f"omission ({','.join(verdict.reasons) or 'flagged'})"
        if omissions >= 2:
            raise _ChunkFailure(REASON_SECOND_OMISSION, last_problem)


# --- the child process ---------------------------------------------------------


def _default_make_provider() -> GeminiProvider:
    return GeminiProvider()


def _default_make_transcriber(timeout_s: float) -> GeminiTranscriber:
    return GeminiTranscriber(timeout_s=timeout_s)


DEFAULT_FACTORIES = (_default_make_provider, _default_make_transcriber)


def _result_ok(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "ok",
        "reason": None,
        "detail": "",
        "failed_chunk": None,
        "chunks": sorted(chunks, key=lambda c: c["index"]),
    }


def _result_failed(
    reason: str, detail: str, failed_chunk: int | None = None
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "failed",
        "reason": reason,
        "detail": detail,
        "failed_chunk": failed_chunk,
    }


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _execute(
    chunks: list[str],
    leaf: GeminiConfig,
    deadline: float,
    scratch: Path,
    factories: tuple[Callable[[], Any], Callable[[float], Any]],
) -> dict[str, Any]:
    if not chunks:
        raise ValueError("no chunks to render")
    make_provider, make_transcriber = factories
    provider = make_provider()
    abort = threading.Event()
    events: queue.Queue[tuple[str, int, Any]] = queue.Queue()

    def job(i: int) -> None:
        try:
            rec = _render_chunk(
                i, chunks[i], leaf, deadline, scratch, provider, make_transcriber, abort
            )
            events.put(("ok", i, rec))
        except _ChunkAborted:
            events.put(("aborted", i, None))
        except _ChunkFailure as exc:
            events.put(("failed", i, exc))
        except Exception as exc:  # noqa: BLE001 -- a bug in a chunk job
            abort.set()
            events.put(("error", i, exc))

    executor = ThreadPoolExecutor(
        max_workers=min(MAX_WORKERS, len(chunks)), thread_name_prefix="gemini-chunk"
    )
    try:
        for i in range(len(chunks)):
            executor.submit(job, i)
        done: dict[int, dict[str, Any]] = {}
        while len(done) < len(chunks):
            try:
                kind, i, payload = events.get(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            except queue.Empty:
                return _result_failed(REASON_DEADLINE, "the phase budget ran out")
            if kind == "ok":
                done[i] = payload
            elif kind == "failed":
                return _result_failed(payload.reason, payload.detail, i)
            elif kind == "error":
                return _result_failed(REASON_CHILD_ERROR, _describe(payload), i)
            # "aborted": a sibling's failure is already on its way to the queue
        return _result_ok(list(done.values()))
    finally:
        # Never wait for in-flight requests: a stuck socket would hold the
        # process open. The child exits via os._exit right after the result.
        executor.shutdown(wait=False, cancel_futures=True)
        close = getattr(provider, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001 -- closing must never raise
                pass


def _run_child(
    chunks: list[str],
    leaf: GeminiConfig,
    deadline: float,
    scratch: Path | str,
    factories: tuple[Callable[[], Any], Callable[[float], Any]] | None = None,
) -> dict[str, Any]:
    """Everything the child does before exiting; returns the result it wrote.

    The terminal ``result.json`` is written *before* return, so a caller that
    then ``os._exit``s never loses it. Any exception is a ``child_error``
    result; only a failure to write the result itself propagates.
    """
    scratch = Path(scratch)
    try:
        result = _execute(
            chunks, leaf, deadline, scratch, factories or DEFAULT_FACTORIES
        )
    except Exception as exc:  # noqa: BLE001
        result = _result_failed(REASON_CHILD_ERROR, _describe(exc))
    _atomic_write_json(scratch / RESULT_NAME, result)
    return result


def _watchdog_loop(
    parent_pid: int,
    deadline: float,
    *,
    poll_s: float,
    getppid: Callable[[], int],
    clock: Callable[[], float],
    exit_fn: Callable[[int], Any],
    stop: threading.Event | None = None,
) -> None:
    """Exit 3 if the parent is gone or the deadline (plus grace) has passed."""
    while stop is None or not stop.is_set():
        if getppid() != parent_pid or clock() > deadline + WATCHDOG_GRACE_SECONDS:
            exit_fn(3)
            return
        if stop is None:
            time.sleep(poll_s)
        else:
            stop.wait(poll_s)


def _start_watchdog(parent_pid: int, deadline: float) -> threading.Thread:
    thread = threading.Thread(
        target=_watchdog_loop,
        args=(parent_pid, deadline),
        kwargs={
            "poll_s": WATCHDOG_POLL_SECONDS,
            "getppid": os.getppid,
            "clock": time.monotonic,
            "exit_fn": _hard_exit,
        },
        name="gemini-watchdog",
        daemon=True,
    )
    thread.start()
    return thread


def _child_main(
    chunks: list[str],
    leaf: GeminiConfig,
    deadline: float,
    scratch: str,
    parent_pid: int,
    factories: tuple[Callable[[], Any], Callable[[float], Any]] | None = None,
    bootstrap: Callable[[], Any] | None = None,
) -> None:
    """Spawn target. Module-level, with only picklable arguments.

    Starts the watchdog (before the bootstrap, so a hung bootstrap is bounded
    too), runs the bootstrap, then ``_run_child``, then exits with ``os._exit``:
    exit code 0 once a result is on disk, 1 if not even a ``child_error`` result
    could be written (the parent then reports ``child_no_result``). Code 3 is
    the watchdog's.
    """
    code = 0
    try:
        _start_watchdog(parent_pid, deadline)
        if bootstrap is not None:
            bootstrap()
        _run_child(chunks, leaf, deadline, scratch, factories)
    except Exception as exc:  # noqa: BLE001
        try:
            _atomic_write_json(
                Path(scratch) / RESULT_NAME,
                _result_failed(REASON_CHILD_ERROR, _describe(exc)),
            )
        except Exception:  # noqa: BLE001
            code = 1
    _hard_exit(code)
