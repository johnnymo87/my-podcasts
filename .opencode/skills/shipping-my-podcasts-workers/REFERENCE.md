# Worker Shipping Reference

## Contents
- DNS requirements
- Secrets and bindings
- Worker inventory

## DNS requirements

- `podcast.mohrbacher.dev` must exist as a proxied DNS record in Cloudflare.
- Route in `workers/podcast-serve/wrangler.toml` is:
  - `podcast.mohrbacher.dev/*`

## Secrets and bindings

Email ingest worker:
- secret: `GMAIL_FORWARD_TO`
- optional secret: `ALLOWED_SENDERS`
- R2 binding: `BUCKET`
- queue producer binding: `INBOX_QUEUE`

Podcast serve worker:
- R2 binding: `BUCKET`

## Worker inventory

- `workers/email-ingest/`
  - receives routed mail, forwards to Gmail, stores raw `.eml`, enqueues metadata
- `workers/podcast-serve/`
  - serves feeds, MP3 files, and artwork from R2 with range support
