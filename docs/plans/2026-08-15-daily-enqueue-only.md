# Daily Job Enqueue-Only Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop The Rundown and FP Digest publishing two episodes per weekday by making the daily CLI enqueue-only, so the consumer is the single executor.

**Architecture:** The systemd timer currently runs `python -m pipeline the-rundown`, which inserts a `pending` job row and then executes the entire pipeline inline (~6 min), marking the job complete only at the very end. The consumer polls `status='pending'` every 10s with no claim, so it starts a *second* full pipeline on the same job within ~10s. We delete the CLI's inline pipeline entirely: the command inserts the row, reports the job id, and exits. This removes the race by construction rather than coordinating around it with a lock or a claim, and it deletes a duplicate implementation that has already silently drifted behind the consumer.

**Tech Stack:** Python 3, click, sqlite3, pytest, systemd.

---

## Context an implementer must have

Read these before starting. They are not optional; several are load-bearing.

**The two runners share a work dir, not just a DB row.** `date_str` is `UNIQUE` on both `pending_the_rundown` and `pending_fp_digest`, so there is exactly one job row per date and both runners derive the *same* work dir from the same job id. Today two processes concurrently write `script.txt`, `summary.txt`, `raw_writer_output.txt` (the retry-reuse artifact), `articles/` and `enrichment/`. Making the CLI enqueue-only fixes this too — do not add separate work-dir locking.

