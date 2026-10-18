"""Run one podcast episode (download -> transcribe -> write) in a killable child.

The whole of ``podcast_report.run_episode_to_script`` runs in a ``spawn``ed
process under one wall-clock deadline. Both the Gemini SDK (a per-read timeout)
and opencode can trickle bytes forever, so no in-process timeout can bound the
run; only a ``kill()`` from the parent can. The consumer is a single thread, so
an unbounded episode would stall every other feed.

The parent and the child talk through files in the work dir, written atomically:

``job.json``           parent -> child: ``item_metadata`` of the episode
``child_stage.json``   child -> parent: the last stage the run reported
``child_result.json``  child -> parent: the terminal result (ok or not)

Same shape as ``pipeline/tts/gemini_phase.py`` (the idea is copied, its private
names are not imported). The parent trusts nothing the child says: an ok result
is checked against ``script.txt`` on disk.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import multiprocessing
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.podcast_feed import PodcastItem
from pipeline.podcast_sources import PodcastSource, get_podcast_source
from pipeline.podcast_transcribe import TranscriptionError
from pipeline.report_engine import persist_raw_output


logger = logging.getLogger(__name__)

# Pilot: ~4.5 min end to end for 80-85 min episodes; the writer's own timeout is
# 900 s. 25 minutes leaves room for a slow ASR day without letting a trickling
# connection hold the consumer.
CHILD_DEADLINE_S = 1500.0

JOB_NAME = "job.json"
RESULT_NAME = "child_result.json"
STAGE_NAME = "child_stage.json"
SCRIPT_NAME = "script.txt"
DEFAULT_RUNNER = "pipeline.podcast_report:run_episode_to_script"

POLL_SECONDS = 0.5  # parent: how often to look for the result
REAP_TIMEOUT_SECONDS = 5.0  # parent: join() after kill()
WATCHDOG_POLL_SECONDS = 0.5
WATCHDOG_GRACE_SECONDS = 30.0  # child: past the deadline before it exits itself

_hard_exit = os._exit


@dataclass(frozen=True)
class ChildOutcome:
    ok: bool
    stage: str  # last stage reached, or "child"
    # "" when ok; else e.g. asr_timeout, deadline, child_no_result, invalid_result,
    # spawn_failed, <ExceptionName>
    reason: str
    code: int | None
    detail: str
    result: dict[str, Any] | None
    elapsed_s: float


# --- atomic files ----------------------------------------------------------------


def _write_json(path: Path, obj: Any) -> None:
    persist_raw_output(path, json.dumps(obj, indent=2, sort_keys=True))


def _read_json_file(path: Path) -> Any | None:
    """The parsed file, or None if there is none. A file that exists but does
    not parse raises ``ValueError``: the writer is atomic, so that is not a
    half-written file but a bad one."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    return json.loads(raw)


# --- the child ---------------------------------------------------------------------


class _BadJobError(Exception):
    pass


