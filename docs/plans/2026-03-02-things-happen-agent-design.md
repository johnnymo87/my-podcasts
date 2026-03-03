# Things Happen AI Agent — Design

## Problem

The current Things Happen pipeline is a fixed chain: fetch articles → summarize via `opencode run` (single-shot) → TTS → publish. Most articles are paywalled (Bloomberg, WSJ, FT), yielding headline-only results. The summarizer works from headlines alone with no enrichment.

We want an AI agent that wakes up when a Levine email arrives, surveys the headlines, decides what enrichment each needs (Exa web search, xAI Twitter search, foreign policy cross-checks), gathers context, generates the briefing, publishes the episode, and communicates with the user via Telegram throughout.

## Architecture

### Trigger flow

```
Levine email arrives
  → email-ingest worker queues to Cloudflare Queue
  → consumer pulls message, processes email
  → _maybe_queue_things_happen() extracts links, inserts job (no delay)
  → consumer's next poll cycle finds the job via list_due_things_happen()
  → consumer launches opencode agent instead of calling process_things_happen_job()
```

The 24-hour delay is removed. Jobs become immediately due.

### Agent lifecycle

```
Consumer detects due job
  → launch_things_happen_agent(job) starts `opencode serve --port 5555`
  → writes PID to /tmp/things-happen-opencode.pid
  → waits for health check
  → creates session via POST /session
  → sends initial prompt via POST /session/:id/prompt_async
  → consumer returns to its poll loop (non-blocking)

opencode serve runs with pigeon plugin loaded
  → AI works, pigeon relays each idle message to Telegram
  → user replies on Telegram, pigeon injects reply, AI continues
  → AI completes enrichment, writes briefing script to /tmp/things-happen-<job_id>.txt
  → AI terminates: fuser -k 5555/tcp (belt) + kill from PID file (suspenders)

Consumer's next poll cycle
  → detects script file exists for a pending job
  → runs existing TTS → upload → episode insert → feed regen pipeline
  → marks job complete, cleans up script file
```

### What the AI agent does (enrichment + script only)

The agent receives the job data (links, date) in its initial prompt. Its scope is strictly the parts that need intelligence — enrichment and briefing writing. Mechanical steps (TTS, upload, feed) stay in existing pipeline code.

The agent:

1. Resolves redirect URLs (existing `resolve_redirect_url()`)
2. Fetches articles (existing `fetch_article()`)
3. Decides enrichment per-headline based on its judgment:
   - Has content? → May still benefit from Twitter context
   - Paywalled (headline only)? → Search Exa for related non-paywalled coverage
   - Foreign policy topic? → Also search antiwar.com and caitlinjohnstone.com via Exa
   - Needs Twitter discussion? → Use xAI Grok x_search
4. Executes enrichment (calls Exa and xAI APIs via Python)
5. Writes the briefing script directly (the AI IS the LLM — no opencode-in-opencode)
6. Saves the script to a known path: `/tmp/things-happen-<job_id>.txt`
7. Terminates the opencode process

### What existing pipeline code does (TTS + publish)

After the agent terminates, the consumer picks up the script file and runs the existing mechanical pipeline:

1. TTS via `ttsjoin`
2. Upload MP3 to R2
3. Insert episode in database
4. Mark job complete
5. Regenerate feed

This is a variant of `process_things_happen_job()` that accepts a pre-written script instead of calling the summarizer. The existing TTS, R2, feed code is proven and should not be reimplemented by the AI.

### Pigeon integration

No pigeon changes needed. The existing flow handles everything:

- **session.created** → plugin registers with daemon, direct channel starts
- **session.idle** → plugin sends last assistant message to Telegram as Stop notification
- **User replies** → Telegram webhook → worker → WebSocket → daemon → direct channel → `promptAsync()` → AI continues
- **question.asked** → plugin sends question with inline keyboard buttons to Telegram
- **User taps button or replies** → answer injected back into session

The AI uses the `question` tool at decision points to ask the user for guidance (e.g., "Here's my enrichment plan for 14 headlines. Approve?").

### Termination

Two mechanisms (belt and suspenders):

1. **Port kill**: `fuser -k 5555/tcp` — kills the specific opencode serve process by port
2. **PID file kill**: `kill $(cat /tmp/things-happen-opencode.pid)` — kills by stored PID

Both are safe because this is a dedicated opencode instance on a unique port, isolated from any interactive sessions. The AI executes both as its final action. Either succeeding is sufficient; both together handle edge cases.

### Guard rails

- **Already running check**: launcher checks PID file + port before starting a second instance
- **Script file as handoff signal**: consumer only runs TTS+publish when the script file exists, confirming the agent finished its work
- **Stuck process timeout**: consumer could check if PID file age > N hours and kill a stuck agent (future enhancement)
- **Graceful degradation**: if Exa or xAI errors, the AI proceeds with what it has
- **Missing API keys**: if EXA_API_KEY or XAI_API_KEY is unset, the AI skips that enrichment source
- **Agent failure**: if the agent crashes without writing a script, the job stays pending. The consumer will attempt to relaunch on the next cycle (with a backoff to avoid tight loops).

## Changes required

### Remove 24-hour delay
- `db.py`: change `delay_hours=24` default to `delay_hours=0`
- Tests: update accordingly

### New module: `pipeline/things_happen_agent.py`
- `launch_things_happen_agent(job, store)`: starts opencode serve, creates session, sends prompt
- `is_agent_running()`: checks PID file + port
- `kill_agent()`: cleanup helper

### Consumer changes
- When a due job has no script file yet and no agent running: call `launch_things_happen_agent(job)`
- When a due job has a script file at the expected path: run TTS + publish pipeline with that script, then mark complete
- Refactor `process_things_happen_job()` to accept an optional pre-written script, skipping the fetch/summarize steps when provided

### New module: `pipeline/exa_client.py`
- Thin wrapper around `exa-py` SDK
- `search_related(headline, domains=None)` → returns list of (title, url, text)
- Used by the AI agent via Python one-liners

### New module: `pipeline/xai_client.py`
- Thin wrapper around `openai` SDK pointing at `api.x.ai/v1`
- `search_twitter(headline, date_range)` → returns summary + citation URLs
- Used by the AI agent via Python one-liners

### New dependency
- `exa-py` added to `pyproject.toml`

### NixOS/secrets
- EXA_API_KEY needs to be obtained, added to SOPS, wired in NixOS config
- XAI_API_KEY already deployed

### Initial prompt (embedded in launcher)
Contains:
- Job data (links with headlines and resolved URLs)
- Enrichment instructions and available tools
- Pipeline completion steps (TTS, upload, feed)
- Termination instructions (port + PID file)
- Pigeon communication guidance ("use question tool for decisions, your messages relay to Telegram on idle")

### Existing code changes
- `pipeline/summarizer.py` — no longer called for Things Happen (AI writes the briefing directly), but kept intact as it may be useful for other feeds or as fallback
- `pipeline/things_happen_processor.py` — refactored to accept a pre-written script, skipping fetch+summarize when one is provided. TTS, upload, episode insert, and feed regen logic stays exactly as-is.
