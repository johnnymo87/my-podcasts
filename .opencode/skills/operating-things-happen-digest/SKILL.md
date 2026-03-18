---
name: operating-things-happen-digest
description: Use when debugging The Rundown writer and collection path, especially opencode timeouts, reused collection artifacts, empty script output, or delayed daily episodes.
---

# Operating The Rundown Writer Path

## How it works

The Rundown daily job collects Levine, Semafor, and Zvi content into a work dir, writes `plan.json`, enriches selected stories, then generates a script through the shared `opencode-serve` daemon. On success, the consumer writes `script.txt`, `summary.txt`, and optional `covered.json`, then hands the script to TTS + publish.

Recent hardening changed the operational behavior:
- Rundown writer timeout is now 300 seconds
- successful collection writes `collection_done.json`
- retries reuse prior collection when `collection_done.json` and `plan.json` exist
- empty script output is rejected at the writer boundary
- retries back off and eventually become `status='errored'` instead of retrying forever

## Quick checks

```bash
# Service running?
sudo systemctl status my-podcasts-consumer --no-pager

# Pending Rundown jobs?
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT id, date_str, status, process_after, failure_count, last_error FROM pending_the_rundown ORDER BY created_at DESC LIMIT 10').fetchall()
for r in rows: print(dict(r))
"

# Rundown episodes?
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT title, pub_date, r2_key FROM episodes WHERE feed_slug = 'the-rundown' ORDER BY created_at DESC LIMIT 5\").fetchall()
for r in rows: print(dict(r))
"

# Feed live?
curl -sI https://podcast.mohrbacher.dev/feeds/the-rundown.xml | head -3
```

Expected healthy state:
- Service is `active (running)`
- Shared opencode-serve is healthy: `curl -s http://127.0.0.1:4096/global/health`
- Pending jobs transition from `pending` to `completed` after the writer + TTS path finishes
- Feed returns `200`

### Check if the writer is currently running

```bash
# Check for active sessions on the shared opencode server
curl -s http://127.0.0.1:4096/session | python3 -m json.tool

# Check current retry state for the latest job
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT id, date_str, status, process_after, failure_count, last_error FROM pending_the_rundown ORDER BY created_at DESC LIMIT 3').fetchall()
for r in rows: print(dict(r))
"
```

### Inspect the generated work dir

```bash
# Rundown work dirs
ls -ld /tmp/the-rundown-*

# View the latest script and collection sentinel
python3 - <<'PY'
from pathlib import Path
p = Path('/tmp/the-rundown-<job_id>')
for name in ['collection_done.json', 'plan.json', 'summary.txt', 'script.txt', 'covered.json']:
    f = p / name
    print(name, 'exists' if f.exists() else 'missing')
PY
```

## Manual operations

```bash
# Force-process a pending Rundown job immediately
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.execute(\"UPDATE pending_the_rundown SET process_after = datetime('now') WHERE status = 'pending'\")
conn.commit()
print('All pending jobs marked as due now')
"
```

## Failure modes

### Shared opencode-serve is down
- Check: `systemctl status opencode-serve.service --no-pager`
- Health: `curl -s http://127.0.0.1:4096/global/health`
- Restart: `sudo systemctl restart opencode-serve.service`
- The consumer will log errors and retry on the next poll cycle.

### Writer times out or returns empty output
- Check logs: `journalctl -u my-podcasts-consumer --since today --no-pager | rg "Failed Rundown job|retry #|empty script|300 seconds"`
- Check opencode-serve logs: `journalctl -u opencode-serve.service -n 50 --no-pager`
- Retries now back off automatically and preserve collection artifacts for reuse.

### Job stays pending
- Consumer service may be down: `sudo systemctl status my-podcasts-consumer`
- Compare `process_after` to current time before assuming it is stuck.
- If `status='errored'`, the retry budget is exhausted and manual reset is required.
- Force retry by restarting: `sudo systemctl restart my-podcasts-consumer`

## Deep checks

See `REFERENCE.md` for:
- Article fetcher fallback chain debugging
- Writer prompt location and customization
- Database schema details
- New module reference

For shared FP Digest / Rundown incident handling, also use `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`.
