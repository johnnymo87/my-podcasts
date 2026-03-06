---
name: operating-things-happen-digest
description: Operates the Things Happen AI agent pipeline. Use when checking pending jobs, debugging the AI agent, verifying briefing script generation, or troubleshooting the feed.
---

# Operating the Things Happen Digest

## How it works

When a Levine email is processed, the pipeline extracts "Things Happen" links and **immediately** launches an AI agent — there is no 24-hour delay. The consumer calls `things_happen_agent.py`, which creates a session on the shared `opencode-serve` daemon (port 4096) via `pipeline/opencode_client.py`. The agent enriches headlines using Exa (full-text search) and xAI/Grok, then writes a briefing script to `<work_dir>/script.txt`. The script is handed to the existing TTS pipeline: `ttsjoin` (voice: nova) → R2 upload → feed publication to `feeds/things-happen.xml`. Session IDs are tracked in the `opencode_session_id` column of `pending_things_happen`.

## Quick checks

```bash
# Service running?
sudo systemctl status my-podcasts-consumer --no-pager

# Pending jobs?
uv run python -c "
import sqlite3, json
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT id, date_str, status, process_after FROM pending_things_happen ORDER BY created_at DESC LIMIT 10').fetchall()
for r in rows: print(dict(r))
"

# Things Happen episodes?
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT title, pub_date, r2_key FROM episodes WHERE feed_slug = 'things-happen' ORDER BY created_at DESC LIMIT 5\").fetchall()
for r in rows: print(dict(r))
"

# Feed live?
curl -sI https://podcast.mohrbacher.dev/feeds/things-happen.xml | head -3
```

Expected healthy state:
- Service is `active (running)`
- Shared opencode-serve is healthy: `curl -s http://127.0.0.1:4096/global/health`
- On email arrival, agent session is created and session ID stored in `pending_things_happen.opencode_session_id`
- Pending jobs transition from `pending` to `completed` after the agent finishes
- Feed returns `200` once the first episode has been published

### Check if an agent is currently running

```bash
# Check for active sessions on the shared opencode server
curl -s http://127.0.0.1:4096/session | python3 -m json.tool

# Check session ID stored in the DB for a pending job
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT id, date_str, opencode_session_id FROM pending_things_happen WHERE status = \"pending\"').fetchall()
for r in rows: print(dict(r))
"
```

### Inspect the generated briefing script

```bash
# List recent script files
ls -lt /tmp/things-happen-*.txt | head -5

# View the latest script
cat /tmp/things-happen-<job_id>.txt
```

## Manual operations

```bash
# Force-process a pending job (trigger agent immediately)
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.execute(\"UPDATE pending_things_happen SET process_after = datetime('now') WHERE status = 'pending'\")
conn.commit()
print('All pending jobs marked as due now')
"

# Inspect links stored in a pending job
uv run python -c "
import sqlite3, json
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
row = conn.execute('SELECT links_json FROM pending_things_happen ORDER BY created_at DESC LIMIT 1').fetchone()
if row: print(json.dumps(json.loads(row['links_json']), indent=2))
"
```

## Failure modes

### Shared opencode-serve is down
- Check: `systemctl status opencode-serve.service --no-pager`
- Health: `curl -s http://127.0.0.1:4096/global/health`
- Restart: `sudo systemctl restart opencode-serve.service`
- The consumer will log errors and retry on the next poll cycle.

### Agent session fails or never starts
- Check logs: `journalctl -u my-podcasts-consumer -n 100 --no-pager | grep -i "things happen\|agent\|opencode"`
- Check opencode-serve logs: `journalctl -u opencode-serve.service -n 50 --no-pager`
- The consumer clears stale session IDs automatically on the next cycle.

### Job stays pending
- Consumer service may be down: `sudo systemctl status my-podcasts-consumer`
- Agent may have crashed mid-run. Check logs for `Failed Things Happen job`.
- Force retry by restarting: `sudo systemctl restart my-podcasts-consumer`

## Deep checks

See `REFERENCE.md` for:
- Article fetcher fallback chain debugging
- Agent prompt location and customization
- Database schema details
- New module reference
