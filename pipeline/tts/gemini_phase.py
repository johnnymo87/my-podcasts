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
``started.json``
    ``{"pid": int, "monotonic": float}``, written by the child as soon as its
    watchdog is up. ``time.monotonic()`` is system-wide on Linux, so the parent
    subtracts its own pre-``start()`` reading to get the real spawn latency
    (``Process.start()`` returns before the child has imported anything).
``result.json``
    The done marker. ``{"schema": 1, "status": "ok"|"failed", "reason": None|
    <reason>, "detail": str, "failed_chunk": None|int}`` plus, on ``ok`` only,
    ``"chunks": [{"index", "file", "bytes", "sha256"}, ...]`` in index order.
    The parent validates it against the files before trusting any audio.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
ERROR_DETAIL_CHARS = 2000  # cap on a child_error traceback in result.json
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
STARTED_NAME = "started.json"
POLL_SECONDS = 0.2  # parent: how often to look for result.json
# Older gemini-phase-* dirs are swept. Must dwarf GEMINI_BUDGET_SECONDS, or the
# sweep could delete a live phase's scratch dir (asserted just below).
STALE_SCRATCH_SECONDS = 6 * 3600
SCRATCH_PREFIX = "gemini-phase-"
if STALE_SCRATCH_SECONDS < 10 * GEMINI_BUDGET_SECONDS:  # survives python -O
    raise RuntimeError("the stale-scratch sweep could delete a live phase's dir")


def chunk_name(i: int) -> str:
    return f"chunk-{i:04d}.pcm"


def progress_name(i: int) -> str:
    return f"progress-{i:04d}.json"


