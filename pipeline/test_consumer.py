from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.consumer import (
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

    store.list_due_the_rundown.return_value = [{"id": job_id, "date_str": "2026-03-02"}]

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

    process_calls = []
    monkeypatch.setattr(
        "pipeline.consumer.process_things_happen_job",
        lambda *a, **kw: process_calls.append(1),
    )

    store.list_due_the_rundown.return_value = [{"id": job_id, "date_str": "2026-03-02"}]

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
    # Work dir should still exist (not immediately deleted)
    assert work_dir.exists()
    # Deferred cleanup should have been invoked
    assert cleanup_calls == [1]
    # Script should have been copied to persist
    assert len(copy_calls) == 1
    assert copy_calls[0][0] == script


def test_consume_forever_cleanup_on_tts_failure(monkeypatch, tmp_path) -> None:
    """If TTS crashes, deferred cleanup still happens via finally."""
    store = MagicMock()
    r2_client = MagicMock()

    # Use a unique job ID, create the work dir and script at /tmp/the-rundown-<id>
    job_id = "job-fail-test-unique-67890"
    work_dir = Path(f"/tmp/the-rundown-{job_id}")
    work_dir.mkdir(parents=True, exist_ok=True)
    script = work_dir / "script.txt"
    script.write_text("test script content")

    def boom(*a, **kw):
        raise RuntimeError("TTS exploded")

    monkeypatch.setattr("pipeline.consumer.process_things_happen_job", boom)

    store.list_due_the_rundown.return_value = [{"id": job_id, "date_str": "2026-03-02"}]

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

    # Work dir should still exist (not immediately deleted)
    assert work_dir.exists()
    # Deferred cleanup should have been invoked
    assert cleanup_calls == [1]


def test_consume_forever_schedules_fp_retry_after_writer_failure(
    monkeypatch, tmp_path
) -> None:
    from pipeline.db import StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()
    job_id = store.insert_pending_fp_digest("2026-03-17")
    assert job_id is not None

    call_count = 0

    def fake_pull(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []
        raise _Done("done")

    mock_consumer = MagicMock()
    mock_consumer.pull.side_effect = fake_pull

    monkeypatch.setattr(time, "sleep", lambda n: None)
    monkeypatch.setattr(
        "pipeline.consumer.collect_fp_artifacts",
        lambda *a, **kw: None,
        raising=False,
    )

    work_dir = Path(f"/tmp/fp-digest-{job_id}")
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.consumer.generate_fp_script",
            side_effect=RuntimeError("FP writer returned empty script"),
        ),
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    assert store.list_due_fp_digest() == []
    row = store._conn.execute(
        "SELECT failure_count, last_error FROM pending_fp_digest WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row["failure_count"] == 1
    assert "empty script" in row["last_error"]
    store.close()


def test_consume_forever_schedules_rundown_retry_after_writer_failure(
    monkeypatch, tmp_path
) -> None:
    from pipeline.db import StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()
    job_id = store.insert_pending_the_rundown("2026-03-17")
    assert job_id is not None

    call_count = 0

    def fake_pull(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []
        raise _Done("done")

    mock_consumer = MagicMock()
    mock_consumer.pull.side_effect = fake_pull
    monkeypatch.setattr(time, "sleep", lambda n: None)

    work_dir = Path(f"/tmp/the-rundown-{job_id}")
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.rundown_writer.generate_rundown_script",
            side_effect=RuntimeError("upstream timeout"),
        ),
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    assert store.list_due_the_rundown() == []
    row = store._conn.execute(
        "SELECT failure_count, last_error FROM pending_the_rundown WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row["failure_count"] == 1
    assert row["last_error"] == "upstream timeout"
    store.close()


def test_consume_forever_marks_fp_job_errored_after_retry_budget(
    monkeypatch, tmp_path
) -> None:
    from pipeline.db import MAX_RETRY_FAILURES, StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()
    job_id = store.insert_pending_fp_digest("2026-03-17")
    assert job_id is not None
    store._conn.execute(
        "UPDATE pending_fp_digest SET failure_count = ? WHERE id = ?",
        (MAX_RETRY_FAILURES - 1, job_id),
    )
    store._conn.commit()

    call_count = 0

    def fake_pull(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []
        raise _Done("done")

    mock_consumer = MagicMock()
    mock_consumer.pull.side_effect = fake_pull
    monkeypatch.setattr(time, "sleep", lambda n: None)

    work_dir = Path(f"/tmp/fp-digest-{job_id}")
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.consumer.generate_fp_script",
            side_effect=RuntimeError("FP writer returned empty script"),
        ),
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    row = store._conn.execute(
        "SELECT status, failure_count FROM pending_fp_digest WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "errored"
    assert row["failure_count"] == MAX_RETRY_FAILURES
    assert store.list_due_fp_digest() == []
    store.close()


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
