from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.consumer import (
    _AGENT_SESSION_TIMEOUT_SECONDS,
    _compute_lookback,
    consume_forever,
)


class _Done(BaseException):
    """Sentinel used to break out of consume_forever's infinite loop in tests.
    Inherits from BaseException (not Exception) so it bypasses except-Exception clauses."""


def test_consume_forever_retries_on_pull_exception(monkeypatch) -> None:
    """Verify that a transient error from consumer.pull() triggers sleep-and-retry
    instead of crashing consume_forever()."""
    store = MagicMock()
    store.list_due_the_rundown.return_value = []
    r2_client = MagicMock()

    call_count = 0

    def flaky_pull(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a 502 / transient API error on first call
            raise Exception("502 Bad Gateway")
        # Second call: exit the infinite loop via a BaseException sentinel
        raise _Done("done")

    mock_consumer = MagicMock()
    mock_consumer.pull.side_effect = flaky_pull

    slept = []
    monkeypatch.setattr(time, "sleep", lambda n: slept.append(n))

    with patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    # Should have made 2 pull calls (first failed, second succeeded and raised _Done)
    assert call_count == 2, f"Expected 2 pull calls, got {call_count}"
    # Should have slept once after the first failure
    assert 5 in slept, f"Expected sleep(5) after failure, got slept={slept}"


def test_consume_forever_processes_rundown_script(monkeypatch, tmp_path) -> None:
    """When a due Rundown job exists with a script file, consumer runs TTS + publish."""
    store = MagicMock()
    r2_client = MagicMock()

    job_id = "job-rundown-tts-test-11111"
    work_dir = Path(f"/tmp/the-rundown-{job_id}")
    work_dir.mkdir(parents=True, exist_ok=True)
    script = work_dir / "script.txt"
    script.write_text("rundown script content")

    process_calls = []
    monkeypatch.setattr(
        "pipeline.consumer.process_things_happen_job",
        lambda *a, **kw: process_calls.append(1),
    )

    stopped = []
    monkeypatch.setattr(
        "pipeline.consumer.stop_agent", lambda session_id: stopped.append(session_id)
    )

    store.list_due_the_rundown.return_value = [{"id": job_id, "date_str": "2026-03-02"}]
    store.get_the_rundown_session_id.return_value = None

    call_count = 0

    def flaky_pull(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []
        raise _Done("done")

    mock_consumer = MagicMock()
    mock_consumer.pull.side_effect = flaky_pull

    monkeypatch.setattr(time, "sleep", lambda n: None)
    monkeypatch.delenv("THE_RUNDOWN_DRY_RUN", raising=False)

    copy_calls: list[tuple] = []
    cleanup_calls: list[int] = []

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch("shutil.copy", lambda src, dst: copy_calls.append((src, dst))),
        patch(
            "pipeline.consumer._cleanup_old_work_dirs",
            lambda **kw: cleanup_calls.append(1),
        ),
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    # TTS should have been called
    assert process_calls == [1]
    # stop_agent should have been called
    assert stopped == [None]
    # Script should have been copied to persist
    assert len(copy_calls) == 1
    assert copy_calls[0][0] == script


def test_consume_forever_dry_run_skips_tts(monkeypatch, tmp_path) -> None:
    """When THE_RUNDOWN_DRY_RUN is set, skip TTS but keep work dir for inspection."""
    monkeypatch.setenv("THE_RUNDOWN_DRY_RUN", "1")

    store = MagicMock()
    r2_client = MagicMock()

    # Use a unique job ID, create the work dir and script at /tmp/the-rundown-<id>
    job_id = "job-dry-test-unique-12345"
    work_dir = Path(f"/tmp/the-rundown-{job_id}")
    work_dir.mkdir(parents=True, exist_ok=True)
    script = work_dir / "script.txt"
    script.write_text("test script content")

    stopped = []
    monkeypatch.setattr(
        "pipeline.consumer.stop_agent", lambda session_id: stopped.append(1)
    )

    process_calls = []
    monkeypatch.setattr(
        "pipeline.consumer.process_things_happen_job",
        lambda *a, **kw: process_calls.append(1),
    )

    store.list_due_the_rundown.return_value = [{"id": job_id, "date_str": "2026-03-02"}]
    store.get_the_rundown_session_id.return_value = None

    call_count = 0

    def flaky_pull(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []
        raise _Done("done")

    mock_consumer = MagicMock()
    mock_consumer.pull.side_effect = flaky_pull
    monkeypatch.setattr(time, "sleep", lambda n: None)

    copy_calls: list[tuple] = []
    cleanup_calls: list[int] = []

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch("shutil.copy", lambda src, dst: copy_calls.append((src, dst))),
        patch(
            "pipeline.consumer._cleanup_old_work_dirs",
            lambda **kw: cleanup_calls.append(1),
        ),
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    # TTS should NOT have been called
    assert process_calls == []
    # Job should be marked completed so it doesn't re-launch
    store.mark_the_rundown_completed.assert_called_once_with(job_id)
    # stop_agent should be called
    assert stopped == [1]
    # Work dir should still exist (not immediately deleted)
    assert work_dir.exists()
    # Deferred cleanup should have been invoked
    assert cleanup_calls == [1]
    # Script should have been copied to persist
    assert len(copy_calls) == 1
    assert copy_calls[0][0] == script


def test_consume_forever_stops_agent_on_tts_failure(monkeypatch, tmp_path) -> None:
    """If TTS crashes, stop_agent() and deferred cleanup still happen via finally."""
    store = MagicMock()
    r2_client = MagicMock()

    # Use a unique job ID, create the work dir and script at /tmp/the-rundown-<id>
    job_id = "job-fail-test-unique-67890"
    work_dir = Path(f"/tmp/the-rundown-{job_id}")
    work_dir.mkdir(parents=True, exist_ok=True)
    script = work_dir / "script.txt"
    script.write_text("test script content")

    stopped = []
    monkeypatch.setattr(
        "pipeline.consumer.stop_agent", lambda session_id: stopped.append(1)
    )

    def boom(*a, **kw):
        raise RuntimeError("TTS exploded")

    monkeypatch.setattr("pipeline.consumer.process_things_happen_job", boom)

    store.list_due_the_rundown.return_value = [{"id": job_id, "date_str": "2026-03-02"}]
    store.get_the_rundown_session_id.return_value = None

    call_count = 0

    def flaky_pull(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []
        raise _Done("done")

    mock_consumer = MagicMock()
    mock_consumer.pull.side_effect = flaky_pull
    monkeypatch.setattr(time, "sleep", lambda n: None)
    monkeypatch.delenv("THE_RUNDOWN_DRY_RUN", raising=False)

    cleanup_calls: list[int] = []

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.consumer._cleanup_old_work_dirs",
            lambda **kw: cleanup_calls.append(1),
        ),
        patch("shutil.copy", lambda src, dst: None),
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    # Agent should be stopped even though TTS failed
    assert stopped == [1]
    # Work dir should still exist (not immediately deleted)
    assert work_dir.exists()
    # Deferred cleanup should have been invoked
    assert cleanup_calls == [1]


def test_consume_forever_kills_stuck_agent(monkeypatch, tmp_path) -> None:
    """If a Rundown agent session exceeds the timeout, it should be killed."""
    store = MagicMock()
    r2_client = MagicMock()

    store.list_due_the_rundown.return_value = [
        {"id": "job-stuck", "date_str": "2026-03-09"}
    ]
    store.get_the_rundown_session_id.return_value = "ses_stuck123"

    monkeypatch.setattr("pipeline.consumer.is_agent_running", lambda session_id: True)

    stopped = []
    monkeypatch.setattr(
        "pipeline.consumer.stop_agent", lambda session_id: stopped.append(session_id)
    )

    # Simulate time: first call (loop 1, line 319) stores the start time,
    # second call (loop 2, line 320) returns a value past the timeout.
    time_values = iter([100.0, 100.0 + _AGENT_SESSION_TIMEOUT_SECONDS + 1])
    monkeypatch.setattr(time, "time", lambda: next(time_values))

    call_count = 0

    def flaky_pull(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return []  # First two loops: no messages, process Rundown jobs
        raise _Done("done")

    mock_consumer = MagicMock()
    mock_consumer.pull.side_effect = flaky_pull
    monkeypatch.setattr(time, "sleep", lambda n: None)

    with patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    # First loop: no tracked start time → starts tracking (not killed)
    # Second loop: tracked time exceeded timeout → killed
    assert "ses_stuck123" in stopped
    store.clear_the_rundown_session_id.assert_called_with("job-stuck")


def test_compute_lookback_none_returns_default():
    store = MagicMock()
    store.days_since_last_episode.return_value = None
    assert _compute_lookback(store, "the-rundown") == 2


def test_compute_lookback_floors_at_2():
    store = MagicMock()
    store.days_since_last_episode.return_value = 0
    assert _compute_lookback(store, "the-rundown") == 2


def test_compute_lookback_caps_at_14():
    store = MagicMock()
    store.days_since_last_episode.return_value = 20
    assert _compute_lookback(store, "the-rundown") == 14


def test_compute_lookback_mid_range():
    store = MagicMock()
    store.days_since_last_episode.return_value = 5
    assert _compute_lookback(store, "the-rundown") == 6