def _load_job(source_key: str, work_dir: Path) -> tuple[PodcastSource, PodcastItem]:
    try:
        source = get_podcast_source(source_key)
    except KeyError:
        raise _BadJobError(f"unknown podcast source {source_key!r}") from None
    try:
        job = _read_json_file(work_dir / JOB_NAME)
    except (OSError, ValueError) as exc:
        raise _BadJobError(f"unreadable {JOB_NAME}: {exc!r}") from exc
    if not isinstance(job, dict):
        raise _BadJobError(f"{JOB_NAME} is missing or not an object")
    try:
        if job["source"] != source_key:
            raise _BadJobError(f"{JOB_NAME} is for source {job['source']!r}")
        duration = job["duration_s"]
        item = PodcastItem(
            guid=str(job["guid"]),
            title=str(job["title"]),
            link=str(job["link"]),
            pub_date=datetime.fromisoformat(job["pub_date"]),
            duration_s=None if duration is None else int(duration),
            enclosure_url=str(job["enclosure_url"]),
            description=str(job["description"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _BadJobError(f"invalid {JOB_NAME}: {exc!r}") from exc
    return source, item


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _child_body(
    source_key: str, work_dir: Path | str, runner: Callable[..., Any]
) -> None:
    """What the child runs: always leaves a ``child_result.json`` behind.

    Every ``Exception`` becomes a not-ok result carrying the last stage the
    runner reported. Only a failure to write the result itself propagates.
    """
    work_dir = Path(work_dir)
    last_stage = ["child"]

    def on_stage(name: str) -> None:
        last_stage[0] = name
        _write_json(work_dir / STAGE_NAME, {"stage": name})

    def failed(stage: str, reason: str, code: int | None, detail: str) -> None:
        _write_json(
            work_dir / RESULT_NAME,
            {
                "ok": False,
                "stage": stage,
                "reason": reason,
                "code": code,
                "detail": detail,
            },
        )

    try:
        source, item = _load_job(source_key, work_dir)
    except _BadJobError as exc:
        failed("job", "bad_job", None, str(exc))
        return

    try:
        res = runner(source, item, work_dir, on_stage=on_stage)
        script_path = Path(res.script_path)
        payload = {
            "ok": True,
            "script_sha256": _sha256_file(script_path),
            "piece_count": res.piece_count,
            "input_tokens": res.input_tokens,
            "output_tokens": res.output_tokens,
        }
    except TranscriptionError as exc:
        failed(last_stage[0], exc.reason, exc.code, str(exc))
        return
    except Exception as exc:  # noqa: BLE001 -- every failure is a result
        failed(last_stage[0], type(exc).__name__, None, str(exc))
        return
    _write_json(work_dir / RESULT_NAME, payload)


def _resolve_runner(spec: str) -> Callable[..., Any]:
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"runner must be 'module:attr', got {spec!r}")
    return getattr(importlib.import_module(module_name), attr)


def _configure_child_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and audioboom's are signed.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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
        name="podcast-child-watchdog",
        daemon=True,
    )
    thread.start()
    return thread


def _child_main(
    source_key: str,
    work_dir: str,
    deadline: float,
    parent_pid: int,
    runner_spec: str,
    bootstrap: Callable[[], Any] | None = None,
) -> None:
    """Spawn target. Module-level, with only small picklable arguments.

    Starts the watchdog first (so a hung bootstrap or import is bounded too),
    runs the bootstrap, configures logging, resolves the runner and runs
    ``_child_body``, then exits with ``os._exit`` from a ``finally``: code 0
    once a result is on disk, 1 if not even a failure result could be written
    (the parent then reports ``child_no_result``), 3 from the watchdog.
    """
    code = 1  # until a result is on disk
    try:
        try:
            _start_watchdog(parent_pid, deadline)
            if bootstrap is not None:
                bootstrap()
            _configure_child_logging()
            runner = _resolve_runner(runner_spec)
            _child_body(source_key, work_dir, runner)
            code = 0
        except Exception as exc:  # noqa: BLE001
            try:
                _write_json(
                    Path(work_dir) / RESULT_NAME,
                    {
                        "ok": False,
                        "stage": "child",
                        "reason": type(exc).__name__,
                        "code": None,
                        "detail": str(exc),
                    },
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


# --- the parent ----------------------------------------------------------------------


def _make_process(args: tuple) -> Any:
    """``spawn``, so the child shares nothing with us: no inherited locks,
    sockets or threads, and no re-run of a ``__main__`` ending in
    ``.__main__`` (the consumer's ``python -m pipeline``)."""
    ctx = multiprocessing.get_context("spawn")
    return ctx.Process(target=_child_main, args=args, name="podcast-child", daemon=True)


class _Reaper:
    """Kill-and-wait for one process, at most once (so a child that cannot be
    killed costs one ``REAP_TIMEOUT_SECONDS``, not two)."""

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
                    print(f"podcast-child: kill() raised {exc!r}", file=sys.stderr)
            self.proc.join(REAP_TIMEOUT_SECONDS)
        return not self.proc.is_alive()


class _InvalidResultError(Exception):
    pass


def _read_result(work_dir: Path) -> dict[str, Any] | None:
    try:
        result = _read_json_file(work_dir / RESULT_NAME)
    except (OSError, ValueError) as exc:
        raise _InvalidResultError(f"unreadable {RESULT_NAME}: {exc!r}") from exc
    if result is None:
        return None
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        raise _InvalidResultError(f"{RESULT_NAME} is not a result object")
    return result


def _read_stage(work_dir: Path) -> str | None:
    try:
        stage = _read_json_file(work_dir / STAGE_NAME)
    except (OSError, ValueError):
        return None
    if isinstance(stage, dict) and isinstance(stage.get("stage"), str):
        return stage["stage"] or None
    return None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_ok(result: dict[str, Any], work_dir: Path) -> None:
    """An ok result is only as good as the script on disk."""
    sha = result.get("script_sha256")
    if not isinstance(sha, str):
        raise _InvalidResultError("ok result has no script_sha256")
    for key in ("piece_count", "input_tokens", "output_tokens"):
        if not _is_int(result.get(key)):
            raise _InvalidResultError(f"ok result has a non-integer {key}")
    script = work_dir / SCRIPT_NAME
    try:
        data = script.read_bytes()
    except OSError as exc:
        raise _InvalidResultError(f"{SCRIPT_NAME} unreadable: {exc!r}") from exc
    if not data.strip():
        raise _InvalidResultError(f"{SCRIPT_NAME} is empty")
    if hashlib.sha256(data).hexdigest() != sha:
        raise _InvalidResultError(
            f"{SCRIPT_NAME} does not match the sha256 the child reported"
        )


def _await_child(
    reaper: _Reaper, work_dir: Path, deadline: float
) -> tuple[dict[str, Any] | None, str | None, str]:
    """Wait for a terminal result: ``(result, None, "")``, or
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
            proc.join(max(0.0, min(POLL_SECONDS, remaining)))
            result = _read_result(work_dir)
            if result is not None:
                return result, None, ""
            if not proc.is_alive():
                result = _read_result(work_dir)  # written just before it exited
                if result is not None:
                    return result, None, ""
                return (
                    None,
                    "child_no_result",
                    f"child exited (exit code {proc.exitcode}) without a result",
                )
            if remaining <= 0:
                reaper.reap()
                result = _read_result(work_dir)
                if result is not None:
                    return result, None, ""
                return None, "deadline", "the episode run exceeded its deadline"
    except _InvalidResultError as exc:
        return None, "invalid_result", str(exc)


def run_episode_in_child(
    source: PodcastSource,
    item: PodcastItem,
    work_dir: Path,
    *,
    deadline_s: float = CHILD_DEADLINE_S,
    _runner: str = DEFAULT_RUNNER,
    _bootstrap: Callable[[], Any] | None = None,
) -> ChildOutcome:
    """Run ``run_episode_to_script`` for one episode in a spawned child, within
    ``deadline_s`` seconds of wall clock.

    One absolute ``time.monotonic()`` deadline; the child honours nothing of it
    but its watchdog, and this function enforces it with ``kill()``. So it
    returns within ``deadline_s`` + one poll + ONE bounded reap. The child is
    dead on return or raise; ``KeyboardInterrupt``/``SystemExit`` kill it and
    propagate.

    ``_runner`` (``"module:attr"``) and ``_bootstrap`` are test seams: see
    ``_podcast_child_testing``.
    """
    from pipeline.podcast_report import item_metadata

    t0 = time.monotonic()
    deadline = t0 + deadline_s

    def outcome(
        ok: bool,
        reason: str = "",
        detail: str = "",
        *,
        stage: str | None = None,
        code: int | None = None,
        result: dict[str, Any] | None = None,
    ) -> ChildOutcome:
        return ChildOutcome(
            ok=ok,
            stage=stage or _read_stage(work_dir) or "child",
            reason=reason,
            code=code,
            detail=detail,
            result=result,
            elapsed_s=time.monotonic() - t0,
        )

    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / RESULT_NAME).unlink(missing_ok=True)
        (work_dir / STAGE_NAME).unlink(missing_ok=True)
        _write_json(work_dir / JOB_NAME, item_metadata(source, item))
    except OSError as exc:
        return outcome(False, type(exc).__name__, str(exc), stage="child")

    args = (
        source.key,
        str(work_dir),
        deadline,
        os.getpid(),
        _runner,
        _bootstrap,
    )
    proc: Any = None
    reaper: _Reaper | None = None
    try:
        try:
            proc = _make_process(args)
            proc.start()
            reaper = _Reaper(proc)  # only a started process can be joined
        except Exception as exc:  # noqa: BLE001
            return outcome(False, "spawn_failed", f"{type(exc).__name__}: {exc}")

        result, reason, detail = _await_child(reaper, work_dir, deadline)
        if result is None:
            return outcome(False, reason or "child_no_result", detail)
        if result["ok"]:
            try:
                _validate_ok(result, work_dir)
            except _InvalidResultError as exc:
                return outcome(False, "invalid_result", str(exc))
            return outcome(True, result=result)

        def text(key: str, default: str) -> str:
            value = result.get(key)
            return value if isinstance(value, str) and value else default

        code = result.get("code")
        return outcome(
            False,
            text("reason", "unknown"),
            str(result.get("detail") or ""),
            stage=text("stage", "child"),
            code=code if _is_int(code) else None,
        )
    finally:
        if reaper is not None and not reaper.reap():
            logger.error("podcast child pid %s could not be killed", proc.pid)
