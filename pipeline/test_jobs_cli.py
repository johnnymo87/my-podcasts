"""Tests for the `jobs` CLI group (list and reset subcommands)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from pipeline.__main__ import cli
from pipeline.db import StateStore


# ---------------------------------------------------------------------------
# jobs list
# ---------------------------------------------------------------------------


def test_jobs_list_errored_shows_jobs(tmp_path: Path) -> None:
    """jobs list --status errored returns errored jobs for both feeds."""
    store = StateStore(tmp_path / "state.sqlite3")
    fp_id = store.insert_pending_fp_digest("2026-03-17")
    run_id = store.insert_pending_the_rundown("2026-03-17")
    # Drive both jobs to errored state
    for _ in range(51):
        store.mark_fp_digest_failed(fp_id, "upstream failure")
        store.mark_the_rundown_failed(run_id, "writer timeout")

    with patch(
        "pipeline.__main__._default_state_db_path",
        return_value=tmp_path / "state.sqlite3",
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "list", "--status", "errored"])

    store.close()
    assert result.exit_code == 0, result.output
    assert "2026-03-17" in result.output
    assert "fp-digest" in result.output
    assert "the-rundown" in result.output
    assert "errored" in result.output


def test_jobs_list_filters_by_feed(tmp_path: Path) -> None:
    """jobs list --feed fp-digest --status errored only shows fp-digest jobs."""
    store = StateStore(tmp_path / "state.sqlite3")
    fp_id = store.insert_pending_fp_digest("2026-03-17")
    run_id = store.insert_pending_the_rundown("2026-03-17")
    for _ in range(51):
        store.mark_fp_digest_failed(fp_id, "err")
        store.mark_the_rundown_failed(run_id, "err")

    with patch(
        "pipeline.__main__._default_state_db_path",
        return_value=tmp_path / "state.sqlite3",
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["jobs", "list", "--feed", "fp-digest", "--status", "errored"]
        )

    store.close()
    assert result.exit_code == 0, result.output
    assert "fp-digest" in result.output
    assert "the-rundown" not in result.output


def test_jobs_list_no_jobs_outputs_nothing(tmp_path: Path) -> None:
    """jobs list --status errored outputs nothing when no errored jobs exist."""
    with patch(
        "pipeline.__main__._default_state_db_path",
        return_value=tmp_path / "state.sqlite3",
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["jobs", "list", "--status", "errored"])

    assert result.exit_code == 0, result.output
    # Either empty output or a "no jobs" message — just no crash
    assert "errored" not in result.output or "no jobs" in result.output.lower()


# ---------------------------------------------------------------------------
# jobs reset
# ---------------------------------------------------------------------------


def test_jobs_reset_by_feed_and_date_resets_to_pending(tmp_path: Path) -> None:
    """jobs reset --feed fp-digest --date 2026-03-17 resets the errored job to pending."""
    store = StateStore(tmp_path / "state.sqlite3")
    fp_id = store.insert_pending_fp_digest("2026-03-17")
    for _ in range(51):
        store.mark_fp_digest_failed(fp_id, "err")

    # Confirm job is errored before reset
    row = store._conn.execute(
        "SELECT status FROM pending_fp_digest WHERE id = ?", (fp_id,)
    ).fetchone()
    assert row["status"] == "errored"

    with patch(
        "pipeline.__main__._default_state_db_path",
        return_value=tmp_path / "state.sqlite3",
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["jobs", "reset", "--feed", "fp-digest", "--date", "2026-03-17"]
        )

    assert result.exit_code == 0, result.output
    assert "reset" in result.output.lower() or fp_id in result.output

    row = store._conn.execute(
        "SELECT status, failure_count FROM pending_fp_digest WHERE id = ?", (fp_id,)
    ).fetchone()
    assert row["status"] == "pending"
    assert row["failure_count"] == 0
    store.close()


def test_jobs_reset_by_job_id_resets_to_pending(tmp_path: Path) -> None:
    """jobs reset --feed the-rundown --job-id <id> resets a specific errored job."""
    store = StateStore(tmp_path / "state.sqlite3")
    run_id = store.insert_pending_the_rundown("2026-03-17")
    for _ in range(51):
        store.mark_the_rundown_failed(run_id, "err")

    with patch(
        "pipeline.__main__._default_state_db_path",
        return_value=tmp_path / "state.sqlite3",
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["jobs", "reset", "--feed", "the-rundown", "--job-id", run_id]
        )

    assert result.exit_code == 0, result.output

    row = store._conn.execute(
        "SELECT status, failure_count FROM pending_the_rundown WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["status"] == "pending"
    assert row["failure_count"] == 0
    store.close()


def test_jobs_reset_clears_artifacts_by_default(tmp_path: Path) -> None:
    """Default reset removes script.txt, summary.txt, and covered.json from work dir."""
    store = StateStore(tmp_path / "state.sqlite3")
    fp_id = store.insert_pending_fp_digest("2026-03-17")
    for _ in range(51):
        store.mark_fp_digest_failed(fp_id, "err")

    # Simulate an existing work dir with artifacts
    work_dir = tmp_path / f"fp-digest-{fp_id}"
    work_dir.mkdir()
    (work_dir / "script.txt").write_text("old script")
    (work_dir / "summary.txt").write_text("old summary")
    (work_dir / "covered.json").write_text('["headline"]')
    (work_dir / "plan.json").write_text('{"themes": []}')  # should NOT be deleted

    with patch(
        "pipeline.__main__._default_state_db_path",
        return_value=tmp_path / "state.sqlite3",
    ):
        with patch("pipeline.__main__._jobs_work_dir_base", return_value=tmp_path):
            runner = CliRunner()
            result = runner.invoke(
                cli, ["jobs", "reset", "--feed", "fp-digest", "--date", "2026-03-17"]
            )

    assert result.exit_code == 0, result.output
    # Artifacts cleared
    assert not (work_dir / "script.txt").exists()
    assert not (work_dir / "summary.txt").exists()
    assert not (work_dir / "covered.json").exists()
    # plan.json preserved
    assert (work_dir / "plan.json").exists()
    store.close()


def test_jobs_reset_keeps_artifacts_with_flag(tmp_path: Path) -> None:
    """--keep-artifacts flag preserves script.txt, summary.txt, covered.json."""
    store = StateStore(tmp_path / "state.sqlite3")
    fp_id = store.insert_pending_fp_digest("2026-03-17")
    for _ in range(51):
        store.mark_fp_digest_failed(fp_id, "err")

    work_dir = tmp_path / f"fp-digest-{fp_id}"
    work_dir.mkdir()
    (work_dir / "script.txt").write_text("old script")
    (work_dir / "summary.txt").write_text("old summary")
    (work_dir / "covered.json").write_text('["headline"]')

    with patch(
        "pipeline.__main__._default_state_db_path",
        return_value=tmp_path / "state.sqlite3",
    ):
        with patch("pipeline.__main__._jobs_work_dir_base", return_value=tmp_path):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "jobs",
                    "reset",
                    "--feed",
                    "fp-digest",
                    "--date",
                    "2026-03-17",
                    "--keep-artifacts",
                ],
            )

    assert result.exit_code == 0, result.output
    # Artifacts preserved
    assert (work_dir / "script.txt").exists()
    assert (work_dir / "summary.txt").exists()
    assert (work_dir / "covered.json").exists()
    store.close()


def test_jobs_reset_requires_feed(tmp_path: Path) -> None:
    """jobs reset fails with a non-zero exit code when --feed is missing."""
    runner = CliRunner()
    result = runner.invoke(cli, ["jobs", "reset", "--date", "2026-03-17"])
    assert result.exit_code != 0


def test_jobs_reset_requires_date_or_job_id(tmp_path: Path) -> None:
    """jobs reset fails when neither --date nor --job-id is supplied."""
    runner = CliRunner()
    result = runner.invoke(cli, ["jobs", "reset", "--feed", "fp-digest"])
    assert result.exit_code != 0


def test_jobs_reset_nonexistent_job_id_emits_clean_error(tmp_path: Path) -> None:
    """jobs reset --job-id <bad-id> prints a clean error and exits nonzero (no traceback)."""
    with patch(
        "pipeline.__main__._default_state_db_path",
        return_value=tmp_path / "state.sqlite3",
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["jobs", "reset", "--feed", "fp-digest", "--job-id", "does-not-exist"],
        )

    # Must be nonzero
    assert result.exit_code != 0
    # Must mention the bad ID in the output (stderr is mixed into output by CliRunner)
    assert "does-not-exist" in result.output
    # Must NOT be an unhandled exception (no traceback)
    assert "Traceback" not in result.output
    assert "ValueError" not in result.output
