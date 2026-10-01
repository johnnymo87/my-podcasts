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
``omission-NNNN-N.pcm``
    The raw PCM of chunk NNNN's TTS attempt N when ASR rejected it as an
    omission, written best-effort and atomically *before* the progress record
    that names it (``attempt["omission_audio"]``). Never used as episode audio;
    the runner reads at most 4 of them into ``PhaseOutcome.omission_audio`` for
    a human to listen to. The parent's validation ignores them.
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
                    "asr_unavailable"|"verified",
         "omission_audio": <omission-NNNN-N.pcm>}  # only on a kept omission

    A span is ``{"script_start", "script_end", "script_words",
    "transcript_words", "net_missing", "flagged", "excerpt", "heard"}``:
    coordinates are normalized-token indices into the chunk, ``excerpt`` is the
    script's first <=30 normalized tokens of the gap and ``heard`` the ASR's.
    An ``omission`` record keeps every flagged span plus the largest unflagged
    ones (at least 3, at most 8) and the raw ASR ``transcript`` (capped); a
    ``pass`` keeps its single largest-``net_missing`` span. All of this is
    diagnostic and best-effort: a failure building it leaves the original fields
    intact and the new ones ``None`` / ``[]``.

    ``None`` tokens mean *unknown* (the request was killed, or failed before
    reporting usage), never zero. Synth and ASR tokens are separate, and a
    discarded omission attempt keeps its own.
``input.json``
    ``{"schema": 1, "chunks": [str, ...], "leaf": asdict(GeminiConfig)}``, written
    by the PARENT, atomically, before the child starts. The episode text does not
    travel through ``Process.start()``'s pipe: a child that stopped reading that
    pipe would block the parent inside ``start()``, past any deadline. The spawn
    arguments are a few hundred bytes (scratch path, deadline, parent pid, the
    test seams); the child reads and validates this file itself.
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
import stat
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.tts.asr import GeminiTranscriber, pcm_to_wav
from pipeline.tts.config import GeminiConfig, leaf_from_dict
from pipeline.tts.providers import GeminiProvider, TTSProviderError
from pipeline.tts.verify import verify_audio


if TYPE_CHECKING:
    from collections.abc import Callable


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

# What an ASR record keeps so a verdict can be diagnosed afterwards (T6 prereq).
MAX_RECORDED_SPANS = 8  # an omission records its flagged spans, then clues, up to this
MIN_RECORDED_OMISSION_SPANS = 3  # an omission records at least this many (if any exist)
MAX_RECORDED_TRANSCRIPT_CHARS = 6000  # omission only; a 3000-char chunk is ~3000
SUMMARY_TOKENS = 12  # script / heard excerpt length in the omission summary

# The audio an omission verdict rejected is kept, so a human can listen: the ASR
# that flags an omission is itself fallible (T5: whisper dropped words in 9 of 10
# clips it flagged). Omissions are rare, so this is cheap.
MAX_OMISSION_CLIPS = 4  # per phase
MAX_OMISSION_CLIP_BYTES = 24_000_000  # ~8.3 min of 24 kHz s16 PCM (48,000 B/s)

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
# Parent-side only: an exception escaped ``run_gemini_phase`` itself (a bug in the
# runner). ``render_episode`` converts it, so a Gemini problem of any kind costs
# an OpenAI episode and never the episode.
REASON_RUNNER_ERROR = "runner_error"
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
        REASON_RUNNER_ERROR,
    }
)

# What a CHILD may report. ``runner_error`` is the parent's alone: a child that
# claims it is lying or broken, and its result is ``invalid_result``.
CHILD_REASONS = FALLBACK_REASONS - {REASON_RUNNER_ERROR}

# --- files ---------------------------------------------------------------------

SCHEMA = 1
RESULT_NAME = "result.json"
STARTED_NAME = "started.json"
INPUT_NAME = "input.json"
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


def omission_name(i: int, n: int) -> str:
    """Chunk ``i``'s rejected audio from TTS attempt ``n`` (raw PCM, like a chunk)."""
    return f"omission-{i:04d}-{n}.pcm"


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


