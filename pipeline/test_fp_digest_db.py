from __future__ import annotations

from datetime import UTC, datetime

from pipeline.db import StateStore


def test_fp_digest_table_created(tmp_path) -> None:
    """Verify that the pending_fp_digest table is created."""
    store = StateStore(tmp_path / "test.sqlite3")
    row = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_fp_digest'"
    ).fetchone()
    assert row is not None, "pending_fp_digest table should exist"
    store.close()


def test_insert_and_list_due(tmp_path) -> None:
    """Insert a pending fp_digest job and verify list_due returns it."""
    store = StateStore(tmp_path / "test.sqlite3")
    job_id = store.insert_pending_fp_digest(date_str="2026-03-06")
    assert job_id is not None
    due = store.list_due_fp_digest()
    assert len(due) == 1
    assert due[0]["id"] == job_id
    assert due[0]["date_str"] == "2026-03-06"
    store.close()


def test_mark_completed(tmp_path) -> None:
    """Insert a job, mark it completed, verify list_due returns empty."""
    store = StateStore(tmp_path / "test.sqlite3")
    job_id = store.insert_pending_fp_digest(date_str="2026-03-06")
    assert job_id is not None
    store.mark_fp_digest_completed(job_id)
    due = store.list_due_fp_digest()
    assert len(due) == 0
    store.close()


def test_no_duplicate_for_same_date(tmp_path) -> None:
    """Inserting the same date twice should return None on the second call."""
    store = StateStore(tmp_path / "test.sqlite3")
    job_id_1 = store.insert_pending_fp_digest(date_str="2026-03-06")
    job_id_2 = store.insert_pending_fp_digest(date_str="2026-03-06")
    assert job_id_1 is not None
    assert job_id_2 is None
    due = store.list_due_fp_digest()
    assert len(due) == 1
    store.close()


def test_mark_fp_digest_failed_schedules_retry(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    job_id = store.insert_pending_fp_digest(date_str="2026-03-06")
    assert job_id is not None

    store.mark_fp_digest_failed(job_id, "upstream error")

    assert store.list_due_fp_digest() == []
    row = store._conn.execute(
        "SELECT failure_count, last_error, process_after FROM pending_fp_digest WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row["failure_count"] == 1
    assert row["last_error"] == "upstream error"
    assert datetime.fromisoformat(row["process_after"]) > datetime.now(tz=UTC)
    store.close()


def test_mark_fp_digest_failed_increases_backoff(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    job_id = store.insert_pending_fp_digest(date_str="2026-03-06")
    assert job_id is not None

    store.mark_fp_digest_failed(job_id, "first")
    first = store._conn.execute(
        "SELECT failure_count, process_after FROM pending_fp_digest WHERE id = ?",
        (job_id,),
    ).fetchone()
    store.mark_fp_digest_failed(job_id, "second")
    second = store._conn.execute(
        "SELECT failure_count, process_after FROM pending_fp_digest WHERE id = ?",
        (job_id,),
    ).fetchone()

    assert first["failure_count"] == 1
    assert second["failure_count"] == 2
    assert datetime.fromisoformat(second["process_after"]) > datetime.fromisoformat(
        first["process_after"]
    )
    store.close()
