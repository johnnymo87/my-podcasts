from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from pipeline.consumer import (
    _compute_lookback,
    consume_forever,
)


class _Done(BaseException):
    """Sentinel used to break out of consume_forever's infinite loop in tests.
    Inherits from BaseException (not Exception) so it bypasses except-Exception
    clauses."""


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

    archive_dir = tmp_path / "rundown-archive"
    monkeypatch.setattr("pipeline.consumer.RUNDOWN_SCRIPT_ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    job_id = "job-rundown-tts-test-11111"
    work_dir = tmp_path / f"the-rundown-{job_id}"
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
    # Script should have been copied to persist (redirected to tmp_path, not /persist)
    assert len(copy_calls) == 1
    assert copy_calls[0][0] == script
    assert copy_calls[0][1] == archive_dir / "2026-03-02.txt"


def test_consume_forever_dry_run_skips_tts(monkeypatch, tmp_path) -> None:
    """When THE_RUNDOWN_DRY_RUN is set, skip TTS but keep work dir for inspection."""
    monkeypatch.setenv("THE_RUNDOWN_DRY_RUN", "1")

    store = MagicMock()
    r2_client = MagicMock()

    archive_dir = tmp_path / "rundown-archive"
    monkeypatch.setattr("pipeline.consumer.RUNDOWN_SCRIPT_ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    # Use a unique job ID, create the work dir and script under tmp_path
    job_id = "job-dry-test-unique-12345"
    work_dir = tmp_path / f"the-rundown-{job_id}"
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
    # Script should have been copied to persist (redirected to tmp_path, not /persist)
    assert len(copy_calls) == 1
    assert copy_calls[0][0] == script
    assert copy_calls[0][1] == archive_dir / "2026-03-02.txt"


def test_consume_forever_cleanup_on_tts_failure(monkeypatch, tmp_path) -> None:
    """If TTS crashes, deferred cleanup still happens via finally."""
    store = MagicMock()
    r2_client = MagicMock()

    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    # Use a unique job ID, create the work dir and script under tmp_path
    job_id = "job-fail-test-unique-67890"
    work_dir = tmp_path / f"the-rundown-{job_id}"
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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    work_dir = tmp_path / f"fp-digest-{job_id}"
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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    work_dir = tmp_path / f"the-rundown-{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.rundown_writer.generate_rundown_script",
            autospec=True,
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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    work_dir = tmp_path / f"fp-digest-{job_id}"
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


def test_consume_forever_passes_reused_collection_true_on_retry(
    monkeypatch, tmp_path
) -> None:
    """Finding 1: reused_collection must reflect whether the collector
    actually reused a prior collection (collection_done.json + plan.json
    already on disk), not job.get("failure_count", 0) -- list_due_the_rundown
    never returns a failure_count key (see db.py list_due_the_rundown), so
    the old expression was permanently False.
    """
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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    # Prior collection already on disk -- this is what "reused" means.
    work_dir = tmp_path / f"the-rundown-{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    report_calls: list[dict] = []
    monkeypatch.setattr(
        "pipeline.consumer._report_run_stats",
        lambda *a, **kw: report_calls.append(kw),
    )

    from pipeline.rundown_writer import WriterOutput

    writer_output = WriterOutput(script="a script", summary="a summary")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.rundown_writer.generate_rundown_script",
            autospec=True,
            return_value=writer_output,
        ),
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    assert len(report_calls) == 1
    assert report_calls[0]["reused_collection"] is True
    store.close()


def test_consume_forever_passes_reused_collection_false_on_fresh_collection(
    monkeypatch, tmp_path
) -> None:
    """Companion to the retry case above: when no prior collection exists,
    reused_collection must be False, captured at the same branch that
    decides whether to skip collect_all_artifacts.
    """
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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    # No prior collection on disk at all -- collect_all_artifacts is mocked
    # to just write plan.json, as a real collector would after a fresh run.
    work_dir = tmp_path / f"the-rundown-{job_id}"

    def fake_collect(*args, **kwargs):
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')

    monkeypatch.setattr(
        "pipeline.things_happen_collector.collect_all_artifacts", fake_collect
    )

    report_calls: list[dict] = []
    monkeypatch.setattr(
        "pipeline.consumer._report_run_stats",
        lambda *a, **kw: report_calls.append(kw),
    )

    from pipeline.rundown_writer import WriterOutput

    writer_output = WriterOutput(script="a script", summary="a summary")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.rundown_writer.generate_rundown_script",
            autospec=True,
            return_value=writer_output,
        ),
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    assert len(report_calls) == 1
    assert report_calls[0]["reused_collection"] is False
    store.close()


# ---------------------------------------------------------------------------
# Task 2 (fp-funnel plan): FP branch emits the funnel report
# ---------------------------------------------------------------------------


def test_fp_digest_emits_run_stats_with_its_own_feed(monkeypatch, tmp_path) -> None:
    """FP's funnel row must be attributable, or run-stats.jsonl silently
    mixes two podcasts under one default feed name."""
    from pipeline.db import StateStore
    from pipeline.rundown_writer import WriterOutput

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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    # Prior collection already on disk so the writer runs immediately.
    work_dir = tmp_path / f"fp-digest-{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    report_calls: list[dict] = []
    monkeypatch.setattr(
        "pipeline.consumer._report_run_stats",
        lambda *a, **kw: report_calls.append(kw),
    )

    writer_output = WriterOutput(
        script="a script", summary="a summary", covered_headlines=["Headline 1"]
    )

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.consumer.generate_fp_script",
            return_value=writer_output,
        ),
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    assert len(report_calls) == 1
    assert report_calls[0]["feed"] == "fp-digest"
    store.close()


