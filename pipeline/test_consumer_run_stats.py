from __future__ import annotations

from pathlib import Path


def _make_minimal_work_dir(work_dir: Path) -> None:
    """A directory with no artifacts -- exercises the default-only path."""
    work_dir.mkdir(exist_ok=True)


def test_report_run_stats_swallows_a_broken_work_dir(tmp_path, capsys, monkeypatch):
    """A broken work_dir (a file, not a directory) fails only the local
    run_stats.json write -- collect_run_stats degrades to defaults for a
    non-directory work_dir (never raises), so the JSONL append and the
    Telegram send are still reached. This changed under Finding 3: the three
    sinks used to share one try/except, so a write failure here used to
    suppress the JSONL append and the Telegram send too. Both other sinks
    must still be mocked -- neither the real /persist path nor a real
    Telegram send may be touched by this test.
    """
    from pipeline.consumer import _report_run_stats

    jsonl_calls = []
    sent = []
    monkeypatch.setattr(
        "pipeline.run_stats.append_jsonl",
        lambda stats, path: jsonl_calls.append(path),
    )
    monkeypatch.setattr(
        "pipeline.alerts.send_alert",
        lambda text, severity="info": sent.append(text) or True,
    )

    # No artifacts at all, and a work dir that is actually a file.
    broken = tmp_path / "not-a-dir"
    broken.write_text("x")
    _report_run_stats(broken, job_id="j", date_str="2026-08-15")  # must not raise

    out = capsys.readouterr().out.lower()
    assert "run stats json write failed" in out
    # The other two sinks are unaffected by the write failure.
    assert len(jsonl_calls) == 1
    assert len(sent) == 1
    assert not (broken / "run_stats_sent").exists()  # broken has no dir to mark


def test_report_run_stats_is_idempotent(tmp_path, monkeypatch):
    from pipeline.consumer import _report_run_stats

    sent = []
    jsonl_calls = []
    monkeypatch.setattr(
        "pipeline.alerts.send_alert",
        lambda text, severity="info": sent.append(text) or True,
    )
    monkeypatch.setattr(
        "pipeline.run_stats.append_jsonl",
        lambda stats, path: jsonl_calls.append(path),
    )

    work_dir = tmp_path / "the-rundown-j"
    _make_minimal_work_dir(work_dir)

    _report_run_stats(work_dir, job_id="j", date_str="2026-08-15")
    _report_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert len(sent) == 1
    assert (work_dir / "run_stats_sent").exists()
    # run_stats.json and the jsonl append are cheap/local and happen every
    # call; only the Telegram send is marker-gated.
    assert (work_dir / "run_stats.json").exists()
    assert len(jsonl_calls) == 2


def test_jsonl_append_failure_does_not_block_the_telegram_send(
    tmp_path, capsys, monkeypatch
):
    """A raising append_jsonl (e.g. a full /persist) must not gate the
    human-visible Telegram report -- the whole point of Finding 3. The old
    single try/except would have skipped send_alert entirely here.
    """
    from pipeline.consumer import _report_run_stats

    sent = []

    def _boom(stats, path):
        raise OSError("disk full")

    monkeypatch.setattr("pipeline.run_stats.append_jsonl", _boom)
    monkeypatch.setattr(
        "pipeline.alerts.send_alert",
        lambda text, severity="info": sent.append(text) or True,
    )

    work_dir = tmp_path / "the-rundown-m"
    _make_minimal_work_dir(work_dir)

    _report_run_stats(work_dir, job_id="m", date_str="2026-08-15")  # must not raise

    out = capsys.readouterr().out.lower()
    assert "run stats jsonl append failed" in out
    assert len(sent) == 1
    assert (work_dir / "run_stats_sent").exists()
    assert (work_dir / "run_stats.json").exists()


def test_report_run_stats_marks_only_after_successful_send(tmp_path, monkeypatch):
    from pipeline.consumer import _report_run_stats

    monkeypatch.setattr("pipeline.run_stats.append_jsonl", lambda *a, **kw: None)
    monkeypatch.setattr(
        "pipeline.alerts.send_alert", lambda text, severity="info": False
    )

    work_dir = tmp_path / "the-rundown-k"
    _make_minimal_work_dir(work_dir)

    _report_run_stats(work_dir, job_id="k", date_str="2026-08-15")

    assert not (work_dir / "run_stats_sent").exists()
