# Pipeline Agent Guide

Quick start and incident-response guide for the two daily podcasts: The Rundown and Foreign Policy Digest.

## Quick Start

1. Check the consumer:
   - `sudo systemctl status my-podcasts-consumer --no-pager`
2. Check recent daily jobs:
   - `uv run python -c "import sqlite3; conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3'); conn.row_factory = sqlite3.Row; [print(dict(r)) for r in conn.execute(\"SELECT 'fp-digest' AS feed, id, date_str, status, process_after, failure_count, last_error FROM pending_fp_digest ORDER BY created_at DESC LIMIT 3\").fetchall()]; [print(dict(r)) for r in conn.execute(\"SELECT 'the-rundown' AS feed, id, date_str, status, process_after, failure_count, last_error FROM pending_the_rundown ORDER BY created_at DESC LIMIT 3\").fetchall()]"`
3. Check recent episodes:
   - `uv run python -c "import sqlite3; conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3'); conn.row_factory = sqlite3.Row; [print(dict(r)) for r in conn.execute(\"SELECT title, feed_slug, pub_date, r2_key FROM episodes WHERE feed_slug IN ('fp-digest','the-rundown') ORDER BY created_at DESC LIMIT 6\").fetchall()]"`
4. Check today’s consumer logs:
   - `journalctl -u my-podcasts-consumer --since today --no-pager`

## Relevant Skills

- Whole-pipeline monitoring: `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
- Stuck, delayed, or errored daily jobs: `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`
- Resetting errored jobs via CLI: `.opencode/skills/resetting-errored-daily-jobs/SKILL.md`
- Rundown-specific collection / writer behavior: `.opencode/skills/operating-things-happen-digest/SKILL.md`

## Current Hardening

- `pipeline/rundown_writer.py`, `pipeline/fp_writer.py` (identical hardening)
  - 900-second opencode writer timeout
  - both compose `pipeline/report_engine.py`: `fetch_report_text` for the session, then `parse_report` for extraction and every publish-boundary refusal. They call the two halves separately (rather than `run_report_prompt`) precisely so raw output can be persisted between them.
  - raw model output persisted to `work_dir/raw_writer_output.txt` before parsing; retries reuse this file and skip the model call. A parse failure **unlinks** it so the next retry regenerates instead of looping on the same broken content — note the `except` catches `RuntimeError` only, which is every refusal the engine raises today, and a test pins that.
  - refusals at the writer boundary: empty script, script below `min_chars=500`, leaked `<script>`/`<summary>`/`<covered>` markup, and (`require_tags=True`) a reply with no `<script>` tag at all. A refusal fails the job, so the consumer backs off and the retry-exhaustion alert fires — that is the intended outcome, not a degraded episode.
  - `min_chars=500` here is **re-derived, not copied** from the transcript path's 2000: this failure path is *bounded* (backoff → `errored` → alert), which is the regime 500 was originally derived for. The transcript path's unbounded redelivery is what forced 2000 there.
  - a second floor, `_validate_script_length`, still runs at the TTS boundary in `things_happen_processor.py`/`fp_processor.py`. The double floor is intended: that one also guards paths that bypass the writer entirely (`--script-file`, `publish-script`).
  - **The Rundown only:** refuses to generate when no section has any article text. FP Digest has no equivalent guard yet — that gap is bead `qd5`.
- `pipeline/things_happen_collector.py`, `pipeline/fp_collector.py`
  - successful collection writes `collection_done.json`
- `pipeline/consumer.py`
  - retries reuse prior collection when `collection_done.json` and `plan.json` exist
  - writer failures back off instead of retrying every 10 seconds
- `pipeline/db.py`
  - bounded retry backoff: 1m, 2m, 4m, 8m, then 15m cap
  - after about 12 hours of retry budget, daily jobs become `status='errored'`

## How To Read Job State

- `status='pending'`
  - job is still eligible to run when `process_after <= now`
- `status='completed'`
  - job finished successfully
- `status='errored'`
  - retry budget was exhausted; consumer will no longer pick it up automatically
- `failure_count`
  - number of consecutive failures on the current job row
- `last_error`
  - truncated most recent failure message; useful for classifying upstream vs TTS vs publish failures

## Incident Checklist

If The Rundown or FP Digest is missing, late, or stuck:

1. Confirm the consumer is running.
2. Check the pending job row for `status`, `failure_count`, and `last_error`.
3. Check recent episodes to see whether the episode actually published late.
4. Read consumer logs for writer-stage failures, TTS failures, or publish failures.
5. If the job is still `pending`, compare `process_after` to current time before assuming it is stuck.
6. If the job is `errored`, use the admin reset CLI: `.opencode/skills/resetting-errored-daily-jobs/SKILL.md`.
