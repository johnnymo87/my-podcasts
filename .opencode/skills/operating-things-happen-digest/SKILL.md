---
name: operating-things-happen-digest
description: Use when debugging The Rundown writer and collection path, especially opencode timeouts, reused collection artifacts, empty script output, or delayed daily episodes.
---

# Operating The Rundown Writer Path

## How it works

The Rundown daily job collects Levine, Semafor, and Zvi content into a work dir, writes `plan.json`, enriches selected stories, then generates a script through the shared `opencode-serve` daemon. On success, the consumer writes `script.txt`, `summary.txt`, and optional `covered.json`, then hands the script to TTS + publish.

Recent hardening changed the operational behavior:
- Rundown writer timeout is now 900 seconds
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

### Read the content-acquisition funnel

Every script stage posts a funnel report to the Telegram General topic and
appends a line to `/persist/my-podcasts/run-stats.jsonl`. Re-render it for any
work dir without sending:

```bash
uv run python -m pipeline run-stats --work-dir /tmp/the-rundown-<job_id>
```

```
The Rundown 2026-08-15 (job 412) - script stage - collect 4m12s, lookback 3d

IN     47 = levine 21, semafor 19, zvi 7
DEDUP  -6 (levine)
FETCH  levine 15: live 6, paywalled 8, http_error 1, fetch_error 0
PLAN   14 directives = 9 episode, 5 fp-routed
EXA    2 flagged -> 1 hit, 1 empty
WRITE  9 selected -> 8 with text (3 live, 4 cache, 1 paywalled+exa), 1 dropped, 1 +open-access
OUT    1840 words, 4 themes, 9 headlines covered

paywalled: bloomberg.com 5, ft.com 2, wsj.com 1
```

What a healthy run looks like: `FETCH` shows a nonzero `live` count, `EXA`
shows more `hit` than `error`, and `WRITE`'s "with text" is close to "selected"
with few `dropped`.

What is currently normal but *not* healthy, and is expected: most Levine
articles land in `paywalled`, because 93% of *Levine files* are headline-only
stubs. That is the measured baseline this reporting exists to track, not a new
fault.

### Reading the `+exa` buckets and the `+open-access` fragment

Open-access substitution (`my-podcasts-85c`) fires Exa on stubbed Levine
stories that were actually *selected*, retrieves other outlets' coverage of
the same story, and appends it to the stub rather than replacing it — so the
stub's true headline still anchors the writer's section even if the search
matched the wrong story.

- A `WRITE` bucket suffixed `+exa` (e.g. `paywalled+exa` above) means that
  writer input's stub got open-access text appended. The plain bucket (bare
  `paywalled`) means it did not — either Exa wasn't triggered, or it was
  triggered and came back empty/error.
- The trailing `, N +open-access` on the `WRITE` line is the same count
  spelled out; it is present only when `N > 0`, so its absence on an older or
  quiet run is normal, not a sign the feature regressed.
- `EXA n flagged` is a much smaller number than the old editor-driven trigger
  produced, **by design**: it now fires per *selected* stubbed Levine story,
  not per directive the editor guessed was paywalled. Measured across 8 real
  runs, only ~1.2 of ~4.75 selected stories per episode are Levine stubs at
  all (`[0, 1, 0, 3, 1, 2, 1, 2]` stubs/run, max 3). **`EXA 2 flagged` is a
  healthy day. `EXA 0 flagged` is legitimate on a light one** — do not
  diagnose a broken trigger from a small number; compare it against that
  day's `FETCH` stub count (`paywalled` + `http_error` + `fetch_error`)
  instead, which is the fair denominator.
- A single run's report can neither confirm nor refute whether this feature
  is working, because the daily numbers are this small. Prefer a week of
  `/persist/my-podcasts/run-stats.jsonl` history over any one report.
- Alert thresholds for any of this remain deliberately unset
  (`my-podcasts-3qs`) until that history exists — don't invent a guessed
  threshold from one day's numbers.

Beware two false alarms:

- **`FETCH ... unknown N`** means the work dir predates the tier instrumentation,
  not that fetching failed.
- **A missing Telegram report** means the reporter or pigeon failed. The episode
  is unaffected — the reporter runs after the script is on disk and swallows all
  its own errors. Check `journalctl -u my-podcasts-consumer | grep 'run stats'`,
  and note the numbers are still in `work_dir/run_stats.json` and the JSONL
  regardless of whether the message was delivered.

The report is labeled **script stage** because TTS and publish happen on a later
consumer loop iteration. It never means an episode shipped.

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
- Check logs: `journalctl -u my-podcasts-consumer --since today --no-pager | rg "Failed Rundown job|retry #|empty script|900 seconds"`
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