def test_fp_digest_reports_even_when_the_writer_covered_nothing(
    monkeypatch, tmp_path
) -> None:
    """Write this NOW, not conditionally.

    5 of the 13 real FP work dirs are writer refusals with no covered.json --
    the single highest-value class for this report. If the report call is
    ever placed inside `if writer_output.covered_headlines:`, the funnel
    vanishes on exactly the runs an operator needs it for, and every
    healthy-path test still passes.
    """
    from pipeline.db import StateStore
    from pipeline.rundown_writer import WriterOutput

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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    work_dir = tmp_path / f"fp-digest-{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    report_calls: list[dict] = []
    monkeypatch.setattr(
        "pipeline.consumer._report_run_stats",
        lambda *a, **kw: report_calls.append(kw),
    )

    # The writer refused: no covered headlines, no covered.json.
    writer_output = WriterOutput(script="a script", summary="a summary")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.consumer.generate_fp_script",
            return_value=writer_output,
        ),
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    assert not (work_dir / "covered.json").exists()
    assert len(report_calls) == 1
    assert report_calls[0]["feed"] == "fp-digest"
    store.close()


def test_fp_digest_run_stats_failure_cannot_fail_the_job(monkeypatch, tmp_path) -> None:
    """_report_run_stats is total by design; prove it for the FP path too.

    script.txt is written before _report_run_stats is ever called, so even
    if reporting itself blows up, the writer's work is not lost: the job
    is left retryable (not permanently errored) rather than the whole
    attempt vanishing.
    """
    from pipeline.db import StateStore
    from pipeline.rundown_writer import WriterOutput

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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    work_dir = tmp_path / f"fp-digest-{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    monkeypatch.setattr(
        "pipeline.consumer._report_run_stats",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    writer_output = WriterOutput(script="a script", summary="a summary")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.consumer.generate_fp_script",
            return_value=writer_output,
        ),
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    script_file = work_dir / "script.txt"
    assert script_file.exists()
    assert script_file.read_text(encoding="utf-8") == "a script"

    row = store._conn.execute(
        "SELECT status, failure_count, last_error FROM pending_fp_digest WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "pending"
    assert row["failure_count"] == 1
    assert "boom" in row["last_error"]
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


# ---------------------------------------------------------------------------
# Task 7: alert when a daily job exhausts its retry budget
# ---------------------------------------------------------------------------


def test_consume_forever_alerts_when_rundown_job_exhausts_retry_budget(
    monkeypatch, tmp_path
) -> None:
    from pipeline.db import MAX_RETRY_FAILURES, StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()
    job_id = store.insert_pending_the_rundown("2026-03-17")
    assert job_id is not None
    store._conn.execute(
        "UPDATE pending_the_rundown SET failure_count = ? WHERE id = ?",
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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    work_dir = tmp_path / f"the-rundown-{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.rundown_writer.generate_rundown_script",
            autospec=True,
            side_effect=RuntimeError("upstream timeout"),
        ),
        patch("pipeline.alerts.send_alert") as alert,
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    row = store._conn.execute(
        "SELECT status FROM pending_the_rundown WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["status"] == "errored"
    assert alert.called
    alert_text = alert.call_args[0][0]
    assert "The Rundown" in alert_text
    assert "2026-03-17" in alert_text
    store.close()


def test_consume_forever_does_not_alert_on_an_ordinary_rundown_retry(
    monkeypatch, tmp_path
) -> None:
    """An alert on every retry (not just exhaustion) would page for nothing;
    a single ordinary failure must stay silent."""
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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    work_dir = tmp_path / f"the-rundown-{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.rundown_writer.generate_rundown_script",
            autospec=True,
            side_effect=RuntimeError("upstream timeout"),
        ),
        patch("pipeline.alerts.send_alert") as alert,
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    row = store._conn.execute(
        "SELECT status, failure_count FROM pending_the_rundown WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["status"] == "pending"
    assert row["failure_count"] == 1
    assert not alert.called
    store.close()


def test_consume_forever_alerts_when_fp_job_exhausts_retry_budget(
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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    work_dir = tmp_path / f"fp-digest-{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.consumer.generate_fp_script",
            side_effect=RuntimeError("FP writer returned empty script"),
        ),
        patch("pipeline.alerts.send_alert") as alert,
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    row = store._conn.execute(
        "SELECT status FROM pending_fp_digest WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["status"] == "errored"
    assert alert.called
    alert_text = alert.call_args[0][0]
    assert "FP Digest" in alert_text
    assert "2026-03-17" in alert_text
    store.close()


def test_consume_forever_does_not_alert_on_an_ordinary_fp_retry(
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
    monkeypatch.setattr("pipeline.consumer._work_dir_base", lambda: tmp_path)

    work_dir = tmp_path / f"fp-digest-{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "plan.json").write_text('{"themes": ["A"], "directives": []}')
    (work_dir / "collection_done.json").write_text("{}")

    with (
        patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer),
        patch(
            "pipeline.consumer.generate_fp_script",
            side_effect=RuntimeError("FP writer returned empty script"),
        ),
        patch("pipeline.alerts.send_alert") as alert,
    ):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    row = store._conn.execute(
        "SELECT status, failure_count FROM pending_fp_digest WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["status"] == "pending"
    assert row["failure_count"] == 1
    assert not alert.called
    store.close()
