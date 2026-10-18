"""``run_episode_in_child`` against a real spawned child.

Real parent, real ``spawn`` process, real child entry point; the runners are the
fakes in ``_podcast_child_testing`` (named by ``"module:attr"``). The child's
bootstrap is ``deny_network``: a conftest monkeypatch does not survive ``spawn``,
so even a bug that bypassed the fakes could not reach a real API.

Every wait is bounded by the deadline passed to the parent, so a regression
fails in seconds instead of hanging the suite.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from pipeline import podcast_child as pc
from pipeline._podcast_child_testing import SCRIPT_TEXT
from pipeline.podcast_feed import parse_podcast_feed
from pipeline.podcast_sources import get_podcast_source
from pipeline.tts._phase_testing import deny_network, pid_gone, wait_for_pid


FIXTURE = Path(__file__).parent / "fixtures" / "argument_feed.xml"
GUID = "audioboom-01a13c3d-ae49-7326-a550-f82c9eff9be1"
TESTING = "pipeline._podcast_child_testing"
SLACK_S = 2.0
GENEROUS_S = 120.0


@pytest.fixture(scope="module")
def source():
    return get_podcast_source("argument")


@pytest.fixture(scope="module")
def item():
    return next(
        i
        for i in parse_podcast_feed(FIXTURE.read_text(encoding="utf-8"))
        if i.guid == GUID
    )


def _run(source, item, work_dir, runner, **kw):
    kw.setdefault("deadline_s", GENEROUS_S)
    return pc.run_episode_in_child(
        source,
        item,
        work_dir,
        _runner=f"{TESTING}:{runner}",
        _bootstrap=deny_network,
        **kw,
    )


@pytest.fixture(scope="module")
def blocked_deadline_s(tmp_path_factory, source, item) -> float:
    """A deadline a blocked-child test can trust to outlast the child's startup.

    The clock starts before ``Process.start()``, and the child must import this
    package, run its bootstrap and reach the blocking call. A test whose
    deadline ends first proves nothing: it would see ``deadline`` without the
    child ever blocking. So measure this host's startup once (a full happy run
    is a safe upper bound) and scale to it.
    """
    out = _run(source, item, tmp_path_factory.mktemp("startup"), "happy_runner")
    startup = out.elapsed_s if out.ok else 10.0
    return max(4.0, 3 * startup + 2.0)


def assert_dead(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # reaped by the parent, not merely a zombie
    assert pid_gone(pid)


def test_happy_runner_is_ok_and_validated(tmp_path, source, item):
    work = tmp_path / "w"
    out = _run(source, item, work, "happy_runner")

    assert out.ok, out
    assert (out.reason, out.code, out.detail) == ("", None, "")
    assert out.stage == "write"
    assert out.result == {
        "ok": True,
        "script_sha256": hashlib.sha256(SCRIPT_TEXT.encode()).hexdigest(),
        "piece_count": 8,
        "input_tokens": 1000,
        "output_tokens": 200,
    }
    assert (work / "script.txt").read_text() == SCRIPT_TEXT
    assert out.elapsed_s < GENEROUS_S


def test_blocked_runner_is_killed_at_the_deadline(
    tmp_path, source, item, blocked_deadline_s
):
    work = tmp_path / "w"
    started = time.monotonic()
    out = _run(source, item, work, "blocking_runner", deadline_s=blocked_deadline_s)
    wall = time.monotonic() - started

    assert (out.ok, out.reason, out.stage) == (False, "deadline", "transcribe")
    assert out.result is None
    bound = pc.REAP_TIMEOUT_SECONDS + pc.POLL_SECONDS + SLACK_S
    assert wall < blocked_deadline_s + bound
    pid = wait_for_pid(work / "entered.pid", 5.0)  # it really blocked
    assert_dead(pid)


def test_transcription_error_reason_and_code_are_carried(tmp_path, source, item):
    out = _run(source, item, tmp_path / "w", "transcription_error_runner")
    assert (out.ok, out.stage, out.reason, out.code) == (
        False,
        "transcribe",
        "asr_error",
        400,
    )
    assert "API key not valid" in out.detail


def test_runner_that_exits_without_a_result_is_child_no_result(tmp_path, source, item):
    out = _run(source, item, tmp_path / "w", "exit_runner")
    assert (out.ok, out.reason, out.stage) == (False, "child_no_result", "download")
    assert "exit code 1" in out.detail


def test_script_that_differs_from_the_reported_sha_is_invalid(tmp_path, source, item):
    out = _run(source, item, tmp_path / "w", "mismatch_runner")
    assert (out.ok, out.reason) == (False, "invalid_result")
    assert "sha256" in out.detail


def test_unresolvable_runner_is_a_child_failure_not_a_hang(tmp_path, source, item):
    out = pc.run_episode_in_child(
        source,
        item,
        tmp_path / "w",
        deadline_s=GENEROUS_S,
        _runner=f"{TESTING}:does_not_exist",
        _bootstrap=deny_network,
    )
    assert (out.ok, out.stage, out.reason) == (False, "child", "AttributeError")


def test_unpicklable_bootstrap_is_spawn_failed(tmp_path, source, item):
    out = pc.run_episode_in_child(
        source,
        item,
        tmp_path / "w",
        deadline_s=GENEROUS_S,
        _runner=f"{TESTING}:happy_runner",
        _bootstrap=lambda: None,
    )
    assert (out.ok, out.reason, out.stage) == (False, "spawn_failed", "child")


def test_a_rerun_in_the_same_work_dir_ignores_the_previous_result(
    tmp_path, source, item, blocked_deadline_s
):
    work = tmp_path / "w"
    assert _run(source, item, work, "happy_runner").ok
    # The previous run's ok result and script are on disk; a blocked rerun must
    # not mistake them for its own.
    out = _run(source, item, work, "blocking_runner", deadline_s=blocked_deadline_s)
    assert (out.ok, out.reason) == (False, "deadline")
