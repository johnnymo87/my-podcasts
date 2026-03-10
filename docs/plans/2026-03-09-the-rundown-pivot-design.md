# The Rundown Pivot Design

**Date**: 2026-03-09
**Status**: Approved

## Overview

Rename "Things Happen" to "The Rundown" and pivot from a finance podcast triggered by Matt Levine's newsletter to a daily current-affairs digest triggered by a fixed schedule. Covers business, technology, AI, law, media, science, culture — everything except foreign policy (which goes to FP Digest).

## Motivation

The podcast has three co-equal sources (Levine, Semafor, Zvi) but its identity, trigger model, and agent prompt are all anchored in Levine's newsletter. This causes the agent to treat Levine links as primary and other sources as enrichment. Decoupling from Levine lets the podcast run even when Levine doesn't publish and gives all sources equal standing.

## Identity

- **Name**: The Rundown
- **Feed slug**: `the-rundown`
- **Category**: News
- **Voice**: Smart friend at a bar — conversational, opinionated, explains complexity without condescension. Treats the listener as someone who reads but doesn't have time to read everything. Has a point of view. Draws connections across stories. Doesn't just summarize.
- **Schedule**: Mon-Fri, 5 PM ET (same as FP Digest)
- **Feed URL**: `podcast.mohrbacher.dev/feeds/the-rundown.xml`

## Sources (co-equal)

1. **Matt Levine's "Things Happen" links** — extracted from email, cached to disk. Optional; the podcast runs without them.
2. **Semafor RSS** — Business, Technology, Media, CEO, Energy, Politics categories. Read from persistent cache.
3. **Zvi Mowshowitz** — AI roundups and essays. Read from persistent cache. Also used for AI enrichment via keyword search.

## Architecture Changes

### 1. Levine Link Cache

When a Levine email arrives, `processor.py` writes the extracted Things Happen links to `/persist/my-podcasts/levine-cache/{date}.json` instead of calling `insert_pending_things_happen()`. The JSON format matches the existing `links_json` schema: an array of `{link_text, raw_url, headline_context}` objects.

The `_maybe_queue_things_happen()` function becomes `_cache_levine_links()`.

### 2. Timer-Based Trigger

A new systemd timer + service replaces the email-triggered flow:

- **Timer**: `the-rundown.timer` — Mon-Fri at 5 PM ET, persistent, 5-min random delay
- **Service**: `the-rundown.service` — runs `uv run python -m pipeline the-rundown [--date YYYY-MM-DD] [--lookback N] [--dry-run]`

The CLI command:
1. Creates a job in the DB (for tracking/idempotency)
2. Computes adaptive lookback from `days_since_last_episode("the-rundown")`
3. Runs the collector (reads from all caches)
4. Launches the agent on opencode-serve
5. Returns — consumer handles "script done → TTS → publish"

### 3. Collector Changes

`collect_all_artifacts()` no longer takes `links_raw` as a parameter. Instead it reads Levine links from `/persist/my-podcasts/levine-cache/` within the lookback window, alongside Semafor and Zvi cache reads.

New signature:
```python
collect_all_artifacts(
    job_id, work_dir,
    levine_cache_dir, semafor_cache_dir, zvi_cache_dir,
    fp_routed_dir, lookback_days,
)
```

If no Levine links exist in the lookback window, the collector proceeds with Semafor + Zvi only.

### 4. Consumer Simplification

The consumer's Things Happen processing narrows to:
- Poll for jobs with a script file → TTS → publish
- Handle stuck-agent timeout
- No longer does collection or agent launch (the timer CLI handles that)

### 5. Agent Prompt Rewrite

The prompt is reframed:
- Identity: "The Rundown" daily digest, everything minus FP
- Sources are co-equal: Levine, Semafor, Zvi all appear in `articles/`
- Writing ethos: smart friend at a bar, broad ethos paragraph, no specific negative rules
- FP skip note retained
- Context window dedup retained

### 6. Full Rename

All references to "things-happen" / "Things Happen" become "the-rundown" / "The Rundown":

- Feed slug: `things-happen` → `the-rundown`
- DB table/methods: rename or add new table
- Systemd services: `my-podcasts-consumer` no longer creates TH jobs; new `the-rundown.service` + `the-rundown.timer`
- Work directories: `/tmp/things-happen-*` → `/tmp/the-rundown-*`
- Persistent scripts: `/persist/my-podcasts/scripts/things-happen/` → `/persist/my-podcasts/scripts/the-rundown/`
- Episode titles: `"{date} - Things Happen"` → `"{date} - The Rundown"`
- `things_happen_processor.py`: `FEED_SLUG`, `CATEGORY`, `preset_name`, `source_tag`
- AGENTS.md
- Source file names: consider renaming `things_happen_*.py` → `the_rundown_*.py` or keep for git history

### 7. Feed Transition

Clean break. New feed at `the-rundown.xml`. Old `things-happen.xml` stops updating. Only subscriber is the user.

## What Stays the Same

- Things Happen extractor logic (parsing Levine HTML)
- Semafor and Zvi cache sync (source_cache.py, zvi_cache.py)
- Human-in-the-loop agent plan approval via Telegram
- TTS pipeline (ttsjoin → R2 → feed)
- Exa enrichment
- FP routing of Levine links to FP Digest
- Stuck-session timeout
- Adaptive lookback computation
