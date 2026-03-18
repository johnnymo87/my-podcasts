from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pipeline.db import MAX_RETRY_FAILURES, StateStore


def test_insert_and_list_pending_things_happen(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    links_json = json.dumps(
        [
            {
                "link_text": "Test",
                "raw_url": "http://example.com",
                "headline_context": "Test story",
            }
        ]
    )
    store.insert_pending_things_happen(
        email_r2_key="inbox/raw/abc.eml",
        date_str="2026-02-26",
        links_json=links_json,
    )
    # Default delay is 0, so the job should be immediately due.
    due = store.list_due_things_happen()
    assert len(due) == 1
    store.close()


def test_due_jobs_returned_after_delay(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    links_json = json.dumps(
        [
            {
                "link_text": "Test",
                "raw_url": "http://example.com",
                "headline_context": "Test story",
            }
        ]
    )
    # Insert with process_after in the past.
    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_things_happen
           (id, email_r2_key, date_str, links_json, process_after, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("test-id", "inbox/raw/abc.eml", "2026-02-26", links_json, past, "pending"),
    )
    store._conn.commit()
    due = store.list_due_things_happen()
    assert len(due) == 1
    assert due[0]["id"] == "test-id"
    assert due[0]["date_str"] == "2026-02-26"
    store.close()


def test_mark_things_happen_completed(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_things_happen
           (id, email_r2_key, date_str, links_json, process_after, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("test-id", "inbox/raw/abc.eml", "2026-02-26", "[]", past, "pending"),
    )
    store._conn.commit()
    store.mark_things_happen_completed("test-id")
    due = store.list_due_things_happen()
    assert len(due) == 0
    store.close()


def test_insert_and_list_due_the_rundown(tmp_path):
    store = StateStore(tmp_path / "test.db")
    job_id = store.insert_pending_the_rundown("2026-03-09")
    assert job_id is not None
    due = store.list_due_the_rundown()
    assert len(due) == 1
    assert due[0]["date_str"] == "2026-03-09"
    store.close()


def test_no_duplicate_the_rundown(tmp_path):
    store = StateStore(tmp_path / "test.db")
    store.insert_pending_the_rundown("2026-03-09")
    assert store.insert_pending_the_rundown("2026-03-09") is None
    store.close()


def test_mark_the_rundown_completed(tmp_path):
    store = StateStore(tmp_path / "test.db")
    job_id = store.insert_pending_the_rundown("2026-03-09")
    store.mark_the_rundown_completed(job_id)
    assert store.list_due_the_rundown() == []
    store.close()


def test_mark_the_rundown_failed_schedules_retry(tmp_path):
    store = StateStore(tmp_path / "test.db")
    job_id = store.insert_pending_the_rundown("2026-03-09")
    assert job_id is not None

    store.mark_the_rundown_failed(job_id, "upstream error")

    assert store.list_due_the_rundown() == []
    row = store._conn.execute(
        "SELECT failure_count, last_error FROM pending_the_rundown WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row["failure_count"] == 1
    assert row["last_error"] == "upstream error"
    store.close()


def test_mark_the_rundown_failed_marks_job_errored_after_retry_budget(tmp_path):
    store = StateStore(tmp_path / "test.db")
    job_id = store.insert_pending_the_rundown("2026-03-09")
    assert job_id is not None

    result = None
    for _ in range(MAX_RETRY_FAILURES):
        result = store.mark_the_rundown_failed(job_id, "upstream error")

    assert result is not None
    assert result.exhausted is True
    assert result.status == "errored"
    assert store.list_due_the_rundown() == []
    row = store._conn.execute(
        "SELECT status, failure_count, last_error FROM pending_the_rundown WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "errored"
    assert row["failure_count"] == MAX_RETRY_FAILURES
    assert row["last_error"] == "upstream error"
    store.close()


def test_list_daily_jobs_by_feed_and_status_the_rundown(tmp_path) -> None:
    """list_daily_jobs returns errored the-rundown jobs filtered by feed_slug and status."""
    store = StateStore(tmp_path / "test.db")
    job_id = store.insert_pending_the_rundown(date_str="2026-03-17")
    assert job_id is not None

    # Drive it to errored
    for _ in range(MAX_RETRY_FAILURES):
        store.mark_the_rundown_failed(job_id, "test error")

    jobs = store.list_daily_jobs(feed_slug="the-rundown", status="errored")
    assert len(jobs) == 1
    assert jobs[0]["date_str"] == "2026-03-17"
    assert jobs[0]["status"] == "errored"
    store.close()


def test_list_daily_jobs_filters_out_other_statuses_the_rundown(tmp_path) -> None:
    """list_daily_jobs does not return completed the-rundown jobs when filtering for errored."""
    store = StateStore(tmp_path / "test.db")
    job_id = store.insert_pending_the_rundown(date_str="2026-03-17")
    assert job_id is not None
    store.mark_the_rundown_completed(job_id)

    jobs = store.list_daily_jobs(feed_slug="the-rundown", status="errored")
    assert jobs == []
    store.close()


def test_reset_the_rundown_job(tmp_path) -> None:
    """reset_the_rundown_job sets a job back to pending with failure_count=0 and last_error=None."""
    store = StateStore(tmp_path / "test.db")
    job_id = store.insert_pending_the_rundown(date_str="2026-03-17")
    assert job_id is not None

    # Drive it to errored
    for _ in range(MAX_RETRY_FAILURES):
        store.mark_the_rundown_failed(job_id, "test error")

    store.reset_the_rundown_job(job_id)

    row = store._conn.execute(
        "SELECT status, failure_count, last_error, process_after FROM pending_the_rundown WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "pending"
    assert row["failure_count"] == 0
    assert row["last_error"] is None
    # process_after should be at or before now (reset to now)
    process_after_dt = datetime.fromisoformat(row["process_after"])
    assert process_after_dt <= datetime.now(tz=UTC)
    store.close()


def test_reset_the_rundown_job_raises_for_unknown_id(tmp_path) -> None:
    """reset_the_rundown_job raises ValueError when the job_id does not exist."""
    store = StateStore(tmp_path / "test.db")
    with pytest.raises(ValueError, match="nonexistent-id"):
        store.reset_the_rundown_job("nonexistent-id")
    store.close()
