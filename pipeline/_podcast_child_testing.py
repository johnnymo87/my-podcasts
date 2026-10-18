"""Test-only fake runners for the podcast child process.

An importable, non-test module because a spawned child cannot see the parent's
monkeypatches: the child resolves its runner by ``"module:attr"`` name and these
are the names the spawn tests pass. Every runner has ``run_episode_to_script``'s
shape ``(source, item, work_dir, *, on_stage=None)`` and touches nothing but
``work_dir``.

``happy_runner``          writes ``script.txt`` and returns a stub result
``blocking_runner``       reports ``transcribe``, leaves ``entered.pid`` in
                          ``work_dir``, then sleeps an hour
``transcription_error_runner``  raises ``TranscriptionError`` (asr_error, code 400)
``exit_runner``           ``os._exit(1)`` after reporting ``download``
``mismatch_runner``       reports a script whose bytes differ from ``script.txt``
"""

from __future__ import annotations

import multiprocessing
import os
import time
from dataclasses import dataclass
from pathlib import Path

from pipeline.podcast_transcribe import TranscriptionError
from pipeline.report_engine import persist_raw_output


SCRIPT_TEXT = "Welcome to the report.\n"
_BLOCK_SECONDS = 3600.0


@dataclass(frozen=True)
class StubResult:
    script_path: Path
    piece_count: int = 8
    input_tokens: int = 1000
    output_tokens: int = 200


def _require_child(what: str) -> None:
    if multiprocessing.parent_process() is None:
        raise RuntimeError(f"{what} must only run in a spawned child")


def _stage(on_stage, name: str) -> None:
    if on_stage is not None:
        on_stage(name)


def happy_runner(source, item, work_dir, *, on_stage=None):
    for name in ("claim", "download", "transcribe", "write"):
        _stage(on_stage, name)
    script = Path(work_dir) / "script.txt"
    persist_raw_output(script, SCRIPT_TEXT)
    return StubResult(script_path=script)


def blocking_runner(source, item, work_dir, *, on_stage=None):
    _require_child("blocking_runner")
    _stage(on_stage, "transcribe")
    persist_raw_output(Path(work_dir) / "entered.pid", str(os.getpid()))
    time.sleep(_BLOCK_SECONDS)


def transcription_error_runner(source, item, work_dir, *, on_stage=None):
    _stage(on_stage, "transcribe")
    raise TranscriptionError("API key not valid", reason="asr_error", code=400)


def exit_runner(source, item, work_dir, *, on_stage=None):
    _require_child("exit_runner")
    _stage(on_stage, "download")
    os._exit(1)


def mismatch_runner(source, item, work_dir, *, on_stage=None):
    """Reports (via ``script_path``) a script other than the one in ``script.txt``."""
    _stage(on_stage, "write")
    persist_raw_output(Path(work_dir) / "script.txt", SCRIPT_TEXT)
    other = Path(work_dir) / "other-script.txt"
    persist_raw_output(other, "A different script.\n")
    return StubResult(script_path=other)
