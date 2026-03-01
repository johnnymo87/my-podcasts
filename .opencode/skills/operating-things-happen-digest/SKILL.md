---
name: operating-things-happen-digest
description: Operates the Things Happen delayed digest pipeline. Use when checking pending jobs, debugging article fetching, verifying LLM summarization, or troubleshooting the delayed digest feed.
---

# Operating the Things Happen Digest

## How it works

When a Levine email is processed, the pipeline extracts "Things Happen" links and creates a pending job with a 24-hour delay. The consumer loop checks for due jobs on every iteration. Due jobs trigger: redirect resolution, article fetching (archive.today -> live -> headline-only), LLM briefing via `opencode run` (Claude Opus 4.6), TTS via `ttsjoin` (voice: nova), R2 upload, and feed publication to `feeds/things-happen.xml`.

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
- Pending jobs transition from `pending` to `completed` after their `process_after` time
- Feed returns `200` once the first episode has been published

## Manual operations

```bash
# Force-process a due job (set process_after to now)
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

## Deep checks

See `REFERENCE.md` for:
- Article fetcher fallback chain debugging
- LLM summarizer troubleshooting
- Archive.today failure patterns
- Database schema details