def _no_diagnostics() -> dict[str, Any]:
    """The diagnostic keys of an ASR record that has nothing to say (an
    unavailable verdict, a request still in flight, or a diagnostics failure):
    same shape as a full record, so readers never branch on missing keys."""
    return {
        "script_tokens": None,
        "transcript_tokens": None,
        "matched_tokens": None,
        "max_net_missing": None,
        "spans": [],
        "transcript": None,
    }


def _span_dict(span) -> dict[str, Any]:
    return {
        "script_start": span.script_start,
        "script_end": span.script_end,
        "script_words": span.script_words,
        "transcript_words": span.transcript_words,
        "net_missing": span.net_missing,
        "flagged": span.flagged,
        "excerpt": span.excerpt,
        "heard": span.heard,
    }


def _recorded_spans(verdict) -> list[dict[str, Any]]:
    """The spans worth keeping from ``verdict``'s alignment.

    ``omission``: every flagged span, then the largest-``net_missing`` unflagged
    ones until there are ``MIN_RECORDED_OMISSION_SPANS`` (a recall-floor failure
    has no flagged span, so these are its best clues), at most
    ``MAX_RECORDED_SPANS``, flagged first and largest first. ``pass``: the single
    span with the largest ``net_missing``, the margin to trend. Anything else
    (``unavailable``): none.
    """
    if verdict.analysis is None:
        return []
    spans = verdict.analysis.spans
    by_net = sorted(spans, key=lambda s: (-s.net_missing, s.script_start))
    if verdict.status == "omission":
        flagged = [s for s in by_net if s.flagged]
        rest = [s for s in by_net if not s.flagged]
        chosen = flagged + rest[: max(0, MIN_RECORDED_OMISSION_SPANS - len(flagged))]
        return [_span_dict(s) for s in chosen[:MAX_RECORDED_SPANS]]
    if verdict.status == "pass":
        return [_span_dict(by_net[0])] if by_net else []
    return []


def _capped_transcript(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) <= MAX_RECORDED_TRANSCRIPT_CHARS:
        return text
    return text[:MAX_RECORDED_TRANSCRIPT_CHARS] + "...[truncated]"


def _diagnostics(verdict) -> dict[str, Any]:
    """Counts, spans and (omission only) the transcript. Best effort: telemetry
    never costs a chunk, so any failure here degrades to ``_no_diagnostics``."""
    try:
        a = verdict.analysis
        if a is None:
            return _no_diagnostics()
        return {
            "script_tokens": a.script_tokens,
            "transcript_tokens": a.transcript_tokens,
            "matched_tokens": a.matched_tokens,
            "max_net_missing": max((s.net_missing for s in a.spans), default=0),
            "spans": _recorded_spans(verdict),
            "transcript": (
                _capped_transcript(verdict.transcript)
                if verdict.status == "omission"
                else None
            ),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"gemini-phase: ASR diagnostics not recorded: {exc!r}", file=sys.stderr)
        return _no_diagnostics()


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
        **_diagnostics(verdict),
    }


def _trim_tokens(text: str) -> str:
    return " ".join(text.split()[:SUMMARY_TOKENS])


