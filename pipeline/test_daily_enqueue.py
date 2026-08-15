from pipeline.__main__ import _stale_daily_jobs
from pipeline.db import StateStore


def test_no_stale_jobs_when_all_completed(tmp_path):
    store = StateStore(tmp_path / "s.db")
    store.insert_pending_the_rundown("2026-08-14")
    jobs = store.list_daily_jobs("the-rundown", "pending")
    store.mark_the_rundown_completed(jobs[0]["id"])
    assert _stale_daily_jobs(store, "the-rundown", today="2026-08-17") == []
    store.close()


def test_pending_row_from_an_earlier_date_is_stale(tmp_path):
    store = StateStore(tmp_path / "s.db")
    store.insert_pending_the_rundown("2026-08-14")
    stale = _stale_daily_jobs(store, "the-rundown", today="2026-08-17")
    assert [r["date_str"] for r in stale] == ["2026-08-14"]
    store.close()


def test_todays_own_pending_row_is_not_stale(tmp_path):
    """The row we just inserted must never trigger the audit."""
    store = StateStore(tmp_path / "s.db")
    store.insert_pending_the_rundown("2026-08-17")
    assert _stale_daily_jobs(store, "the-rundown", today="2026-08-17") == []
    store.close()


def test_errored_rows_are_also_stale(tmp_path):
    store = StateStore(tmp_path / "s.db")
    store.insert_pending_fp_digest("2026-08-14")
    job = store.list_daily_jobs("fp-digest", "pending")[0]
    for _ in range(60):  # exhaust the retry budget -> status becomes 'errored'
        retry = store.mark_fp_digest_failed(job["id"], "boom")
        if retry.exhausted:
            break
    stale = _stale_daily_jobs(store, "fp-digest", today="2026-08-17")
    assert [r["date_str"] for r in stale] == ["2026-08-14"]
    store.close()