**The CLI path has drifted and is now worse than the consumer path.** `_the_rundown_full_run` resolves article text with `_find_rundown_article_text`, while the consumer uses `_assemble_writer_inputs` (open-access Exa append, PR #9) plus `_report_run_stats` (funnel telemetry, PR #8). Deleting the CLI pipeline is therefore a bug fix twice over. Do not try to port the missing features into the CLI.

**`__main__.py` is NOT just a CLI entry point.** `pipeline/consumer.py:323` contains `from pipeline.__main__ import find_rundown_article_source` — a *lazy*, function-local import on the Rundown script path. Consequences:

- `find_rundown_article_source` **must survive** this work. So must `_find_rundown_article_text`, which the surviving `--dry-run` paths still call.
- Breaking `__main__.py` breaks the running consumer, but **not at startup** — `systemctl status` stays green and it fails mid-job. This is more dangerous than a crash-loop, not less.
- The consumer has not run a Rundown job since its restart, so `pipeline.__main__` is not in its `sys.modules` and will be imported **from disk** at the next 04:30 run. Never leave a half-finished state on `main` overnight.

**Deploy is a restart.** `sudo systemctl restart my-podcasts-consumer`, after merging AND pulling on the live tree at `/home/dev/projects/my-podcasts`. Check for in-flight jobs first.

**Never** run `git stash`, `git reset`, `git checkout -- <path>`, `git restore`, or `git clean` — this is a shared checkout and it has already cost a peer's uncommitted data once.

**Verification after every task:** `uv run pytest -q` (baseline **500** passing), `uv run ruff check .`, `uv run ruff format --check .`. All must be clean before you commit.

---

### Task 1: A stale-job detector for the enqueue-time audit

Once the CLI only enqueues, a green timer no longer means "an episode published" — it means "a row was inserted." We need the timer to audit its own *previous* fire. This task builds the pure query half; Task 6 wires it up.

**Files:**
- Modify: `pipeline/__main__.py`
- Test: `pipeline/test_daily_enqueue.py` (create)

**Step 1: Write the failing tests**

Note we compare `date_str` lexically. These are `YYYY-MM-DD` strings, so `<` is a correct date comparison and needs no parsing — deliberately simpler and more robust than reading timestamps.

```python
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
```

**Step 2: Run to verify they fail**

Run: `uv run pytest pipeline/test_daily_enqueue.py -q`
Expected: FAIL, `ImportError: cannot import name '_stale_daily_jobs'`

**Step 3: Implement**

Add near the other daily-job helpers in `pipeline/__main__.py`:

```python
def _stale_daily_jobs(store: StateStore, feed_slug: str, today: str) -> list[dict]:
    """Return daily job rows for *feed_slug* left unfinished on an earlier date.

    A row still 'pending' or 'errored' for a date before *today* means a
    previous run was enqueued but never carried to completion - the signal that
    the consumer is wedged, stopped, or exhausted its retries. Comparison is
    lexical because date_str is always YYYY-MM-DD.
    """
    stale: list[dict] = []
    for status in ("pending", "errored"):
        stale.extend(
            row
            for row in store.list_daily_jobs(feed_slug, status)
            if row["date_str"] < today
        )
    return sorted(stale, key=lambda r: r["date_str"])
```

**Step 4: Run to verify they pass**

Run: `uv run pytest pipeline/test_daily_enqueue.py -q` -> 4 passed
Then: `uv run pytest -q` -> 504 passed

**Step 5: Commit**

```bash
git add pipeline/__main__.py pipeline/test_daily_enqueue.py
git commit -m "feat(daily): detect daily jobs left unfinished on an earlier date"
```

---

### Task 2: The shared enqueue helper

**Files:**
- Modify: `pipeline/__main__.py`
- Test: `pipeline/test_daily_enqueue.py`

**Step 1: Write the failing tests**

```python
import pytest
from pipeline.__main__ import _enqueue_daily_job


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
```

**Step 2: Run to verify they fail**

Run: `uv run pytest pipeline/test_daily_enqueue.py -q`
Expected: FAIL, `ImportError: cannot import name '_enqueue_daily_job'`

**Step 3: Implement**

```python
_DAILY_FEED_LABELS: dict[str, str] = {
    "the-rundown": "The Rundown",
    "fp-digest": "FP Digest",
}


def _enqueue_daily_job(store: StateStore, feed_slug: str, date_str: str) -> str | None:
    """Insert a pending daily job and report the outcome. Returns the job id.

    Returns None when a row for *date_str* already exists, which is a normal,
    successful outcome: date_str is UNIQUE, so a Persistent=true timer catch-up
    fire is idempotent for free.
    """
    label = _DAILY_FEED_LABELS.get(feed_slug)
    if label is None:
        raise ValueError(f"Unknown feed_slug: {feed_slug!r}")

    # Reject a malformed date here rather than letting the consumer retry a
    # garbage row for ~12h. The old inline CLI failed loudly in the operator's
    # terminal; enqueue-only would otherwise make the same typo silent.
    datetime.strptime(date_str, "%Y-%m-%d")

    if feed_slug == "the-rundown":
        job_id = store.insert_pending_the_rundown(date_str)
    else:
        job_id = store.insert_pending_fp_digest(date_str)

    if job_id is not None:
        click.echo(f"Queued {label} job {job_id} for {date_str}.")
        click.echo("The consumer will pick it up within ~10s. Follow it with:")
        click.echo("  journalctl -fu my-podcasts-consumer")
        return job_id

    # A row already exists. Report its ACTUAL status: 'errored' rows are NOT
    # eligible for execution (list_due_* filters status='pending'), so a bare
    # "already exists" would leave the operator believing work was queued when
    # nothing will ever run.
    existing = next(
        (
            row
            for status in ("pending", "errored", "completed")
            for row in store.list_daily_jobs(feed_slug, status)
            if row["date_str"] == date_str
        ),
        None,
    )
    status = existing["status"] if existing else "unknown"
    click.echo(f"{label} job already exists for {date_str} (status={status}).")
    if status == "errored":
        click.echo("It is errored, so the consumer will NOT run it. Reset it with:")
        click.echo(f"  uv run python -m pipeline jobs reset --feed {feed_slug} "
                   f"--date {date_str}")
    elif status == "completed":
        click.echo("Already published; nothing to do.")
    return None
```

Add `from datetime import datetime` at module top if not already imported at that scope.

**Step 4: Run to verify they pass**

Run: `uv run pytest pipeline/test_daily_enqueue.py -q` -> 8 passed

**Step 5: Commit**

```bash
git add pipeline/__main__.py pipeline/test_daily_enqueue.py
git commit -m "feat(daily): add shared enqueue helper for daily jobs"
```

---

### Task 3: Make `the-rundown` enqueue-only

This is the commit that actually fixes the bug for The Rundown.

**Files:**
- Modify: `pipeline/__main__.py` (delete `_the_rundown_full_run`, lines ~659-766; rewire `the_rundown_command` ~573-586)
- Test: `pipeline/test_daily_enqueue.py`

**Step 1: Write the failing test**

```python
from click.testing import CliRunner
from unittest.mock import MagicMock, patch
from pipeline.__main__ import cli


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
    store.insert_pending_the_rundown("2026-08-17")
    job = store.list_daily_jobs("the-rundown", "pending")[0]
    for _ in range(60):
        if store.mark_the_rundown_failed(job["id"], "boom").exhausted:
            break
    store.close()
    with patch("pipeline.__main__._default_state_db_path", return_value=db):
        result = CliRunner().invoke(cli, ["the-rundown", "--date", "2026-08-17"])
    assert "status=errored" in result.output
    assert "jobs reset" in result.output


def test_enqueue_rejects_a_malformed_date(tmp_path):
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
```

Also assert the dead code is gone, so a later refactor cannot quietly resurrect it:

```python
def test_full_run_helpers_are_deleted():
    """The inline CLI pipelines are the double-publish bug; they must not return."""
    import pipeline.__main__ as m

    assert not hasattr(m, "_the_rundown_full_run")
    assert not hasattr(m, "_fp_digest_full_run")


def test_shared_helpers_survive():
    """consumer.py:323 lazily imports find_rundown_article_source from here."""
    import pipeline.__main__ as m

    assert hasattr(m, "find_rundown_article_source")
    assert hasattr(m, "_find_rundown_article_text")  # still used by --dry-run
```

**Step 2: Run to verify they fail**

Run: `uv run pytest pipeline/test_daily_enqueue.py -q`
Expected: FAIL — `test_full_run_helpers_are_deleted` fails (they still exist), and the command still tries to run the whole pipeline.

**Step 3: Implement**

Delete `_the_rundown_full_run` entirely. Rewrite the dispatch in `the_rundown_command`:

```python
    if dry_run:
        _the_rundown_dry_run(date_str, lookback_days)
        return

    store = StateStore(_default_state_db_path())
    try:
        _enqueue_daily_job(store, "the-rundown", date_str)
        _audit_previous_daily_run(store, "the-rundown", date_str)
    finally:
        store.close()
```

`_audit_previous_daily_run` does not exist yet — add a temporary no-op stub returning `None` so this commit is independently safe, and implement it in Task 6. **Do not** leave a call to an undefined name: an `AttributeError` here would surface inside the running consumer via the lazy import.

**Step 4: Run to verify they pass**

Run: `uv run pytest -q`
Expected: green. Some existing tests may reference the deleted function; update them in this commit.

**Step 5: Commit**

```bash
git add -A
git commit -m "fix(rundown): make the daily CLI enqueue-only

The timer ran the whole pipeline inline while holding the job row at
status='pending', and the consumer picked the same row up ~10s later and
ran it again - two TTS renders, two PUTs to one r2_key, two episodes rows.
16 duplicated keys across 9 days since the last cleanup.

Deleting the inline path rather than locking around it also removes a
duplicate pipeline that had drifted: it lacked both the open-access Exa
append and the funnel telemetry the consumer path gained last week."
```

---

### Task 4: Make `fp-digest` enqueue-only

Identical surgery on the twin. **Both feeds must land before the next 04:30 fire**, or FP Digest still double-publishes.

**Files:**
- Modify: `pipeline/__main__.py` (delete `_fp_digest_full_run`, lines ~442-550; rewire `fp_digest_command` ~356-368)
- Test: `pipeline/test_daily_enqueue.py`

**Steps:** mirror Task 3 exactly, with `fp-digest` / "Queued FP Digest job". `test_full_run_helpers_are_deleted` from Task 3 now covers the second half and should go green.

**While you are here, harmonize the date default.** `fp_digest_command`
(`__main__.py:360-363`) defaults `date_str` from **UTC**, while
`the_rundown_command` (`:577-581`) uses **America/New_York**. So a manual FP
enqueue after ~20:00 ET silently files tomorrow's date. This is pre-existing,
but manual enqueue becomes the normal operator gesture after this change, so fix
it to ET to match. Add a test asserting both commands derive the same default
date for a fixed clock.

**Commit:**

```bash
git commit -m "fix(fp-digest): make the daily CLI enqueue-only"
```

---

### Task 5: Fail loudly on `--lookback` without `--dry-run`

After Tasks 3-4 the consumer computes the lookback via `_compute_lookback`, so a `--lookback` passed to the enqueue path is silently ignored. Silently ignoring an operator's explicit instruction is this project's recurring failure mode; make it an error instead.

**Files:**
- Modify: `pipeline/__main__.py` (both commands)
- Test: `pipeline/test_daily_enqueue.py`

**Step 1: Write the failing test**

```python
def test_lookback_without_dry_run_is_an_error(tmp_path):
    db = tmp_path / "s.db"
    with patch("pipeline.__main__._default_state_db_path", return_value=db):
        result = CliRunner().invoke(
            cli, ["the-rundown", "--date", "2026-08-17", "--lookback", "5"]
        )
    assert result.exit_code != 0
    assert "--lookback" in result.output
    # and it must not have enqueued anything
    store = StateStore(db)
    assert store.list_daily_jobs("the-rundown", "pending") == []
    store.close()


def test_lookback_still_works_with_dry_run():
    """--dry-run never touches the DB, so --lookback remains meaningful there."""
    with patch("pipeline.__main__._the_rundown_dry_run") as dry:
        CliRunner().invoke(cli, ["the-rundown", "--dry-run", "--lookback", "5"])
    # Assert on the value, not the call shape, so a later switch to keyword
    # arguments does not fail a test that still holds.
    assert 5 in dry.call_args.args or 5 in dry.call_args.kwargs.values()
```

**Step 2-4:** raise before opening the store:

```python
    if lookback_days is not None and not dry_run:
        raise click.UsageError(
            "--lookback only applies to --dry-run. The consumer computes the "
            "lookback window when it executes the job."
        )
```

**Step 5: Commit**

```bash
git commit -m "fix(daily): reject --lookback on the enqueue path instead of ignoring it"
```

---

### Task 6: Alert when a previous run never completed

**Files:**
- Modify: `pipeline/__main__.py` (replace the Task 3 stub)
- Test: `pipeline/test_daily_enqueue.py`

**Step 1: Write the failing tests**

```python
def test_audit_alerts_on_a_stale_pending_row(tmp_path):
    store = StateStore(tmp_path / "s.db")
    store.insert_pending_the_rundown("2026-08-14")
    with patch("pipeline.alerts.send_alert") as alert:
        _audit_previous_daily_run(store, "the-rundown", "2026-08-17")
    assert alert.called
    assert "2026-08-14" in alert.call_args[0][0]
    store.close()


def test_audit_is_silent_on_a_healthy_board(tmp_path):
    store = StateStore(tmp_path / "s.db")
    store.insert_pending_the_rundown("2026-08-17")
    with patch("pipeline.alerts.send_alert") as alert:
        _audit_previous_daily_run(store, "the-rundown", "2026-08-17")
    assert not alert.called
    store.close()


def test_audit_never_raises(tmp_path):
    """The audit must never be able to break the enqueue that precedes it."""
    store = StateStore(tmp_path / "s.db")
    with patch("pipeline.__main__._stale_daily_jobs", side_effect=RuntimeError("x")):
        _audit_previous_daily_run(store, "the-rundown", "2026-08-17")  # must not raise
    store.close()
```

**Step 3: Implement**

```python
def _audit_previous_daily_run(store: StateStore, feed_slug: str, today: str) -> None:
    """Alert if a previous run of *feed_slug* was enqueued but never finished.

    The timer is now the watchdog for its own previous fire. Since the CLI only
    enqueues, a green timer unit no longer implies an episode shipped, and this
    is what closes that gap without needing a change to the Nix-managed units.

    Never raises: a monitoring failure must not break the enqueue.
    """
    try:
        stale = _stale_daily_jobs(store, feed_slug, today)
        if not stale:
            return
        from pipeline.alerts import send_alert

        label = _DAILY_FEED_LABELS.get(feed_slug, feed_slug)
        lines = [f"{label}: {len(stale)} earlier job(s) never completed."]
        for row in stale:
            lines.append(
                f"  {row['date_str']} status={row['status']} "
                f"failures={row['failure_count']} last_error={row['last_error']}"
            )
        lines.append("The consumer may be stopped or wedged. Check:")
        lines.append("  systemctl status my-podcasts-consumer")
        send_alert("\n".join(lines), severity="warning")
    except Exception as exc:  # noqa: BLE001 - monitoring must never break the job
        print(f"[daily-audit] skipped ({type(exc).__name__}: {exc})")
```

Note `send_alert` already never raises on its own; the `try` guards the query and formatting.

**Step 5: Commit**

```bash
git commit -m "feat(daily): alert when a previous daily run never completed"
```

---

### Task 7: Alert when the consumer exhausts a job's retry budget

`consumer.py:549` already detects `retry.exhausted` and only prints it. Nobody reads journald proactively.

**Files:**
- Modify: `pipeline/consumer.py` (Rundown ~549, FP Digest ~693)
- Test: `pipeline/test_consumer.py`

**This is the only task that touches `consumer.py`, so it is the one that makes the restart mandatory** — see the deploy section.

Add to both exhausted branches (note `label` is **not** in scope at
`consumer.py:549`/`:694` — define it, or the snippet raises `NameError` on the
one path that only executes when something has already gone wrong):

```python
                        from pipeline.alerts import send_alert

                        label = "The Rundown"  # "FP Digest" in the FP branch
                        send_alert(
                            f"{label} job {job['date_str']} gave up after "
                            f"{retry.failure_count} failures.\n"
                            f"Last error: {exc}",
                            severity="error",
                        )
```

Test that it fires on exhaustion and does **not** fire on an ordinary retry.

**Commit:** `feat(daily): alert when a daily job exhausts its retry budget`

---

### Task 7b: `jobs complete` — close the manual-publish trap

**This exists because the plan as first written created a new instance of the very
bug it fixes.** After Tasks 3-4 the only way to publish a daily episode with the
consumer down is `--dry-run` then `publish-script`. But `publish-script` never
touches the job row (verified: no `pending_*` or `mark_*` reference anywhere in
it). So the row stays `pending`, and the moment the consumer comes back it
executes the job and publishes a **second** episode — the exact double-publish
this plan is fixing, now reachable through the documented recovery path.

There is no way to close a job by hand today: the `jobs` group has only `list`
and `reset` (`__main__.py:168`, `:204`).

**Files:**
- Modify: `pipeline/db.py` (expose completion by feed slug), `pipeline/__main__.py`
- Test: `pipeline/test_jobs_cli.py`

**Step 1: Write the failing test**

```python
def test_jobs_complete_marks_a_pending_job_completed(tmp_path):
    db = tmp_path / "s.db"
    store = StateStore(db)
    store.insert_pending_the_rundown("2026-08-17")
    store.close()
    with patch("pipeline.__main__._default_state_db_path", return_value=db):
        result = CliRunner().invoke(
            cli, ["jobs", "complete", "--feed", "the-rundown", "--date", "2026-08-17"]
        )
    assert result.exit_code == 0, result.output
    store = StateStore(db)
    assert store.list_daily_jobs("the-rundown", "pending") == []
    assert len(store.list_daily_jobs("the-rundown", "completed")) == 1
    store.close()


def test_jobs_complete_rejects_an_unknown_date(tmp_path):
    db = tmp_path / "s.db"
    with patch("pipeline.__main__._default_state_db_path", return_value=db):
        result = CliRunner().invoke(
            cli, ["jobs", "complete", "--feed", "the-rundown", "--date", "2026-01-01"]
        )
    assert result.exit_code != 0
```

**Step 3: Implement** — mirror `jobs_reset_command` (`__main__.py:204`), resolving
the row by feed+date and calling the existing `_mark_pending_job_completed` via a
thin `StateStore.complete_daily_job(feed_slug, job_id)` that reuses
`_FEED_SLUG_TO_TABLE`. Raise `click.ClickException` when no such row exists.

**Step 5: Commit**

```bash
git commit -m "feat(jobs): add 'jobs complete' so a manual publish can close its job row

Without this, the documented consumer-down recovery (dry-run then
publish-script) leaves the row pending, and the returning consumer
publishes a second episode - reintroducing the double-publish through
the recovery path itself."
```

---

### Task 8: Documentation

**Files:** `AGENTS.md`, `pipeline/AGENTS.md`, `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`

Record:
- The CLI **enqueues only**; the consumer is the single executor. `--dry-run` still runs collection + generation inline and touches no DB.
- A green timer means "enqueued", not "published". The enqueue-time audit and the retry-exhaustion alert are what cover the difference.
- `--lookback` applies to `--dry-run` only.
- To force a run: `uv run python -m pipeline the-rundown --date YYYY-MM-DD`, then watch `journalctl -fu my-podcasts-consumer`.
- Never run a second consumer by hand; it would reintroduce exactly this race.
- **Manual publish with the consumer down:** `--dry-run`, then `publish-script`,
  then **`jobs complete`**. Skipping the last step leaves the row `pending` and
  the returning consumer publishes a duplicate.
- Re-running an already-`completed` date is not supported (and was not before);
  the command reports `status=completed` and does nothing.

**State the availability tradeoff explicitly** — it is a real regression, not a
free win. Before this change the timer's inline run was accidental redundancy:
it published even when the consumer was dead. Now a dead consumer means no
episode. We accept that because (a) `Restart=on-failure` covers the crash case,
(b) a dead consumer stops the email-driven feeds too, so it gets noticed and
fixed regardless, and (c) the concurrent "redundancy" was itself corrupting a
shared work dir. But note the detection gap honestly: the enqueue-time audit
only runs at the **next** weekday 04:30, so a consumer that dies Friday morning
yields no episode and no alert until Monday — roughly 72 hours. If that proves
unacceptable in practice, the fix is a consumer heartbeat file checked at
enqueue time; file a bead rather than pre-building it.

**Commit:** `docs: record the enqueue-only daily job model`

---

## Deploy (do NOT skip or reorder)

1. `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .` — all clean.
2. Open the PR, CI green, merge.
3. On the live tree: `cd /home/dev/projects/my-podcasts && git pull`.
4. Confirm nothing is mid-flight:
   `SELECT date_str, status FROM pending_the_rundown ORDER BY date_str DESC LIMIT 3` (and `pending_fp_digest`).
5. **`sudo systemctl restart my-podcasts-consumer`.** Mandatory. Task 7 changes `consumer.py`, and the lazy `from pipeline.__main__ import ...` at `consumer.py:323` means an unrestarted consumer would pair its old resident modules with the new `__main__.py` from disk.
6. Verify: `systemctl status my-podcasts-consumer`, then one clean loop iteration in `journalctl -u my-podcasts-consumer -n 50`.
7. Do this **with daylight before Monday 04:30 ET**, never late Sunday.

## Post-deploy remediation (separate from the fix)

Only after the fix is deployed, clean the 16 duplicated `r2_key`s (32 rows, 16 stale extras). For each duplicated key keep the **newest** row, delete the rest, and regenerate the affected feeds. Back up `state.sqlite3` first. Cleaning before the fix just restarts the clock, which is exactly what happened on 2026-07-30.

**Do not apply keep-newest blindly.** The rule came from a measurement that read
"the newest-created row matches the actual R2 object for **146 of 147** keys" —
which means there is a known mismatch. Re-run that comparison over today's 16
keys, and for any key where the newest row does not match the live R2 object,
hand-check it and keep the row that does. A blanket keep-newest would knowingly
leave one feed entry pointing at the wrong artifact.

## Verification on the first real run (Monday ~06:00 ET)

- Exactly **one** episodes row per feed for the date, and no duplicated `r2_key`.
- A funnel report in Telegram (it comes from the consumer path, which is now the only path).
- `journalctl` shows the consumer, not the CLI, doing collection and TTS.
- The timer units show `Type=oneshot` success within seconds rather than minutes.

## Out of scope (already filed)

- `my-podcasts-yq3` — re-publish when the consumer crashes between `insert_episode` and `mark_*_completed`. Survives this fix; rarer; same symptom.
- A consumer-side pidfile `flock` to stop a second manual consumer. Documented in Task 8 instead.
