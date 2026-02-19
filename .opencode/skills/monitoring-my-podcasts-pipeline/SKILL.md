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
```

Expected healthy state:
- service is `active (running)`
- queue `message_backlog_count` is `0` or draining
- feed endpoint returns `200`

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
