---
name: monitoring-my-podcasts-pipeline
description: Monitors and validates the my-podcasts production pipeline. Use when checking service health, queue backlog, feed endpoints, R2 artifacts, or end-to-end delivery status.
---

# Monitoring My Podcasts Pipeline

## Quick checks

Run these first:

```bash
sudo systemctl status my-podcasts-consumer --no-pager
curl -sS -X POST -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" "https://api.cloudflare.com/client/v4/accounts/3b6b247e124787ccf95772b6432fefe4/queues/fb2d616c57034fed8e6505a4ccd315b9/messages/pull" --data '{"batch_size":5,"visibility_timeout":30}'
curl -I https://podcast.mohrbacher.dev/feed.xml
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
for table, feed in [('pending_fp_digest', 'fp-digest'), ('pending_the_rundown', 'the-rundown')]:
    rows = conn.execute(f'SELECT id, date_str, status, process_after, failure_count, last_error FROM {table} ORDER BY created_at DESC LIMIT 3').fetchall()
    print(feed)
    for r in rows: print(dict(r))
"
```

Expected healthy state:
- service is `active (running)`
- queue `message_backlog_count` is `0` or draining
- feed endpoint returns `200`
- daily jobs are usually `completed` shortly after their timer window; `pending` is normal if `process_after` is still in the future

## Daily Podcast State

- `failure_count > 0` means the current job has hit consecutive failures
- `last_error` is the current best clue for whether the failure is in writer generation, TTS, or publishing
- `status='errored'` means the job exhausted its retry budget and will not be picked up again automatically
- retry cadence is now bounded backoff: 1m, 2m, 4m, 8m, then 15m cap
- retry exhaustion happens after about 12 hours of failed attempts

## Streaming checks

```bash
curl -I https://podcast.mohrbacher.dev/episodes/<path>.mp3
curl -H "Range: bytes=0-1023" -o /dev/null -s -w "%{http_code}" https://podcast.mohrbacher.dev/episodes/<path>.mp3
```

Expected:
- `Accept-Ranges: bytes` on HEAD
- `206` for the range request

## Deep checks

See `REFERENCE.md` for:
- SQL checks for `/persist/my-podcasts/state.sqlite3`
- R2 object verification commands
- known failure signatures and fixes

For stuck or errored FP Digest / The Rundown jobs, also use `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`.
