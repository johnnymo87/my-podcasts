# Things Happen Digest Reference

## Contents
- Pipeline stages
- New modules (agent, Exa, xAI)
- Database schema
- Article fetcher fallback chain
- Agent prompt details
- Common failures

## Pipeline stages

1. **Extraction** (`pipeline/things_happen_extractor.py`): Parses `<h2>Things happen</h2>` from Levine email HTML, extracts `<a>` tags from the following `<p>`. Returns `ThingsHappenLink(link_text, raw_url, headline_context)`.

2. **Job queuing** (`pipeline/processor.py:_maybe_queue_things_happen`): Called during Levine email processing. Serializes links to JSON, inserts into `pending_things_happen` for **immediate** processing (no 24-hour delay). Wrapped in try/except so failures never block the main pipeline.

3. **Redirect resolution** (`pipeline/things_happen_extractor.py:resolve_redirect_url`): Follows Bloomberg's `links.message.bloomberg.com/s/c/...` redirects via `requests.head(allow_redirects=True)`. Returns original URL on failure.

4. **AI agent launch** (`pipeline/things_happen_agent.py`): Consumer calls the agent launcher, which starts an `opencode serve` process on **port 5555** and writes a PID file to `/tmp/things-happen-opencode.pid`. The agent:
   - Enriches headlines via **Exa** (`pipeline/exa_client.py`) — full-text article search
   - Analyzes content via **xAI/Grok** (`pipeline/xai_client.py`)
   - Writes the finished briefing script to `/tmp/things-happen-<job_id>.txt`
   - Shuts down and removes the PID file when done

5. **TTS + publish** (`pipeline/things_happen_processor.py`): Reads script from `/tmp/things-happen-<job_id>.txt`, runs `ttsjoin` (model: tts-1-hd, voice: nova), uploads MP3 to `episodes/things-happen/<date>-things-happen.mp3`, inserts episode, regenerates feeds.

## New modules

| Module | Role |
|--------|------|
| `pipeline/things_happen_agent.py` | Agent launcher; contains `build_agent_prompt()` for the agent system prompt |
| `pipeline/exa_client.py` | Exa search API wrapper used by the agent for full-text article retrieval |
| `pipeline/xai_client.py` | xAI/Grok API wrapper used by the agent for headline analysis |

**Agent prompt:** Edit `things_happen_agent.py:build_agent_prompt()` to change what the agent is asked to do (tone, structure, sources, etc.).

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
| `live` | Direct URL | Publicly available portion | Paywall (< 200 chars extracted = rejected) |
| `headline_only` | N/A | Just the headline | Always succeeds |

Each `FetchedArticle` carries a `source_tier` and `source_label` so the LLM can communicate transparency to the listener.

`fetch_all_articles()` adds a 3-second delay between requests to be polite.

## Agent details

- Launched via: `things_happen_agent.py` → `opencode serve` on port 5555
- PID file: `/tmp/things-happen-opencode.pid`
- Script output: `/tmp/things-happen-<job_id>.txt`
- Agent uses Exa for article retrieval and xAI/Grok for analysis
- Prompt template (`build_agent_prompt()`) instructs the agent to:
  - Be conversational and TTS-friendly
  - State source quality before each story
  - Flag 2-3 biggest stories early
  - No markdown, no "delve"

## Common failures

### opencode serve fails or agent crashes
- Check that `opencode` is on the PATH for the service user.
- Check logs: `journalctl -u my-podcasts-consumer -n 100 --no-pager | grep -i "opencode\|things happen\|agent"`
- If port 5555 is already in use: `fuser -k 5555/tcp` then restart the consumer.
- PID file may be stale after a crash: check `/tmp/things-happen-opencode.pid` and kill if needed.

### Job stays pending
- Consumer service may be down: `sudo systemctl status my-podcasts-consumer`
- Job processing may have failed silently. Check logs for `Failed Things Happen job`.
- Force retry by restarting: `sudo systemctl restart my-podcasts-consumer`

### No Things Happen section found in a Levine email
- Some Levine emails (weekend editions, special editions) may not have a "Things Happen" section. The extractor returns an empty list and no job is queued. This is expected.

### Feed returns 404
- `feeds/things-happen.xml` is created on first episode publication. Before any episode is processed, the feed won't exist. This is expected.
