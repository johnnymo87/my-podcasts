"""Tests for the `run-stats` CLI command."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from pipeline.__main__ import cli


def test_run_stats_renders_report_for_empty_work_dir(tmp_path: Path) -> None:
    """A work dir with no artifacts still renders (degradation path)."""
    work_dir = tmp_path / "the-rundown-job-123"
    work_dir.mkdir()

    runner = CliRunner()
    result = runner.invoke(cli, ["run-stats", "--work-dir", str(work_dir)])

    assert result.exit_code == 0, result.output
    assert "job-123" in result.output


def test_run_stats_missing_work_dir_errors(tmp_path: Path) -> None:
    """click.Path(exists=True) rejects a nonexistent --work-dir."""
    missing = tmp_path / "does-not-exist"

    runner = CliRunner()
    result = runner.invoke(cli, ["run-stats", "--work-dir", str(missing)])

    assert result.exit_code != 0


def test_run_stats_send_posts_and_ignores_marker(tmp_path: Path, monkeypatch) -> None:
    """--send calls send_alert regardless of any run_stats_sent marker."""
    work_dir = tmp_path / "the-rundown-job-456"
    work_dir.mkdir()
    # A marker that would normally suppress an automatic send.
    (work_dir / "run_stats_sent").touch()

    sent_reports = []
    monkeypatch.setattr(
        "pipeline.alerts.send_alert",
        lambda text, **kw: sent_reports.append(text) or True,
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["run-stats", "--work-dir", str(work_dir), "--send"])

    assert result.exit_code == 0, result.output
    assert len(sent_reports) == 1
    assert "sent" in result.output


def test_run_stats_send_failure_reported(tmp_path: Path, monkeypatch) -> None:
    """A False from send_alert surfaces as 'send failed', not a traceback."""
    work_dir = tmp_path / "the-rundown-job-789"
    work_dir.mkdir()

    monkeypatch.setattr("pipeline.alerts.send_alert", lambda text, **kw: False)

    runner = CliRunner()
    result = runner.invoke(cli, ["run-stats", "--work-dir", str(work_dir), "--send"])

    assert result.exit_code == 0, result.output
    assert "send failed" in result.output
