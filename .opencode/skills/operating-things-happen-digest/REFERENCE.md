# Things Happen Digest Reference

## Contents
- Pipeline stages
- Database schema
- Article fetcher fallback chain
- LLM summarizer details
- Common failures

## Pipeline stages

1. **Extraction** (`pipeline/things_happen_extractor.py`): Parses `<h2>Things happen</h2>` from Levine email HTML, extracts `<a>` tags from the following `<p>`. Returns `ThingsHappenLink(link_text, raw_url, headline_context)`.

2. **Job queuing** (`pipeline/processor.py:_maybe_queue_things_happen`): Called during Levine email processing. Serializes links to JSON, inserts into `pending_things_happen` with 24-hour delay. Wrapped in try/except so failures never block the main pipeline.

3. **Redirect resolution** (`pipeline/things_happen_extractor.py:resolve_redirect_url`): Follows Bloomberg's `links.message.bloomberg.com/s/c/...` redirects via `requests.head(allow_redirects=True)`. Returns original URL on failure.

4. **Article fetching** (`pipeline/article_fetcher.py`): Three-tier fallback:
   - `archive`: `archive.today/newest/<url>` — full archived article
   - `live`: Direct URL fetch — publicly available portion (requires 200+ chars)
   - `headline_only`: Just the headline text

5. **LLM summarization** (`pipeline/summarizer.py`): Builds a prompt with source-quality labels, writes to temp file, invokes `opencode run --file <path> <instruction>`. Strips header lines from output.

6. **TTS + publish** (`pipeline/things_happen_processor.py`): Runs `ttsjoin` (model: tts-1-hd, voice: nova), uploads MP3 to `episodes/things-happen/<date>-things-happen.mp3`, inserts episode, regenerates feeds.

## Database schema

```sql
CREATE TABLE IF NOT EXISTS pending_things_happen (
    id TEXT PRIMARY KEY,
    email_r2_key TEXT NOT NULL,
    date_str TEXT NOT NULL,
    links_json TEXT NOT NULL,
    process_after TEXT NOT NULL,    -- ISO 8601, UTC
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' or 'completed'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Key queries:
- Due jobs: `SELECT * FROM pending_things_happen WHERE status = 'pending' AND process_after <= datetime('now')`
- Job history: `SELECT id, date_str, status, process_after FROM pending_things_happen ORDER BY created_at DESC`

## Article fetcher fallback chain

The `fetch_article()` function tries sources in order:

| Tier | Source | What you get | Typical issues |
|------|--------|-------------|----------------|
| `archive` | `archive.today/newest/<url>` | Full article text | 429 rate limiting, no archived version |
| `live` | Direct URL | Publicly available portion | Paywall (< 200 chars extracted = rejected) |
| `headline_only` | N/A | Just the headline | Always succeeds |

Each `FetchedArticle` carries a `source_tier` and `source_label` so the LLM can communicate transparency to the listener.

`fetch_all_articles()` adds a 3-second delay between requests to be polite.

## LLM summarizer details

- Invoked via: `opencode run --file <prompt_file> <instruction>`
- Model: Claude Opus 4.6 (opencode default)
- Timeout: 120 seconds
- Output parsing: Strips lines starting with `>` or blank lines at the top (opencode header), returns the rest
- Prompt template instructs the LLM to:
  - Be conversational and TTS-friendly
  - State source quality before each story
  - Flag 2-3 biggest stories early
  - No markdown, no "delve"

## Common failures

### archive.today returns 429 for all requests
- Expected behavior in production. The fetcher gracefully falls back to `live` or `headline_only`.
- Future improvement: headless browser or delayed retries.
- Check: `curl -I "https://archive.today/newest/https://www.bloomberg.com/example"` — if 429, archive.today is blocking automated requests.

### opencode run fails or times out
- Check that `opencode` is on the PATH for the service user.
- Check logs: `journalctl -u my-podcasts-consumer -n 100 --no-pager | grep -i "opencode\|things happen"`
- The 120-second timeout may be too short for very long prompts. Adjustable in `pipeline/summarizer.py`.

### Job stays pending past its delay
- Consumer service may be down: `sudo systemctl status my-podcasts-consumer`
- Job processing may have failed silently. Check logs for `Failed Things Happen job`.
- Force retry by restarting: `sudo systemctl restart my-podcasts-consumer`

### No Things Happen section found in a Levine email
- Some Levine emails (weekend editions, special editions) may not have a "Things Happen" section. The extractor returns an empty list and no job is queued. This is expected.

### Feed returns 404
- `feeds/things-happen.xml` is created on first episode publication. Before any episode is processed, the feed won't exist. This is expected.
