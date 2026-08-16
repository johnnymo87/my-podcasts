---
name: operating-daily-podcast-jobs
description: Use when FP Digest or The Rundown is stuck, missing, delayed, retrying, or marked errored, and you need to inspect current job state, interpret failure counters, or recover manually.
---

# Operating Daily Podcast Jobs

## Quick checks

Run these first:

```bash
sudo systemctl status my-podcasts-consumer --no-pager
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
for table, feed in [('pending_fp_digest', 'fp-digest'), ('pending_the_rundown', 'the-rundown')]:
    rows = conn.execute(f'SELECT id, date_str, status, process_after, failure_count, last_error FROM {table} ORDER BY created_at DESC LIMIT 5').fetchall()
    print(feed)
    for r in rows: print(dict(r))
"
journalctl -u my-podcasts-consumer --since today --no-pager | rg "FP digest|Rundown|retry #|errored|empty script|900 seconds|Failed"
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT title, feed_slug, pub_date, r2_key FROM episodes WHERE feed_slug IN ('fp-digest','the-rundown') ORDER BY created_at DESC LIMIT 6\").fetchall()
for r in rows: print(dict(r))
"
```

## How to read the state

- `pending` with future `process_after`
  - normal backoff; the consumer is waiting for the next retry window
- `pending` with past `process_after`
  - should run on the next consumer loop unless the service is down or another failure occurs
- `completed`
  - job finished; check episodes before assuming the feed is missing
- `errored`
  - retry budget was exhausted after about 12 hours; the consumer will not retry automatically
- `failure_count`
  - consecutive failures on the current job row
- `last_error`
  - most recent failure message; usually enough to classify writer timeout vs empty script vs TTS vs publish failure

## Current hardening behavior

- FP Digest and The Rundown writers now wait up to 900 seconds
- successful collection writes `collection_done.json`
- retries reuse prior collection instead of rerunning expensive collection every time
- empty script output is rejected at the writer boundary
- retries back off: 1m, 2m, 4m, 8m, then 15m cap
- after about 12 hours of failures, jobs become `status='errored'`

## Resetting an errored job

Use the `jobs` CLI — see `.opencode/skills/resetting-errored-daily-jobs/SKILL.md` for the full workflow.

Quick reference:

```bash
# List errored jobs
uv run python -m pipeline jobs list

# Reset by date (clears stale writer artifacts by default)
uv run python -m pipeline jobs reset --feed fp-digest --date 2026-03-17
uv run python -m pipeline jobs reset --feed the-rundown --date 2026-03-17
```

Restart the consumer only if the service is `inactive` or `failed`; a reset job is picked up on the next loop automatically.

## Forcing a run

The daily CLI is **enqueue-only** — it inserts a job row and exits in under a
second. The consumer does the work.

```bash
uv run python -m pipeline the-rundown --date 2026-08-17
journalctl -fu my-podcasts-consumer     # this is where the run is visible
```

If it prints `status=errored`, the consumer will NOT run it — `jobs reset` first.
If it prints `status=completed`, the episode already published; re-running a
completed date is not supported.

`--lookback` is a hard error unless paired with `--dry-run`; the consumer
computes the window itself.

## Publishing by hand when the consumer is down

```bash
uv run python -m pipeline the-rundown --date 2026-08-17 --dry-run   # writes a script
uv run python -m pipeline publish-script --script-file ... --feed-slug the-rundown ...
uv run python -m pipeline jobs complete --feed the-rundown --date 2026-08-17
```

**The third command is not optional.** `publish-script` never touches the job
row, so without it the row stays `pending` and the consumer publishes a second
episode the moment it comes back — recreating the double-publish bug
(`my-podcasts-78b`) through the recovery procedure itself.

## Why a green timer no longer means an episode shipped

The timer unit now completes in seconds because it only enqueues. Two alerts
cover the difference: an enqueue-time audit (each daily run checks whether an
*earlier* run is still `pending`/`errored` and alerts if so) and a
retry-exhaustion alert from the consumer. A consumer that dies mid-week is
therefore caught at the next weekday 04:30, not immediately.

## When to use other skills

- Whole-system monitoring: `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
- Reset errored jobs step-by-step: `.opencode/skills/resetting-errored-daily-jobs/SKILL.md`
- Rundown-specific collection and writer behavior: `.opencode/skills/operating-things-happen-digest/SKILL.md`
