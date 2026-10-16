---
name: shipping-my-podcasts-workers
description: Builds and deploys my-podcasts Cloudflare workers and validates routes. Use when shipping email-ingest or podcast-serve worker updates.
---

# Shipping My Podcasts Workers

## Deploy email ingest worker

```bash
npm --prefix workers/email-ingest run typecheck
npm --prefix workers/email-ingest run deploy
```

## Deploy podcast serving worker

```bash
npm --prefix workers/podcast-serve run typecheck
npm --prefix workers/podcast-serve run deploy
```

## Post-deploy checks

```bash
curl -I https://podcast.mohrbacher.dev/feed.xml
curl -I https://podcast.mohrbacher.dev/episodes/<path>.mp3
curl -H "Range: bytes=0-1023" -o /dev/null -s -w "%{http_code}" https://podcast.mohrbacher.dev/episodes/<path>.mp3
```

Expected:
- feed returns `200`
- episode returns `200` with `Accept-Ranges: bytes`
- range request returns `206`

## Related references

- Routing and DNS steps: `REFERENCE.md`
