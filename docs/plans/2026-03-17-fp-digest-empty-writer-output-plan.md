# FP Digest Empty Writer Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent FP Digest from accepting empty LLM output, retry script generation instead of TTS, and give the writer enough time to complete.

**Architecture:** Keep the existing collection-reuse retry flow, but harden the writer and processor boundaries. The FP writer should wait longer, reject empty parsed scripts, and surface a clear error. The FP processor should defensively reject empty script files so bad artifacts never enter TTS.

**Tech Stack:** Python, pytest, opencode client, ttsjoin

---

### Task 1: Harden FP writer behavior

**Files:**
- Modify: `pipeline/fp_writer.py`
- Test: `pipeline/test_fp_writer.py`

**Step 1: Write the failing tests**

Add tests for:

```python
with pytest.raises(RuntimeError, match="300 seconds"):
    generate_fp_script(themes, articles_by_theme, date_str="2026-03-06")
```

```python
with pytest.raises(RuntimeError, match="empty script"):
    generate_fp_script(themes, articles_by_theme, date_str="2026-03-06")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_fp_writer.py -v`

**Step 3: Write minimal implementation**

Increase the FP wait timeout to `300` seconds and reject empty parsed script output before returning `WriterOutput`.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_fp_writer.py -v`

**Step 5: Commit**

```bash
git add pipeline/fp_writer.py pipeline/test_fp_writer.py
git commit -m "fix: reject empty FP writer output"
```

### Task 2: Defend the FP processor against empty scripts

**Files:**
- Modify: `pipeline/fp_processor.py`
- Test: `pipeline/test_fp_processor.py`

**Step 1: Write the failing test**

Add a processor test that writes an empty script file and asserts:

```python
with pytest.raises(RuntimeError, match="empty script"):
    process_fp_digest_job(job, store, r2_client, script_path=script_file)
```

and confirms no upload happened.

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_fp_processor.py::test_process_fp_digest_job_rejects_empty_script -v`

**Step 3: Write minimal implementation**

Strip the loaded script and raise before invoking `ttsjoin` when it is empty.

**Step 4: Run tests to verify it passes**

Run: `uv run pytest pipeline/test_fp_processor.py -v`

**Step 5: Commit**

```bash
git add pipeline/fp_processor.py pipeline/test_fp_processor.py
git commit -m "fix: reject empty FP scripts before TTS"
```

### Task 3: Verify end-to-end and recover today’s pending job

**Files:**
- Inspect: `/tmp/fp-digest-939c7fdc-28f6-434a-b001-d0af159c3d68/`

**Step 1: Run targeted tests**

Run: `uv run pytest pipeline/test_fp_writer.py pipeline/test_fp_processor.py pipeline/test_consumer.py -v`

**Step 2: Run broader verification**

Run: `uv run pytest`

**Step 3: Restart consumer**

Run: `sudo systemctl restart my-podcasts-consumer && sudo systemctl status my-podcasts-consumer --no-pager`

**Step 4: Clear bad artifacts for today’s FP job**

Remove the empty `script.txt` and `summary.txt` for job `939c7fdc-28f6-434a-b001-d0af159c3d68` so the pending job retries generation cleanly.

**Step 5: Verify job state**

Check consumer logs and DB until the job either completes or surfaces a new writer-stage error.
