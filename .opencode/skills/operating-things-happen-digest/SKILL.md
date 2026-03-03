---
name: operating-things-happen-digest
description: Operates the Things Happen AI agent pipeline. Use when checking pending jobs, debugging the AI agent, verifying briefing script generation, or troubleshooting the feed.
---

# Operating the Things Happen Digest

## How it works

When a Levine email is processed, the pipeline extracts "Things Happen" links and **immediately** launches an AI agent — there is no 24-hour delay. The consumer calls `things_happen_agent.py`, which starts an `opencode serve` agent on port 5555. The agent enriches headlines using Exa (full-text search) and xAI/Grok, then writes a briefing script to `/tmp/things-happen-<job_id>.txt`. The script is handed to the existing TTS pipeline: `ttsjoin` (voice: nova) → R2 upload → feed publication to `feeds/things-happen.xml`.

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
- On email arrival, agent launches immediately and PID file appears at `/tmp/things-happen-opencode.pid`
- Pending jobs transition from `pending` to `completed` after the agent finishes
- Feed returns `200` once the first episode has been published

### Check if an agent is currently running

```bash
# Check PID file
ls /tmp/things-happen-opencode.pid

# Check if port 5555 is in use
fuser 5555/tcp

# Kill a stuck agent
fuser -k 5555/tcp
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

### Agent crashes or never starts
- Check logs: `journalctl -u my-podcasts-consumer -n 100 --no-pager | grep -i "things happen\|agent\|opencode"`
- Check if port 5555 is already occupied from a previous stuck agent: `fuser 5555/tcp`
- Kill the stuck process and restart the consumer: `fuser -k 5555/tcp && sudo systemctl restart my-podcasts-consumer`

### opencode serve won't start (port conflict)
- Another process may be holding port 5555: `fuser 5555/tcp`
- Free the port: `fuser -k 5555/tcp`
- No PID file cleanup needed — `things_happen_agent.py` manages `/tmp/things-happen-opencode.pid`

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
