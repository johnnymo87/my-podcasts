# Admin CLI For Errored Daily Jobs Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a small admin CLI to inspect and reset errored FP Digest / The Rundown jobs, with stale artifacts cleared by default, and document the operator workflow in a repo skill.

**Architecture:** Add shared DB helpers for listing and resetting daily podcast jobs by feed/status, then expose those helpers through a narrow `jobs` click command group in `pipeline/__main__.py`. Keep artifact cleanup in the CLI layer because it depends on the work-dir naming convention, not DB state. Document the supported workflow in a repo-local skill that uses the CLI commands as the operational primitive.

**Tech Stack:** Python, click, sqlite3, pytest, repo-local skill docs

---

### Task 1: Add DB helpers for errored daily jobs

**Files:**
- Modify: `pipeline/db.py`
- Test: `pipeline/test_fp_digest_db.py`
- Test: `pipeline/test_things_happen_db.py`

**Step 1: Write the failing tests**

Add tests for:

```python
jobs = store.list_daily_jobs(feed_slug="fp-digest", status="errored")
assert jobs[0]["date_str"] == "2026-03-17"

store.reset_fp_digest_job(job_id)
row = store._conn.execute(...).fetchone()
assert row["status"] == "pending"
assert row["failure_count"] == 0
assert row["last_error"] is None
```

and the matching Rundown reset test.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py -v`

**Step 3: Write minimal implementation**

Add shared helpers to list daily jobs by feed/status and reset a specific job back to pending with `process_after=now`.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py -v`

**Step 5: Commit**

```bash
git add pipeline/db.py pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py
git commit -m "feat: add daily job reset helpers"
```

### Task 2: Expose jobs list/reset in the CLI

**Files:**
- Modify: `pipeline/__main__.py`
- Create: `pipeline/test_jobs_cli.py`

**Step 1: Write the failing tests**

Add CLI tests for:

```python
runner.invoke(cli, ["jobs", "list", "--status", "errored"])
runner.invoke(cli, ["jobs", "reset", "--feed", "fp-digest", "--date", "2026-03-17"])
```

Verify reset clears artifacts by default and preserves them with `--keep-artifacts`.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_jobs_cli.py -v`

**Step 3: Write minimal implementation**

Add a `jobs` click group with `list` and `reset`. Support `--feed`, `--status`, `--date`, and `--job-id`. Default reset behavior clears `script.txt`, `summary.txt`, and `covered.json` in the job work directory.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_jobs_cli.py -v`

**Step 5: Commit**

```bash
git add pipeline/__main__.py pipeline/test_jobs_cli.py
git commit -m "feat: add admin CLI for errored daily jobs"
```

### Task 3: Document the operator workflow in a repo skill

**Files:**
- Create: `.opencode/skills/resetting-errored-daily-jobs/SKILL.md`

**Step 1: Write the failing test case (documentation baseline)**

Define the operational scenario the skill must cover:
- inspect errored daily jobs
- reset one safely
- monitor the consumer after reset

**Step 2: Write minimal skill documentation**

Document the CLI-first workflow and emphasize that `jobs reset` clears stale artifacts by default.

**Step 3: Verify the skill matches the actual CLI**

Run the documented commands locally and confirm the help text / behavior match.

**Step 4: Commit**

```bash
git add .opencode/skills/resetting-errored-daily-jobs/SKILL.md
git commit -m "docs: add skill for resetting errored daily jobs"
```

### Task 4: Final verification

**Files:**
- Inspect: `/tmp/fp-digest-*`
- Inspect: `/tmp/the-rundown-*`

**Step 1: Run targeted tests**

Run: `uv run pytest pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py pipeline/test_jobs_cli.py -v`

**Step 2: Run full suite**

Run: `uv run pytest`

**Step 3: Verify CLI help**

Run: `uv run python -m pipeline jobs --help`

**Step 4: Verify reset flow manually on a test DB or synthetic job**

Create a synthetic errored job, reset it through the CLI, and confirm DB state plus artifact cleanup match the documentation.
