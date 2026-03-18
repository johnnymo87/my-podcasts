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
journalctl -u my-podcasts-consumer --since today --no-pager | rg "FP digest|Rundown|retry #|errored|empty script|300 seconds|Failed"
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

- FP Digest and The Rundown writers now wait up to 300 seconds
- successful collection writes `collection_done.json`
- retries reuse prior collection instead of rerunning expensive collection every time
- empty script output is rejected at the writer boundary
- retries back off: 1m, 2m, 4m, 8m, then 15m cap
- after about 12 hours of failures, jobs become `status='errored'`

## Manual recovery today

Until the admin reset CLI lands, manual recovery is:

1. Inspect the latest pending row and confirm whether it is still retrying or already `errored`.
2. Inspect the work dir:

```bash
python3 - <<'PY'
from pathlib import Path
for prefix in ['fp-digest', 'the-rundown']:
    for p in sorted(Path('/tmp').glob(f'{prefix}-*'))[-3:]:
        print(p)
        for name in ['collection_done.json', 'plan.json', 'script.txt', 'summary.txt', 'covered.json']:
            f = p / name
            print(' ', name, 'exists' if f.exists() else 'missing')
PY
```

3. If the writer produced bad empty artifacts, remove `script.txt`, `summary.txt`, and `covered.json` for that job so the writer reruns cleanly while collection is preserved.
4. If the job is still `pending`, you can move `process_after` to now for an immediate retry:

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.execute(\"UPDATE pending_fp_digest SET process_after = datetime('now') WHERE id = ? AND status = 'pending'\", ('<job_id>',))
conn.commit()
"
```

Replace `pending_fp_digest` with `pending_the_rundown` as needed.

5. Restart the consumer only if you need to kick the loop or after code changes:

```bash
sudo systemctl restart my-podcasts-consumer
```

## When to use other skills

- Whole-system monitoring: `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
- Rundown-specific collection and writer behavior: `.opencode/skills/operating-things-happen-digest/SKILL.md`