# Module attributes, so in-process tests can replace them without patching the
# shared ``os`` module for everything else in the process.
_hard_exit = os._exit
_replace = os.replace


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

    There is deliberately no ``fsync``. The threat model is the child being
    SIGKILLed (by the parent's deadline, or by the OOM killer): the kernel page
    cache survives that, so a completed ``write`` + ``replace`` is visible to the
    parent. A machine crash would lose the scratch dir anyway -- it is deleted
    after every phase and never read across boots.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        _replace(tmp, path)
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
        # Best effort: progress is telemetry. A failed write must not fail a
        # chunk whose audio is fine. (The chunk PCM and result.json are strict.)
        try:
            _atomic_write_json(
                scratch / progress_name(i),
                {"schema": SCHEMA, "index": i, "attempts": attempts},
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"gemini-phase: progress-{i:04d}.json not saved: {exc!r}",
                file=sys.stderr,
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
        # First, so a sibling's failure is never overtaken by a consequence of
        # the same failure (a spurious "deadline" or "exhausted" of our own).
        if abort.is_set():
            raise _ChunkAborted()
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
            save()  # the ASR record first: it must survive a kill during the PCM write
            _atomic_write(scratch / chunk_name(i), pcm)
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
    """``exc`` with its traceback, capped at ``ERROR_DETAIL_CHARS``; also logged.

    The tail is kept when capping: a traceback ends with the exception line,
    which is the part that names the problem.
    """
    text = "".join(traceback.format_exception(exc)).rstrip()
    print(f"gemini-phase child error:\n{text}", file=sys.stderr, flush=True)
    if len(text) > ERROR_DETAIL_CHARS:
        text = "...[truncated]\n" + text[-ERROR_DETAIL_CHARS:]
    return text


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
        except BaseException as exc:  # noqa: BLE001 -- a bug in a chunk job
            # BaseException too: a worker that dies must surface as child_error
            # now, not leave the main thread waiting out the whole budget.
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
    too), runs the bootstrap, then ``_run_child``, then exits with ``os._exit``
    from a ``finally`` (so also after ``KeyboardInterrupt``/``SystemExit``):
    exit code 0 once a result is on disk, 1 if not even a ``child_error`` result
    could be written (the parent then reports ``child_no_result``). Code 3 is
    the watchdog's. stdout/stderr are flushed first, since ``os._exit`` skips it.
    """
    entered = time.monotonic()
    code = 1  # until a result is on disk
    try:
        try:
            _start_watchdog(parent_pid, deadline)
            try:  # telemetry only: never at the cost of the phase
                _atomic_write_json(
                    Path(scratch) / STARTED_NAME,
                    {"pid": os.getpid(), "monotonic": entered},
                )
            except Exception:  # noqa: BLE001
                pass
            if bootstrap is not None:
                bootstrap()
            _run_child(chunks, leaf, deadline, scratch, factories)
            code = 0
        except Exception as exc:  # noqa: BLE001
            try:
                _atomic_write_json(
                    Path(scratch) / RESULT_NAME,
                    _result_failed(REASON_CHILD_ERROR, _describe(exc)),
                )
                code = 0
            except Exception:  # noqa: BLE001
                code = 1
    finally:
        # Always exit, whatever was raised: a child that falls out of here would
        # run the spawn machinery's own teardown and could hang on a stuck thread.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass
        _hard_exit(code)


# --- the parent ----------------------------------------------------------------


@dataclass(frozen=True)
class PhaseOutcome:
    """What the Gemini phase produced.

    ``ok`` implies ``pcm_parts`` (one validated PCM blob per chunk, in order);
    otherwise ``reason`` is one of ``FALLBACK_REASONS`` and ``pcm_parts`` is
    None -- no Gemini audio is ever used from a failed phase. ``chunk_records``
    are the per-chunk progress files (attempts, tokens), telemetry only: a
    missing or unreadable one is flagged (``"progress": "missing"|"unreadable"``)
    and never fails the phase. ``spawn_s`` is how long ``Process.start()`` took;
    ``child_started_s`` is the true latency until the child was running (its own
    clock reading minus ours), or None if it never reported one.
    """

    ok: bool
    reason: str | None
    detail: str
    pcm_parts: list[bytes] | None
    chunk_records: list[dict[str, Any]]
    elapsed_s: float
    spawn_s: float
    failed_chunk: int | None = None
    child_pid: int | None = None
    child_started_s: float | None = None


class _InvalidResult(Exception):
    """result.json (or the files it names) cannot be trusted."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_result(scratch: Path) -> Any | None:
    """The parsed ``result.json``, or None if it does not exist yet.

    Writes are atomic, so a file that exists but does not parse is not a write
    in progress: it is corruption, and ``_InvalidResult``.
    """
    try:
        raw = (scratch / RESULT_NAME).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _InvalidResult(f"result.json unreadable: {exc}") from exc
    try:
        result = json.loads(raw)
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise _InvalidResult(f"result.json is not valid JSON: {exc}") from exc
    if result is None:
        raise _InvalidResult("result.json is null")
    return result


def _validate_result(result: Any, scratch: Path, n_chunks: int) -> list[bytes] | None:
    """Trust nothing the child says about its own audio.

    Returns the PCM blobs (in chunk order) for a valid ``ok`` result, or None for
    a valid ``failed`` one. Anything else raises ``_InvalidResult``: a wrong
    schema, a bad reason, indices other than exactly ``0..n-1`` in order, a file
    that is missing / not the canonical name / of a different size than declared
    / empty / odd-length (not whole 16-bit samples) / of a different sha256.
    """
    if not isinstance(result, dict):
        raise _InvalidResult("result is not an object")
    if result.get("schema") != SCHEMA:
        raise _InvalidResult(f"unknown result schema {result.get('schema')!r}")
    status = result.get("status")
    if status == "failed":
        reason = result.get("reason")
        # isinstance first: membership in a frozenset hashes, and an unhashable
        # value (a list) would raise TypeError instead of being refused.
        if not isinstance(reason, str) or reason not in FALLBACK_REASONS:
            raise _InvalidResult(f"unknown failure reason {reason!r}")
        failed_chunk = result.get("failed_chunk")
        if failed_chunk is not None and not (
            _is_int(failed_chunk) and 0 <= failed_chunk < n_chunks
        ):
            raise _InvalidResult(
                f"failed_chunk {failed_chunk!r} is not a chunk index"
                f" (0..{n_chunks - 1})"
            )
        return None
    if status != "ok":
        raise _InvalidResult(f"unknown result status {status!r}")

    chunks = result.get("chunks")
    if not isinstance(chunks, list) or not all(isinstance(c, dict) for c in chunks):
        raise _InvalidResult("ok result has no chunk list")
    indices = [c.get("index") for c in chunks]
    if indices != list(range(n_chunks)) or not all(_is_int(i) for i in indices):
        raise _InvalidResult(f"chunk indices {indices!r}, expected 0..{n_chunks - 1}")
    parts: list[bytes] = []
    for rec in chunks:
        i = rec["index"]
        name = chunk_name(i)
        if rec.get("file") != name:
            raise _InvalidResult(
                f"chunk {i} names file {rec.get('file')!r}, not {name}"
            )
        if not _is_int(rec.get("bytes")) or not isinstance(rec.get("sha256"), str):
            raise _InvalidResult(f"chunk {i} has a malformed record")
        try:
            data = (scratch / name).read_bytes()
        except OSError as exc:
            raise _InvalidResult(f"{name} missing or unreadable: {exc}") from exc
        if len(data) != rec["bytes"]:
            raise _InvalidResult(
                f"{name} has {len(data)} bytes, result declared {rec['bytes']}"
            )
        if not data:
            raise _InvalidResult(f"{name} is empty")
        if len(data) % 2:
            raise _InvalidResult(f"{name} has an odd byte count ({len(data)})")
        if hashlib.sha256(data).hexdigest() != rec["sha256"]:
            raise _InvalidResult(f"{name} sha256 does not match the result")
        parts.append(data)
    return parts


def _read_progress(scratch: Path | None, n_chunks: int) -> list[dict[str, Any]]:
    """Per-chunk progress records. Telemetry: this never raises."""
    records: list[dict[str, Any]] = []
    if scratch is None:
        return records
    for i in range(n_chunks):
        try:
            raw = (scratch / progress_name(i)).read_bytes()
        except FileNotFoundError:
            records.append({"index": i, "attempts": [], "progress": "missing"})
            continue
        except OSError:
            records.append({"index": i, "attempts": [], "progress": "unreadable"})
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("attempts"), list):
            records.append(parsed)
        else:
            records.append({"index": i, "attempts": [], "progress": "unreadable"})
    return records


