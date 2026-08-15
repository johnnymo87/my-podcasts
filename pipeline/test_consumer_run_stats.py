from __future__ import annotations

from pathlib import Path


def _make_minimal_work_dir(work_dir: Path) -> None:
    """A directory with no artifacts -- exercises the default-only path."""
    work_dir.mkdir(exist_ok=True)


def test_report_run_stats_swallows_a_broken_work_dir(tmp_path, capsys, monkeypatch):
    from pipeline.consumer import _report_run_stats

    # append_jsonl must never touch the real /persist path, even on a path
    # that (with this test's broken work_dir) never reaches the call.
    monkeypatch.setattr("pipeline.run_stats.append_jsonl", lambda *a, **kw: None)

    # No artifacts at all, and a work dir that is actually a file.
    broken = tmp_path / "not-a-dir"
    broken.write_text("x")
    _report_run_stats(broken, job_id="j", date_str="2026-08-15")  # must not raise
    assert "run stats" in capsys.readouterr().out.lower()


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
