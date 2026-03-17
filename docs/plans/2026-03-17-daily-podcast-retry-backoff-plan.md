# Daily Podcast Retry Backoff Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add bounded retry backoff for both FP Digest and The Rundown so upstream writer failures stop re-running every 10 seconds.

**Architecture:** Reuse the existing `process_after` scheduling field on pending job rows. Add small DB helpers that record failure state (`failure_count`, `last_error`) and move `process_after` forward with bounded exponential backoff; reset failure state on success. The consumer keeps collection reuse intact and simply schedules the next retry on exception.

**Tech Stack:** Python, sqlite3, pytest, systemd consumer loop

---

### Task 1: Add retry scheduling to pending job storage

**Files:**
- Modify: `pipeline/db.py`
- Test: `pipeline/test_fp_digest_db.py`
- Test: `pipeline/test_things_happen_db.py`

**Step 1: Write the failing tests**

Add tests that verify failed FP and Rundown jobs:

```python
store.mark_fp_digest_failed(job_id, "upstream error")
due = store.list_due_fp_digest()
assert due == []
row = store._conn.execute("SELECT failure_count, last_error, process_after FROM pending_fp_digest WHERE id = ?", (job_id,)).fetchone()
assert row["failure_count"] == 1
assert row["last_error"] == "upstream error"
```

and a second failure increases the retry delay.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py -v`

**Step 3: Write minimal implementation**

Add schema migrations for `failure_count` and `last_error` on both pending tables, plus helpers to mark failed and reset failure state on completion. Use bounded backoff: 1m, 2m, 4m, 8m, 15m cap.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py -v`

**Step 5: Commit**

```bash
git add pipeline/db.py pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py
git commit -m "feat: add retry backoff state for daily podcast jobs"
```

### Task 2: Use retry scheduling in the consumer

**Files:**
- Modify: `pipeline/consumer.py`
- Test: `pipeline/test_consumer.py`

**Step 1: Write the failing tests**

Add consumer-level tests that force an FP writer failure and a Rundown writer failure, then assert the job is no longer immediately due and its failure state is incremented.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_consumer.py -v`

**Step 3: Write minimal implementation**

In each exception path, call the new DB failure helper instead of leaving the row instantly due. Include failure count and next retry timing in the log line.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_consumer.py -v`

**Step 5: Commit**

```bash
git add pipeline/consumer.py pipeline/test_consumer.py
git commit -m "feat: throttle daily podcast retries after failures"
```

### Task 3: Verify runtime behavior

**Files:**
- Inspect: `/persist/my-podcasts/state.sqlite3`

**Step 1: Run targeted tests**

Run: `uv run pytest pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py pipeline/test_consumer.py -v`

**Step 2: Run full suite**

Run: `uv run pytest`

**Step 3: Restart consumer**

Run: `sudo systemctl restart my-podcasts-consumer && sudo systemctl status my-podcasts-consumer --no-pager`

**Step 4: Verify live retry scheduling**

Check the latest pending FP row and logs to confirm `process_after` moves forward and retries are no longer every 10 seconds.