def _read_started(scratch: Path | None) -> float | None:
    if scratch is None:
        return None
    try:
        started = json.loads((scratch / STARTED_NAME).read_bytes())
        value = started["monotonic"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _make_process(args: tuple) -> Any:
    """The child process object. ``spawn``, so the child shares nothing with us:
    no inherited locks, sockets or threads, and no re-run of a ``__main__``
    ending in ``.__main__`` (the consumer's ``python -m pipeline``)."""
    ctx = multiprocessing.get_context("spawn")
    return ctx.Process(target=_child_main, args=args, name="gemini-phase", daemon=True)


def _join_child(proc: Any, timeout: float) -> None:
    proc.join(timeout)


class _Reaper:
    """Kill-and-wait for one process, at most once.

    The deadline path reaps before it re-reads ``result.json``, and the runner's
    ``finally`` reaps again; without this, a child that cannot be killed would
    cost two ``REAP_TIMEOUT_SECONDS`` waits instead of one.
    """

    def __init__(self, proc: Any) -> None:
        self.proc = proc
        self._done = False

    def reap(self) -> bool:
        """Kill if running, wait (bounded) for it to die; True if it is dead."""
        if not self._done:
            self._done = True
            if self.proc.is_alive():
                try:
                    self.proc.kill()
                except Exception as exc:  # noqa: BLE001 -- it may have just died
                    print(f"gemini-phase: kill() raised {exc!r}", file=sys.stderr)
            self.proc.join(REAP_TIMEOUT_SECONDS)
        return not self.proc.is_alive()


def _await_child(
    reaper: _Reaper, scratch: Path, deadline: float
) -> tuple[Any, str | None, str]:
    """Wait for a terminal ``result.json``: ``(result, None, "")``, or
    ``(None, reason, detail)`` if there will not be one.

    A result beats everything else seen in the same poll, including a dead
    process and an expired deadline: at the deadline we kill, then look ONCE
    more, so an ``ok`` that landed as the clock ran out is not thrown away (it
    is still validated by the caller).
    """
    proc = reaper.proc
    try:
        while True:
            remaining = deadline - time.monotonic()
            _join_child(proc, max(0.0, min(POLL_SECONDS, remaining)))
            result = _read_result(scratch)
            if result is not None:
                return result, None, ""
            if not proc.is_alive():
                result = _read_result(scratch)  # written just before it exited
                if result is not None:
                    return result, None, ""
                return (
                    None,
                    REASON_CHILD_NO_RESULT,
                    f"child exited (exit code {proc.exitcode}) without a result",
                )
            if remaining <= 0:
                reaper.reap()
                result = _read_result(scratch)
                if result is not None:
                    return result, None, ""
                return None, REASON_DEADLINE, "the Gemini phase budget ran out"
    except _InvalidResult as exc:
        return None, REASON_INVALID_RESULT, str(exc)


def _sweep_stale_scratch(root: str | os.PathLike[str]) -> None:
    """Delete ``gemini-phase-*`` dirs under ``root`` untouched for 6 hours.

    A phase removes its own scratch dir, but a parent that was SIGKILLed, or a
    child that could not be killed, leaves one behind. Best effort, and it never
    raises: housekeeping must not fail a render. Only real directories (not
    symlinks) with our prefix are touched.
    """
    try:
        cutoff = time.time() - STALE_SCRATCH_SECONDS
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if (
                        entry.name.startswith(SCRATCH_PREFIX)
                        and entry.is_dir(follow_symlinks=False)
                        and entry.stat(follow_symlinks=False).st_mtime < cutoff
                    ):
                        shutil.rmtree(entry.path, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        return


def run_gemini_phase(
    chunks: list[str],
    leaf: GeminiConfig,
    *,
    budget_s: float = GEMINI_BUDGET_SECONDS,
    scratch_root: str | os.PathLike[str] | None = None,
    _factories: tuple[Callable[[], Any], Callable[[float], Any]] | None = None,
    _child_bootstrap: Callable[[], Any] | None = None,
) -> PhaseOutcome:
    """Synthesize and verify ``chunks`` in a spawned child, within ``budget_s``.

    The deadline is one absolute ``time.monotonic()`` value computed before
    anything else; the child honours it cooperatively and this function enforces
    it with ``kill()``. So it returns within ``budget_s`` + one poll interval
    (``POLL_SECONDS``) + ONE bounded reap (``REAP_TIMEOUT_SECONDS``) -- the reap
    is shared by the deadline path and the cleanup, never paid twice. A
    ``budget_s`` of zero or less returns ``deadline`` without spawning.

    On return or raise the child is dead and its scratch dir is gone, except
    that a child which could not be killed is logged and its scratch dir left in
    place (deleting it under a live process helps nobody); the next phase's
    stale sweep collects it. ``KeyboardInterrupt``/``SystemExit`` are cleaned up
    after and propagate; they are never turned into a fallback.

    ``_factories`` and ``_child_bootstrap`` are test seams (module-level
    callables, so they pickle): see ``_phase_testing``.
    """
    t0 = time.monotonic()
    deadline = t0 + budget_s
    chunks = list(chunks)
    scratch: Path | None = None
    proc: Any = None
    reaper: _Reaper | None = None
    started = False
    spawn_s = 0.0

    def outcome(
        reason: str | None,
        detail: str = "",
        *,
        pcm: list[bytes] | None = None,
        failed_chunk: int | None = None,
    ) -> PhaseOutcome:
        child_started = _read_started(scratch)
        return PhaseOutcome(
            ok=reason is None,
            reason=reason,
            detail=detail,
            pcm_parts=pcm,
            chunk_records=_read_progress(scratch, len(chunks)),
            elapsed_s=time.monotonic() - t0,
            spawn_s=spawn_s,
            failed_chunk=failed_chunk,
            child_pid=getattr(proc, "pid", None) if started else None,
            child_started_s=None if child_started is None else child_started - t0,
        )

    if budget_s <= 0:
        return outcome(REASON_DEADLINE, "no budget left for the Gemini phase")

    try:
        try:
            root = scratch_root if scratch_root is not None else tempfile.gettempdir()
            Path(root).mkdir(parents=True, exist_ok=True)
            _sweep_stale_scratch(root)
            scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=root))
        except Exception as exc:  # noqa: BLE001
            return outcome(
                REASON_SPAWN_FAILED, f"scratch dir: {type(exc).__name__}: {exc}"
            )
        args = (
            chunks,
            leaf,
            deadline,
            str(scratch),
            os.getpid(),
            _factories,
            _child_bootstrap,
        )
        try:
            proc = _make_process(args)
            before_start = time.monotonic()
            try:
                proc.start()
                started = True
            finally:
                spawn_s = time.monotonic() - before_start
        except Exception as exc:  # noqa: BLE001
            return outcome(REASON_SPAWN_FAILED, f"{type(exc).__name__}: {exc}")

        reaper = _Reaper(proc)
        result, reason, detail = _await_child(reaper, scratch, deadline)
        if reason is not None:
            return outcome(reason, detail)
        try:
            parts = _validate_result(result, scratch, len(chunks))
        except _InvalidResult as exc:
            return outcome(REASON_INVALID_RESULT, str(exc))
        if parts is None:  # a valid failure the child reported itself
            return outcome(
                result["reason"],
                str(result.get("detail", ""))[: 2 * ERROR_DETAIL_CHARS],
                failed_chunk=result.get("failed_chunk"),
            )
        return outcome(None, pcm=parts)
    finally:
        # The child is dead before the scratch dir goes: a live child writing
        # into a deleted directory is how a stray file would outlive a phase.
        #
        # By design, a KeyboardInterrupt that lands inside ``proc.start()`` leaves
        # ``started`` False, so the child is not reaped here. If the fork did
        # happen, that child is a self-terminating orphan: its watchdog sees the
        # parent gone (or the deadline pass) and exits, and the next phase's
        # stale sweep removes its scratch dir. Reaping a process whose start
        # never returned would mean guessing at a half-built Process object.
        child_dead = True
        if started:  # a process whose start() never returned is not reaped
            try:
                child_dead = (reaper or _Reaper(proc)).reap()
            except Exception as exc:  # noqa: BLE001 -- never mask the real outcome
                print(
                    f"gemini-phase: reaping the child failed: {exc!r}", file=sys.stderr
                )
                child_dead = False
            if child_dead:
                try:
                    proc.close()  # releases the sentinel fd
                except Exception:  # noqa: BLE001
                    pass
        if scratch is not None:
            if child_dead:
                shutil.rmtree(scratch, ignore_errors=True)
            else:
                print(
                    f"gemini-phase: child pid {getattr(proc, 'pid', '?')} is still "
                    f"alive after kill; leaving {scratch} in place",
                    file=sys.stderr,
                )
