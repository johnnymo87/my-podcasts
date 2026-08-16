from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from pipeline.__main__ import _enqueue_daily_job, _stale_daily_jobs, cli
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


# ---------------------------------------------------------------------------
# Task 3: `the-rundown` enqueue-only
# ---------------------------------------------------------------------------


def test_the_rundown_command_only_enqueues(tmp_path):
    """The CLI must NOT collect, generate, or publish - only enqueue.

    Patching R2Client is not incidental. BEFORE the implementation lands this
    test drives the OLD inline pipeline, which would construct a real R2 client
    and attempt a real ~6-minute collection + LLM + TTS + publish. On a devbox
    shell the R2 env vars are unset so it dies early by luck; in an operator
    shell with secrets sourced, running this "failing test" would fire a real
    production publish out of pytest. The mock makes that impossible, and
    asserting it was never called is also the only DIRECT proof that the new
    command performs no pipeline work - absence of the helper name is a
    resurrection tripwire, not evidence of behavior.
    """
    db = tmp_path / "s.db"
    fake_r2 = MagicMock()
    with (
        patch("pipeline.__main__._default_state_db_path", return_value=db),
        patch("pipeline.__main__.R2Client", fake_r2),
    ):
        result = CliRunner().invoke(cli, ["the-rundown", "--date", "2026-08-17"])
    assert result.exit_code == 0, result.output
    assert "Queued The Rundown job" in result.output
    fake_r2.assert_not_called()
    store = StateStore(db)
    assert len(store.list_daily_jobs("the-rundown", "pending")) == 1
    store.close()


def test_enqueue_reports_errored_status_and_how_to_reset(tmp_path):
    """An errored row is NOT eligible for execution; saying "already exists" lies."""
    db = tmp_path / "s.db"
    store = StateStore(db)
    job_id = store.insert_pending_the_rundown("2026-08-17")
    for _ in range(60):
        if store.mark_the_rundown_failed(job_id, "boom").exhausted:
            break
    store.close()
    with patch("pipeline.__main__._default_state_db_path", return_value=db):
        result = CliRunner().invoke(cli, ["the-rundown", "--date", "2026-08-17"])
    assert "status=errored" in result.output
    assert "jobs reset" in result.output


def test_enqueue_rejects_a_malformed_date_via_cli(tmp_path):
    db = tmp_path / "s.db"
    with patch("pipeline.__main__._default_state_db_path", return_value=db):
        result = CliRunner().invoke(cli, ["the-rundown", "--date", "17-08-2026"])
    assert result.exit_code != 0
    store = StateStore(db)
    assert store.list_daily_jobs("the-rundown", "pending") == []
    store.close()


def test_the_rundown_command_is_idempotent(tmp_path):
    db = tmp_path / "s.db"
    with patch("pipeline.__main__._default_state_db_path", return_value=db):
        runner = CliRunner()
        runner.invoke(cli, ["the-rundown", "--date", "2026-08-17"])
        result = runner.invoke(cli, ["the-rundown", "--date", "2026-08-17"])
    assert result.exit_code == 0
    assert "already exists" in result.output


def test_the_rundown_full_run_helper_is_deleted():
    """The inline CLI pipeline is the double-publish bug; it must not return.

    Only the Rundown half is asserted here; the FP half is asserted once
    Task 4 deletes _fp_digest_full_run (see test_full_run_helpers_are_deleted).
    """
    import pipeline.__main__ as m

    assert not hasattr(m, "_the_rundown_full_run")


def test_shared_helpers_survive():
    """consumer.py:323 lazily imports find_rundown_article_source from here."""
    import pipeline.__main__ as m

    assert hasattr(m, "find_rundown_article_source")
    assert hasattr(m, "_find_rundown_article_text")  # still used by --dry-run


# ---------------------------------------------------------------------------
# Task 4: `fp-digest` enqueue-only + date-default harmonization
# ---------------------------------------------------------------------------


def test_fp_digest_command_only_enqueues(tmp_path):
    db = tmp_path / "s.db"
    fake_r2 = MagicMock()
    with (
        patch("pipeline.__main__._default_state_db_path", return_value=db),
        patch("pipeline.__main__.R2Client", fake_r2),
    ):
        result = CliRunner().invoke(cli, ["fp-digest", "--date", "2026-08-17"])
    assert result.exit_code == 0, result.output
    assert "Queued FP Digest job" in result.output
    fake_r2.assert_not_called()
    store = StateStore(db)
    assert len(store.list_daily_jobs("fp-digest", "pending")) == 1
    store.close()


def test_fp_digest_command_is_idempotent(tmp_path):
    db = tmp_path / "s.db"
    with patch("pipeline.__main__._default_state_db_path", return_value=db):
        runner = CliRunner()
        runner.invoke(cli, ["fp-digest", "--date", "2026-08-17"])
        result = runner.invoke(cli, ["fp-digest", "--date", "2026-08-17"])
    assert result.exit_code == 0
    assert "already exists" in result.output


class _FixedDateTime(datetime):
    """A datetime.datetime replacement whose .now() returns a fixed instant.

    The instant is 2026-08-16T02:00:00 UTC, which is 2026-08-15T22:00:00 in
    America/New_York - deliberately chosen so the UTC calendar date and the
    ET calendar date disagree. A test built on the real wall clock only
    catches a UTC-vs-ET regression during the ~4h/day window where the two
    zones disagree about the date, so it is flaky by construction (it must
    happen to run in that window). Freezing the clock makes the assertion
    unconditionally exercise the disagreement instead of depending on when
    the suite happens to run.
    """

    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)
        return base if tz is None else base.astimezone(tz)


def test_fp_digest_and_rundown_default_dates_agree():
    """Both commands must derive the same default date for a fixed clock.

    Before this fix fp-digest defaulted from UTC while the-rundown defaulted
    from America/New_York, so a manual FP enqueue after ~20:00 ET silently
    filed tomorrow's date.
    """
    import pipeline.__main__ as m

    fp_dates: list[str] = []
    rundown_dates: list[str] = []
    with (
        patch.object(m, "_enqueue_daily_job") as enqueue,
        patch.object(m, "_audit_previous_daily_run"),
        patch.object(m, "_default_state_db_path", return_value=Path("/dev/null")),
        patch.object(m, "StateStore"),
        patch("datetime.datetime", _FixedDateTime),
    ):

        def _capture(_store, feed_slug, date_str):
            (fp_dates if feed_slug == "fp-digest" else rundown_dates).append(date_str)
            return "job-id"

        enqueue.side_effect = _capture
        CliRunner().invoke(cli, ["fp-digest"])
        CliRunner().invoke(cli, ["the-rundown"])

    # Sanity check that the freeze actually landed on a UTC/ET-disagreement
    # instant - otherwise this test would pass even without the fix.
    assert fp_dates == ["2026-08-15"]
    assert rundown_dates == ["2026-08-15"]
    assert fp_dates == rundown_dates


def test_full_run_helpers_are_deleted():
    """The inline CLI pipelines are the double-publish bug; they must not return.

    Completes the check started in test_the_rundown_full_run_helper_is_deleted
    (Task 3) now that Task 4 has deleted the FP half too.
    """
    import pipeline.__main__ as m

    assert not hasattr(m, "_the_rundown_full_run")
    assert not hasattr(m, "_fp_digest_full_run")
