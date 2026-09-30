"""``run_gemini_phase`` against a real spawned child.

Each test runs the real parent runner, a real ``spawn`` process, the real child
worker, and ``_phase_testing`` fakes in place of Gemini. The child's bootstrap is
``deny_network`` (a conftest monkeypatch does not survive ``spawn``), so even a
bug that bypassed the fakes could not reach a real API.

Every wait here is bounded by the runner's own budget or by an explicit timeout,
so a regression fails in seconds instead of hanging the suite.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pipeline.tts import gemini_phase as gp
from pipeline.tts._phase_testing import (
    deny_network,
    factories,
    fake_pcm,
    hang_bootstrap,
    make_chunks,
    pid_gone,
    wait_for_pid,
)
from pipeline.tts.config import GeminiConfig


LEAF = GeminiConfig(model="fake-model", voice="Kore")
# A child needs ~1-2 s to start and import; the blocked-child budgets must leave
# room for it to actually reach the blocking call, or the test proves nothing.
BLOCKED_BUDGET_S = 4.0
SLACK_S = 2.0
PID_WAIT_S = 60.0


class Run:
    def __init__(self, tmp_path: Path) -> None:
        self.roots = tmp_path / "roots"
        self.markers = tmp_path / "markers"
        self.markers.mkdir()
        self.wall = 0.0

    def __call__(
        self, behavior, *, chunk=None, n=2, budget_s=BLOCKED_BUDGET_S, bootstrap=None
    ):
        started = time.monotonic()
        try:
            return gp.run_gemini_phase(
                make_chunks(n),
                LEAF,
                budget_s=budget_s,
                scratch_root=self.roots,
                _factories=factories(behavior, chunk=chunk, marker_dir=self.markers),
                _child_bootstrap=bootstrap or deny_network,
            )
        finally:
            self.wall = time.monotonic() - started

    def assert_clean(self) -> None:
        """No scratch dir is left behind."""
        assert list(self.roots.iterdir()) == []


@pytest.fixture
def run(tmp_path) -> Run:
    return Run(tmp_path)


def assert_dead(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # reaped by the runner, not merely a zombie
    assert pid_gone(pid)


def test_happy_path_returns_every_chunk_in_order(run):
    out = run("ok", n=3, budget_s=60.0)
    assert out.ok, out.detail
    assert out.reason is None
    assert out.pcm_parts == [fake_pcm(i) for i in range(3)]
    assert [r["index"] for r in out.chunk_records] == [0, 1, 2]
    for rec in out.chunk_records:
        (attempt,) = rec["attempts"]
        assert attempt["outcome"] == "verified"
        assert attempt["synth"]["audio_tokens"] == 100
        assert attempt["asr"]["output_tokens"] == 30
    # The spawn is measured, and the child's own clock agrees it started.
    assert 0 <= out.spawn_s < 5.0
    assert out.child_started_s is not None and 0 <= out.child_started_s < 15.0
    assert_dead(out.child_pid)
    run.assert_clean()


def test_synth_blocked_forever_hits_the_deadline_and_the_child_is_dead(run):
    out = run("block_synth_forever", chunk=0)
    assert (out.ok, out.reason) == (False, gp.REASON_DEADLINE)
    assert out.pcm_parts is None
    assert run.wall <= BLOCKED_BUDGET_S + gp.REAP_TIMEOUT_SECONDS + SLACK_S
    pid = wait_for_pid(run.markers / "entered-synth-0", 5.0)  # it really blocked
    assert pid == out.child_pid
    assert_dead(pid)
    run.assert_clean()


def test_asr_blocked_forever_hits_the_deadline_and_the_child_is_dead(run):
    out = run("block_asr_forever", chunk=0)
    assert (out.ok, out.reason) == (False, gp.REASON_DEADLINE)
    assert run.wall <= BLOCKED_BUDGET_S + gp.REAP_TIMEOUT_SECONDS + SLACK_S
    pid = wait_for_pid(run.markers / "entered-asr-0", 5.0)
    assert pid == out.child_pid
    # The synth of the verified-in-flight attempt was recorded before the kill.
    (attempt,) = out.chunk_records[0]["attempts"]
    assert attempt["synth"]["status"] == "ok"
    assert attempt["asr"]["status"] == "started"
    assert attempt["asr"]["input_tokens"] is None  # unknown, not zero
    assert_dead(pid)
    run.assert_clean()


def test_early_fatal_returns_at_once_while_a_sibling_is_blocked(run):
    budget = 40.0
    # Chunk 1 is stuck in synth (its marker exists before chunk 0 fails fatally);
    # the phase must end on the fatal, not wait for chunk 1 or the budget.
    out = run("fatal_and_block_others", chunk=0, n=2, budget_s=budget)
    assert (out.ok, out.reason, out.failed_chunk) == (False, gp.REASON_FATAL, 0)
    assert run.wall < budget / 2
    blocked = wait_for_pid(run.markers / "entered-synth-1", 5.0)
    assert blocked == out.child_pid  # the stuck sibling lived in the child we killed
    assert_dead(blocked)
    run.assert_clean()


def test_child_that_exits_at_once_is_child_no_result(run):
    out = run("exit_on_build", budget_s=30.0)
    assert (out.ok, out.reason) == (False, gp.REASON_CHILD_NO_RESULT)
    assert "exit code 1" in out.detail
    assert run.wall < 15.0  # did not wait out the budget
    assert_dead(out.child_pid)
    run.assert_clean()


def test_a_hung_child_that_never_answers_is_killed_by_the_parent(run):
    # The bootstrap never returns, so the child cannot report its own deadline:
    # only the parent's kill() (and the child's watchdog, a second later) can end it.
    out = run("ok", budget_s=3.0, bootstrap=hang_bootstrap)
    assert (out.ok, out.reason) == (False, gp.REASON_DEADLINE)
    assert run.wall <= 3.0 + gp.REAP_TIMEOUT_SECONDS + SLACK_S
    assert_dead(out.child_pid)
    run.assert_clean()


def test_keyboard_interrupt_kills_the_child_and_propagates(run, monkeypatch):
    seen: dict[str, int] = {}

    def interrupt(proc, timeout):
        seen["pid"] = wait_for_pid(run.markers / "entered-synth-0", PID_WAIT_S)
        raise KeyboardInterrupt

    monkeypatch.setattr(gp, "_join_child", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run("block_synth_forever", chunk=0, budget_s=300.0)
    assert_dead(seen["pid"])
    run.assert_clean()


HELPER = (
    "import sys; from pipeline.tts._phase_testing import parent_main; "
    "parent_main(*sys.argv[1:])"
)


def test_child_exits_on_its_own_when_the_parent_is_sigkilled(tmp_path):
    markers = tmp_path / "markers"
    markers.mkdir()
    err = (tmp_path / "helper.err").open("wb")
    helper = subprocess.Popen(
        [sys.executable, "-c", HELPER, str(markers), str(tmp_path / "roots"), "300"],
        stdout=subprocess.DEVNULL,
        stderr=err,
    )
    pid = None
    try:
        pid = wait_for_pid(markers / "entered-synth-0", PID_WAIT_S)
        assert not pid_gone(pid) and pid != helper.pid
        helper.send_signal(signal.SIGKILL)
        helper.wait(timeout=10)
        killed_at = time.monotonic()
        while not pid_gone(pid):
            assert time.monotonic() - killed_at < 10.0, "orphaned child still alive"
            time.sleep(0.05)
        # The watchdog polls every 0.25 s, so "about a second", with host slack.
        assert time.monotonic() - killed_at < 3.0
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.wait(timeout=10)
        err.close()
        if pid is not None and not pid_gone(pid):
            os.kill(pid, signal.SIGKILL)
