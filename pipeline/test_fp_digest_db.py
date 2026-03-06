from __future__ import annotations

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
