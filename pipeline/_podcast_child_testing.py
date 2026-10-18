"""Test-only fake runners for the podcast child process.

An importable, non-test module because a spawned child cannot see the parent's
monkeypatches: the child resolves its runner by ``"module:attr"`` name and these
are the names the spawn tests pass. Every runner has ``run_episode_to_script``'s
shape ``(source, item, work_dir, *, on_stage=None, display_title=None,
feed_slug=None)`` and touches nothing but ``work_dir``.

``happy_runner``          writes ``script.txt`` and ``episode.mp3``, returns a
                          stub result
``blocking_runner``       reports ``transcribe``, leaves ``entered.pid`` in
                          ``work_dir``, then sleeps an hour
``transcription_error_runner``  raises ``TranscriptionError`` (asr_error, code 400)
``exit_runner``           ``os._exit(1)`` after reporting ``download``
``mismatch_runner``       reports a script whose bytes differ from ``script.txt``
``audio_mismatch_runner`` reports an audio sha256 that is not ``episode.mp3``'s
``no_audio_runner``       reports audio but never writes ``episode.mp3``
``empty_audio_runner``    writes a zero-byte ``episode.mp3``
``stale_audio_runner``    records whether an old ``episode.mp3`` survived to it
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import time
from dataclasses import dataclass
from pathlib import Path

from pipeline.podcast_transcribe import TranscriptionError
from pipeline.report_engine import persist_raw_output


SCRIPT_TEXT = "Welcome to the report.\n"
AUDIO_BYTES = b"\xff\xfb\x90\x00" * 64
_BLOCK_SECONDS = 3600.0


@dataclass(frozen=True)
class StubResult:
    script_path: Path
    piece_count: int = 8
    input_tokens: int = 1000
    output_tokens: int = 200
    audio_sha256: str = hashlib.sha256(AUDIO_BYTES).hexdigest()
    audio_bytes: int = len(AUDIO_BYTES)
    tts_text_sha256: str = hashlib.sha256(b"tts text").hexdigest()


def _require_child(what: str) -> None:
    if multiprocessing.parent_process() is None:
        raise RuntimeError(f"{what} must only run in a spawned child")


def _stage(on_stage, name: str) -> None:
    if on_stage is not None:
        on_stage(name)


def happy_runner(source, item, work_dir, *, on_stage=None, **_job):
    for name in ("claim", "download", "transcribe", "write"):
        _stage(on_stage, name)
    script = Path(work_dir) / "script.txt"
    persist_raw_output(script, SCRIPT_TEXT)
    (Path(work_dir) / "episode.mp3").write_bytes(AUDIO_BYTES)
    return StubResult(script_path=script)


def blocking_runner(source, item, work_dir, *, on_stage=None, **_job):
    _require_child("blocking_runner")
    _stage(on_stage, "transcribe")
    persist_raw_output(Path(work_dir) / "entered.pid", str(os.getpid()))
    time.sleep(_BLOCK_SECONDS)


def transcription_error_runner(source, item, work_dir, *, on_stage=None, **_job):
    _stage(on_stage, "transcribe")
    raise TranscriptionError("API key not valid", reason="asr_error", code=400)


def exit_runner(source, item, work_dir, *, on_stage=None, **_job):
    _require_child("exit_runner")
    _stage(on_stage, "download")
    os._exit(1)


def mismatch_runner(source, item, work_dir, *, on_stage=None, **_job):
    """Reports (via ``script_path``) a script other than the one in ``script.txt``."""
    _stage(on_stage, "write")
    persist_raw_output(Path(work_dir) / "script.txt", SCRIPT_TEXT)
    other = Path(work_dir) / "other-script.txt"
    persist_raw_output(other, "A different script.\n")
    return StubResult(script_path=other)


def audio_mismatch_runner(source, item, work_dir, *, on_stage=None, **_job):
    """Reports an audio sha256 other than the bytes in ``episode.mp3``."""
    res = happy_runner(source, item, work_dir, on_stage=on_stage)
    return StubResult(script_path=res.script_path, audio_sha256="0" * 64)


def no_audio_runner(source, item, work_dir, *, on_stage=None, **_job):
    """Reports audio it never wrote."""
    persist_raw_output(Path(work_dir) / "script.txt", SCRIPT_TEXT)
    return StubResult(script_path=Path(work_dir) / "script.txt")


def empty_audio_runner(source, item, work_dir, *, on_stage=None, **_job):
    res = happy_runner(source, item, work_dir, on_stage=on_stage)
    (Path(work_dir) / "episode.mp3").write_bytes(b"")
    return StubResult(
        script_path=res.script_path,
        audio_sha256=hashlib.sha256(b"").hexdigest(),
        audio_bytes=0,
    )


def stale_audio_runner(source, item, work_dir, *, on_stage=None, **_job):
    """Fails after recording whether a previous run's ``episode.mp3`` was still
    there when the runner started (the parent must have removed it)."""
    persist_raw_output(
        Path(work_dir) / "stale_seen.txt",
        str((Path(work_dir) / "episode.mp3").exists()),
    )
    _stage(on_stage, "render")
    raise RuntimeError("render failed")


def parent_main(work_dir: str, deadline_s: str) -> None:
    """Entry point of the helper parent in the parent-death test.

    Runs a real child whose runner blocks (leaving its pid in
    ``work_dir/entered.pid``). The test then SIGKILLs this process and checks
    that the orphaned child's watchdog ends it. Never returns in the test.
    """
    from datetime import UTC, datetime

    from pipeline import podcast_child
    from pipeline.podcast_feed import PodcastItem
    from pipeline.podcast_sources import get_podcast_source
    from pipeline.tts._phase_testing import deny_network

    item = PodcastItem(
        guid="g",
        title="t",
        link="https://x.example/p",
        pub_date=datetime(2026, 10, 15, tzinfo=UTC),
        duration_s=100,
        enclosure_url="https://x.example/a.mp3",
        description="d",
    )
    podcast_child.run_episode_in_child(
        get_podcast_source("argument"),
        item,
        Path(work_dir),
        deadline_s=float(deadline_s),
        _runner="pipeline._podcast_child_testing:blocking_runner",
        _bootstrap=deny_network,
    )
