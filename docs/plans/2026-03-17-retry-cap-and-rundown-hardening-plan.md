# Retry Cap And Rundown Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop daily podcast jobs after roughly 12 hours of failed retries and apply the same empty-output hardening to The Rundown that FP Digest already has.

**Architecture:** Extend the shared pending-job retry logic in `pipeline/db.py` so failures continue to use `process_after` backoff until a total retry budget is exhausted, then mark the job `errored` and stop scheduling it. Keep the consumer thin: it delegates failure scheduling to DB helpers and only logs whether a retry was scheduled or exhausted. Separately, mirror FP writer validation in `pipeline/rundown_writer.py` so empty LLM output fails at the writer boundary instead of leaking downstream.

**Tech Stack:** Python, sqlite3, pytest, systemd consumer loop

---

### Task 1: Add retry exhaustion for daily podcast jobs

**Files:**
- Modify: `pipeline/db.py`
- Test: `pipeline/test_fp_digest_db.py`
- Test: `pipeline/test_things_happen_db.py`
- Test: `pipeline/test_consumer.py`

**Step 1: Write the failing tests**

Add tests that verify:

```python
for _ in range(MAX_RETRY_FAILURES):
    result = store.mark_fp_digest_failed(job_id, "upstream error")

assert result.exhausted is True
assert result.status == "errored"
assert store.list_due_fp_digest() == []
```

and the same for `the-rundown`, plus a consumer test asserting exhausted jobs are logged and no longer due.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py pipeline/test_consumer.py -v`

**Step 3: Write minimal implementation**

Add a shared retry-limit constant representing about 12 hours at the current backoff cadence. Return structured retry state from the DB helper (`failure_count`, `process_after`, `status`, `exhausted`) so the consumer can log clearly without re-deriving state.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py pipeline/test_consumer.py -v`

**Step 5: Commit**

```bash
git add pipeline/db.py pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py pipeline/test_consumer.py
git commit -m "feat: stop exhausted daily podcast retries"
```

### Task 2: Apply empty-output hardening to The Rundown

**Files:**
- Modify: `pipeline/rundown_writer.py`
- Test: `pipeline/test_rundown_writer.py`

**Step 1: Write the failing test**

Add a Rundown writer test that returns only whitespace inside `<script>` and asserts:

```python
with pytest.raises(RuntimeError, match="empty script"):
    generate_rundown_script(...)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_rundown_writer.py::test_generate_rundown_script_rejects_empty_output -v`

**Step 3: Write minimal implementation**

After parsing the assistant response, reject empty script output in `pipeline/rundown_writer.py` before returning `WriterOutput`.

**Step 4: Run tests to verify it passes**

Run: `uv run pytest pipeline/test_rundown_writer.py -v`

**Step 5: Commit**

```bash
git add pipeline/rundown_writer.py pipeline/test_rundown_writer.py
git commit -m "fix: reject empty Rundown writer output"
```

### Task 3: Verify runtime behavior and wrap up

**Files:**
- Modify: `docs/plans/2026-03-17-retry-cap-and-rundown-hardening-plan.md`

**Step 1: Run targeted tests**

Run: `uv run pytest pipeline/test_fp_digest_db.py pipeline/test_things_happen_db.py pipeline/test_consumer.py pipeline/test_rundown_writer.py -v`

**Step 2: Run full suite**

Run: `uv run pytest`

**Step 3: Restart consumer**

Run: `sudo systemctl restart my-podcasts-consumer && sudo systemctl status my-podcasts-consumer --no-pager`

**Step 4: Verify live state**

Check pending rows and recent logs to confirm healthy jobs still complete and exhausted jobs would not remain due.