def _omission_summary(verdict) -> str:
    """One line naming what an omission dropped, e.g.
    ``omission (long_unmatched_span) recall 0.912, max net 24: script "..." heard
    "..."`` (the largest-``net_missing`` span; script and heard trimmed to 12 tokens).

    It becomes the ``second_omission`` failure detail, so it must never raise:
    on any problem it falls back to the plain ``omission (<reasons>)`` text.
    """
    try:
        plain = f"omission ({','.join(verdict.reasons) or 'flagged'})"
    except Exception:  # noqa: BLE001
        return "omission (flagged)"
    try:
        recall = verdict.recall
        head = f"{plain} recall {'n/a' if recall is None else f'{recall:.3f}'}"
        spans = verdict.analysis.spans if verdict.analysis is not None else ()
        if not spans:
            return head
        top = max(spans, key=lambda s: s.net_missing)
        # A recall-floor-only omission has no flagged span: say so, or "max net 3"
        # reads as a gap that was near the flag rather than scattered loss.
        unflagged = "" if any(s.flagged for s in spans) else " (no flagged span)"
        return (
            f"{head}, max net {top.net_missing}{unflagged}: "
            f'script "{_trim_tokens(top.excerpt)}" heard "{_trim_tokens(top.heard)}"'
        )
    except Exception:  # noqa: BLE001
        return plain


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
            **_no_diagnostics(),
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
        # Best effort and before the save() that records the outcome, so a name
        # in the progress file always has a whole file behind it (the write is
        # atomic). Never changes the chunk's outcome.
        name = omission_name(i, attempt["n"])
        try:
            _atomic_write(scratch / name, pcm)
            attempt["omission_audio"] = name
        except Exception as exc:  # noqa: BLE001
            print(f"gemini-phase: {name} not saved: {exc!r}", file=sys.stderr)
        save()
        omissions += 1
        last_problem = _omission_summary(verdict)
        if omissions >= 2:
            raise _ChunkFailure(REASON_SECOND_OMISSION, last_problem)


# --- the child's input -----------------------------------------------------------


class _InputError(ValueError):
    """The input file the parent wrote is missing, unreadable or malformed."""


def _write_input(scratch: Path | str, chunks: list[str], leaf: GeminiConfig) -> None:
    _atomic_write_json(
        Path(scratch) / INPUT_NAME,
        {"schema": SCHEMA, "chunks": list(chunks), "leaf": asdict(leaf)},
    )


