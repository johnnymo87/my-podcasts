import pytest

from pipeline.__main__ import _enqueue_daily_job, _stale_daily_jobs
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


def test_enqueue_inserts_a_pending_row(tmp_path, capsys):
    store = StateStore(tmp_path / "s.db")
    job_id = _enqueue_daily_job(store, "the-rundown", "2026-08-17")
    assert job_id is not None
    rows = store.list_daily_jobs("the-rundown", "pending")
    assert [r["date_str"] for r in rows] == ["2026-08-17"]
    assert "2026-08-17" in capsys.readouterr().out
    store.close()


def test_enqueue_is_idempotent_for_the_same_date(tmp_path):
    """A Persistent=true timer catch-up fire must not create a second job."""
    store = StateStore(tmp_path / "s.db")
    first = _enqueue_daily_job(store, "the-rundown", "2026-08-17")
    second = _enqueue_daily_job(store, "the-rundown", "2026-08-17")
    assert first is not None
    assert second is None
    assert len(store.list_daily_jobs("the-rundown", "pending")) == 1
    store.close()


def test_enqueue_supports_fp_digest(tmp_path):
    store = StateStore(tmp_path / "s.db")
    assert _enqueue_daily_job(store, "fp-digest", "2026-08-17") is not None
    assert len(store.list_daily_jobs("fp-digest", "pending")) == 1
    store.close()


def test_enqueue_rejects_unknown_feed(tmp_path):
    store = StateStore(tmp_path / "s.db")
    with pytest.raises(ValueError):
        _enqueue_daily_job(store, "nonsense", "2026-08-17")
    store.close()


def test_enqueue_rejects_a_malformed_date_before_any_insert(tmp_path):
    """The date guard must run before the insert, not after."""
    store = StateStore(tmp_path / "s.db")
    with pytest.raises(ValueError):
        _enqueue_daily_job(store, "the-rundown", "17-08-2026")
    assert store.list_daily_jobs("the-rundown", "pending") == []
    store.close()


def test_enqueue_reports_pending_status_on_collision(tmp_path, capsys):
    store = StateStore(tmp_path / "s.db")
    _enqueue_daily_job(store, "the-rundown", "2026-08-17")
    capsys.readouterr()
    result = _enqueue_daily_job(store, "the-rundown", "2026-08-17")
    out = capsys.readouterr().out
    assert result is None
    assert "status=pending" in out
    store.close()


def test_enqueue_reports_errored_status_and_reset_hint_on_collision(tmp_path, capsys):
    """An errored row is NOT eligible for execution; 'already exists' alone lies."""
    store = StateStore(tmp_path / "s.db")
    job_id = store.insert_pending_the_rundown("2026-08-17")
    for _ in range(60):  # exhaust the retry budget -> status becomes 'errored'
        if store.mark_the_rundown_failed(job_id, "boom").exhausted:
            break
    capsys.readouterr()
    result = _enqueue_daily_job(store, "the-rundown", "2026-08-17")
    out = capsys.readouterr().out
    assert result is None
    assert "status=errored" in out
    assert "jobs reset" in out
    store.close()


def test_enqueue_reports_completed_status_on_collision(tmp_path, capsys):
    store = StateStore(tmp_path / "s.db")
    job_id = store.insert_pending_the_rundown("2026-08-17")
    store.mark_the_rundown_completed(job_id)
    capsys.readouterr()
    result = _enqueue_daily_job(store, "the-rundown", "2026-08-17")
    out = capsys.readouterr().out
    assert result is None
    assert "status=completed" in out
    store.close()
