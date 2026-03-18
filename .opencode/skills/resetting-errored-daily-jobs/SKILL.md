---
name: resetting-errored-daily-jobs
description: Use when a FP Digest or The Rundown job has status=errored and the consumer has stopped retrying it automatically.
---

# Resetting Errored Daily Jobs

## Overview

When a daily job exhausts its retry budget (~12 hours of failures), the consumer marks it `status='errored'` and stops picking it up. Use the `jobs` CLI to inspect and reset it back to `pending`.

`jobs reset` clears stale writer artifacts (`script.txt`, `summary.txt`, `covered.json`) by default so the writer reruns cleanly on the next consumer pickup. Collection is preserved.

## Workflow

### 1. Inspect errored jobs

```bash
# All errored jobs (both feeds)
uv run python -m pipeline jobs list

# Filter to one feed
uv run python -m pipeline jobs list --feed fp-digest
uv run python -m pipeline jobs list --feed the-rundown

# Also check pending jobs (for jobs still retrying)
uv run python -m pipeline jobs list --status pending
```

Output columns: `feed  job-id  date  status  failures=N  error=<last_error>`

### 2. Reset a job

By date (most common):

```bash
uv run python -m pipeline jobs reset --feed fp-digest --date 2026-03-17
uv run python -m pipeline jobs reset --feed the-rundown --date 2026-03-17
```

By job UUID (if you have it from step 1):

```bash
uv run python -m pipeline jobs reset --feed fp-digest --job-id <uuid>
```

`reset` removes stale writer artifacts by default. Use `--keep-artifacts` only if you want the writer to skip regeneration (e.g. you manually fixed `script.txt`).

### 3. Monitor after reset

```bash
# Watch the consumer pick it up
journalctl -u my-podcasts-consumer -f

# Check job status again after a minute
uv run python -m pipeline jobs list --status pending
uv run python -m pipeline jobs list --status completed

# Verify episode appeared
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT title, feed_slug, pub_date FROM episodes WHERE feed_slug IN ('fp-digest','the-rundown') ORDER BY created_at DESC LIMIT 4\").fetchall()
for r in rows: print(dict(r))
"
```

If the job re-errors quickly, check `last_error` in the list output — it usually identifies whether the failure is in the writer, TTS, or publish stage.

## When to restart the consumer

Only restart if the service is `inactive` or `failed`. A reset job will be picked up on the next consumer loop without a restart.

```bash
sudo systemctl status my-podcasts-consumer --no-pager
sudo systemctl restart my-podcasts-consumer  # only if needed
```

## Related skills

- Whole-system health: `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
- Full incident checklist and job state reference: `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`