def _read_input(scratch: Path | str) -> tuple[list[str], GeminiConfig]:
    """The chunks and Gemini leaf the parent asked for; ``_InputError`` if the
    file cannot be trusted (the child then reports ``child_error``)."""
    try:
        data = json.loads((Path(scratch) / INPUT_NAME).read_bytes())
    except (OSError, ValueError) as exc:
        raise _InputError(f"input.json unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise _InputError("input.json has an unknown shape or schema")
    chunks = data.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise _InputError("input.json has no chunk list")
    for i, chunk in enumerate(chunks):
        if not isinstance(chunk, str) or not chunk:
            raise _InputError(f"input chunk {i} is not a non-empty string")
    try:
        leaf = leaf_from_dict(data.get("leaf"))
    except ValueError as exc:
        raise _InputError(f"input leaf invalid: {exc}") from exc
    if not isinstance(leaf, GeminiConfig):
        raise _InputError(f"input leaf is {leaf.provider!r}, not gemini")
    return chunks, leaf


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
    scratch: str,
    deadline: float,
    parent_pid: int,
    factories: tuple[Callable[[], Any], Callable[[float], Any]] | None = None,
    bootstrap: Callable[[], Any] | None = None,
) -> None:
    """Spawn target. Module-level, with only small picklable arguments.

    Starts the watchdog (before the bootstrap, so a hung bootstrap is bounded
    too), runs the bootstrap, reads its input from ``input.json`` in ``scratch``
    (unusable input is a ``child_error``), then ``_run_child``, then exits with
    ``os._exit`` from a ``finally`` (so also after ``KeyboardInterrupt``/
    ``SystemExit``): exit code 0 once a result is on disk, 1 if not even a
    ``child_error`` result could be written (the parent then reports
    ``child_no_result``). Code 3 is the watchdog's. stdout/stderr are flushed
    first, since ``os._exit`` skips it.
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
            chunks, leaf = _read_input(scratch)
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
    spawn_s: float | None  # None = unknown (the runner itself failed)
    failed_chunk: int | None = None
    child_pid: int | None = None
    child_started_s: float | None = None
    # (chunk index, attempt n, PCM) for each omission attempt whose rejected audio
    # was kept: at most MAX_OMISSION_CLIPS, in (chunk, n) order, collected whether
    # or not the phase succeeded. Diagnostic only; may be empty for any reason.
    omission_audio: tuple[tuple[int, int, bytes], ...] = ()


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
        if not isinstance(reason, str) or reason not in CHILD_REASONS:
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
        attempts = parsed.get("attempts") if isinstance(parsed, dict) else None
        if isinstance(attempts, list) and all(isinstance(a, dict) for a in attempts):
            records.append(parsed)
        else:
            records.append({"index": i, "attempts": [], "progress": "unreadable"})
    return records


def _collect_omission_audio(
    scratch: Path | None, records: Any, failed_chunk: int | None = None
) -> tuple[tuple[int, int, bytes], ...]:
    """The rejected audio the child kept, from its progress ``records``.

    A clip is a candidate only if its attempt names exactly
    ``omission_name(chunk, n)`` and the file is a regular, non-empty,
    even-length file of at most ``MAX_OMISSION_CLIP_BYTES`` (checked with
    ``lstat`` before anything is read). At most ``MAX_OMISSION_CLIPS`` are
    kept: the failing chunk's first (``failed_chunk``, the chunk that ended the
    phase, whose audio is the one you most need), then the earliest of the
    rest; a candidate that then fails to read is replaced by the next. The
    result is always in (chunk, n) order. Telemetry: this never raises; any
    problem yields fewer clips.
    """
    kept: list[tuple[int, int, bytes]] = []
    try:
        if scratch is None or not isinstance(records, list):
            return ()
        candidates: list[tuple[int, int, Path]] = []
        for i, rec in enumerate(records):
            attempts = rec.get("attempts") if isinstance(rec, dict) else None
            if not isinstance(attempts, list):
                continue
            wanted = set()
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                n = attempt.get("n")
                if _is_int(n) and attempt.get("omission_audio") == omission_name(i, n):
                    wanted.add(n)
            for n in sorted(wanted):
                try:
                    path = scratch / omission_name(i, n)
                    st = path.lstat()  # lstat: a symlink is not followed
                except Exception:  # noqa: BLE001
                    continue
                if (
                    stat.S_ISREG(st.st_mode)
                    and 0 < st.st_size <= MAX_OMISSION_CLIP_BYTES
                    and not st.st_size % 2
                ):
                    candidates.append((i, n, path))
        # sorted() is stable: the failing chunk first, everything else in order.
        failing = failed_chunk if _is_int(failed_chunk) else None
        candidates.sort(key=lambda c: c[0] != failing)
        for i, n, path in candidates:
            if len(kept) >= MAX_OMISSION_CLIPS:
                break
            try:
                data = path.read_bytes()
            except Exception:  # noqa: BLE001 -- one bad clip costs only itself
                continue
            if 0 < len(data) <= MAX_OMISSION_CLIP_BYTES and not len(data) % 2:
                kept.append((i, n, data))
    except Exception:  # noqa: BLE001
        pass
    return tuple(sorted(kept, key=lambda c: (c[0], c[1])))


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
        records = _read_progress(scratch, len(chunks))
        try:  # read now: the scratch dir goes as soon as this returns
            clips = _collect_omission_audio(scratch, records, failed_chunk)
        except Exception:  # noqa: BLE001 -- diagnostics never cost the phase
            clips = ()
        return PhaseOutcome(
            ok=reason is None,
            reason=reason,
            detail=detail,
            pcm_parts=pcm,
            chunk_records=records,
            omission_audio=clips,
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
            # The episode goes to the child as a file, never through start()'s pipe.
            _write_input(scratch, chunks, leaf)
        except Exception as exc:  # noqa: BLE001
            return outcome(
                REASON_SPAWN_FAILED, f"scratch dir: {type(exc).__name__}: {exc}"
            )
        args = (str(scratch), deadline, os.getpid(), _factories, _child_bootstrap)
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


def runner_error_outcome(exc: BaseException, *, elapsed_s: float) -> PhaseOutcome:
    """The outcome for an exception that escaped ``run_gemini_phase``.

    No chunk records: nothing is known about what the phase did or cost, and the
    manifest reports that as unknown rather than zero. The detail is the
    traceback, capped like a ``child_error``'s (and logged to stderr).
    """
    return PhaseOutcome(
        ok=False,
        reason=REASON_RUNNER_ERROR,
        detail=_describe(exc),
        pcm_parts=None,
        chunk_records=[],
        elapsed_s=elapsed_s,
        spawn_s=None,
    )
