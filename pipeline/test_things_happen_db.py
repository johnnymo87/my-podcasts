from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from pipeline.db import StateStore


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
