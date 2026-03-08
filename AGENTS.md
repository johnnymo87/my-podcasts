# My Podcasts Agent Guide

Quick start and navigation for humans and coding agents.

## Quick Start

1. Sync dependencies:
   - `uv sync`
2. Run tests:
   - `uv run pytest`
3. Check consumer service:
   - `sudo systemctl status my-podcasts-consumer --no-pager`
4. Restart consumer (after code changes):
   - `sudo systemctl restart my-podcasts-consumer`
5. Sync source caches:
   - `uv run python -m pipeline sync-sources`
6. Subscription URLs:
   - Main: `https://podcast.mohrbacher.dev/feed.xml`
   - Levine: `https://podcast.mohrbacher.dev/feeds/levine.xml`
   - Yglesias: `https://podcast.mohrbacher.dev/feeds/yglesias.xml`
   - Silver Bulletin: `https://podcast.mohrbacher.dev/feeds/silver.xml`
   - Things Happen: `https://podcast.mohrbacher.dev/feeds/things-happen.xml` (created on first episode)
   - FP Digest: `https://podcast.mohrbacher.dev/feeds/fp-digest.xml`

## Docs TOC

- Agent skills:
  - `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
  - `.opencode/skills/monitoring-my-podcasts-pipeline/REFERENCE.md`
  - `.opencode/skills/shipping-my-podcasts-workers/SKILL.md`
  - `.opencode/skills/shipping-my-podcasts-workers/REFERENCE.md`
  - `.opencode/skills/operating-things-happen-digest/SKILL.md`
  - `.opencode/skills/operating-things-happen-digest/REFERENCE.md`

## Asset Locations

- Podcast artwork source files:
  - `assets/podcast/cover-general.jpg`
  - `assets/podcast/cover-levine.jpg`
  - `assets/podcast/cover-yglesias.jpg`
  - `assets/podcast/cover-silver.jpg`
  - `assets/podcast/cover-things-happen.jpg`
- Artwork served via worker URLs:
  - `https://podcast.mohrbacher.dev/cover-general.jpg`
  - `https://podcast.mohrbacher.dev/cover-levine.jpg`
  - `https://podcast.mohrbacher.dev/cover-yglesias.jpg`
  - `https://podcast.mohrbacher.dev/cover-silver.jpg`
  - `https://podcast.mohrbacher.dev/cover-things-happen.jpg`

## Core Paths

- Email parser: `email_processor/`
- Pipeline + feed generation: `pipeline/`
- Email ingest worker: `workers/email-ingest/`
- Podcast serving worker: `workers/podcast-serve/`
- Things Happen AI agent: `pipeline/things_happen_agent.py` (launcher), `pipeline/exa_client.py` (Exa wrapper), `pipeline/xai_client.py` (xAI/Grok wrapper)

## Things Happen Pipeline

When a Levine email with a "Things Happen" section is processed, the consumer **immediately** queues and launches an AI agent — there is no 24-hour delay. Focuses on finance, business, technology, and law.

**Sources:**
- Matt Levine's "Things Happen" links (extracted from email)
- Semafor RSS (`semafor.com/rss.xml`) — Business, Technology, Media, CEO, Energy categories + Politics (shared with FP Digest). Read from persistent cache with adaptive lookback.
- Zvi Mowshowitz / "Don't Worry About the Vase" (`thezvi.substack.com/feed`) — AI roundup sections split by topic, essays kept whole. Persistent cache at `/persist/my-podcasts/zvi-cache/` (180-day retention). Also used for AI enrichment via keyword search.

**Flow:**
1. Email parsed → links extracted by `things_happen_extractor.py`
2. `things_happen_collector.py` fetches articles, reads Semafor from cache (with adaptive lookback window), syncs Zvi cache and copies lookback-window posts, routes FP-flagged links to `/persist/my-podcasts/fp-routed-links/` for FP Digest
3. Consumer calls `things_happen_agent.py` which creates a session on the shared `opencode-serve` daemon (port 4096)
4. Agent enriches headlines using **Exa** (full-text article search), **xAI/Grok** for analysis, and **Zvi cache** keyword search for AI perspectives
5. Agent writes the briefing script to `<work_dir>/script.txt`
6. Script handed off to existing TTS + publish pipeline (`ttsjoin` → R2 upload → feed update)

**Key modules:**
- `pipeline/opencode_client.py` — shared HTTP client for the opencode-serve API
- `pipeline/things_happen_agent.py` — agent launcher; contains `build_agent_prompt()`
- `pipeline/things_happen_collector.py` — article collection, Semafor integration, Zvi integration, FP routing
- `pipeline/things_happen_editor.py` — Gemini AI for research plan (classifies `is_foreign_policy`)
- `pipeline/zvi_cache.py` — Zvi RSS fetch, roundup splitting, persistent cache, keyword search
- `pipeline/exa_client.py` — Exa search API wrapper
- `pipeline/xai_client.py` — xAI/Grok API wrapper
- `pipeline/rss_sources.py` — RSS source definitions, `SEMAFOR`, `categorize_semafor_article()`
- `pipeline/source_cache.py` — Persistent cache sync for Semafor, Antiwar RSS, and Antiwar homepage

## FP Digest Pipeline

Daily foreign policy podcast. Fully automated, no human-in-the-loop.

**Sources (all read from persistent caches with adaptive lookback):**
- antiwar.com homepage (~49 curated external links across 13 regions)
- 3 antiwar.com RSS feeds + Caitlin Johnstone feed
- Semafor RSS — Africa, Gulf, Security categories + Politics (shared with Things Happen)
- Routed FP links from Things Happen (via `/persist/my-podcasts/fp-routed-links/`)

**Flow:**
1. Systemd timer triggers daily at 6 PM EST (23:00 UTC)
2. `fp_collector.py` reads homepage articles, RSS articles, Semafor FP articles from persistent caches (with adaptive lookback window) + routed Levine FP links
4. `fp_editor.py` (Gemini Flash-Lite) triages into 3-5 themes, selects 8-12 stories
5. Exa enrichment for paywalled articles
6. `fp_writer.py` generates script via opencode-serve
7. `fp_processor.py` runs TTS + publishes to R2

**Key modules:**
- `pipeline/fp_homepage_scraper.py` — antiwar.com homepage parser
- `pipeline/fp_editor.py` — story triage and theme identification
- `pipeline/fp_collector.py` — multi-source collection orchestrator (homepage, RSS, Semafor, routed links)
- `pipeline/fp_writer.py` — script generation via opencode-serve
- `pipeline/fp_processor.py` — TTS + publish
- `pipeline/rss_sources.py` — RSS source definitions, Semafor category routing
- `pipeline/source_cache.py` — Persistent cache sync for all sources

**CLI:** `uv run python -m pipeline fp-digest [--date YYYY-MM-DD] [--lookback N] [--dry-run]`

## Content Routing

Foreign policy content is exclusively routed to FP Digest, not Things Happen:

- **Things Happen editor** classifies each link with `is_foreign_policy: bool`
- FP-flagged links are written to `/persist/my-podcasts/fp-routed-links/{date}-{job_id}.json`
- FP Digest collector reads routed files within the lookback window
- **Semafor** articles are split by category: FP categories → FP Digest, business/tech → Things Happen, Politics → both
- Routed link files are cleaned up after 7 days

## Source Caching

All external sources are cached daily to persistent storage by the `sync-sources` timer (noon ET daily). Podcasts read from caches instead of fetching live, with an adaptive lookback window based on days since the last episode (min 2, max 14 days).

**Caches:**
- Zvi: `/persist/my-podcasts/zvi-cache/` (also synced on-demand by Things Happen collector)
- Semafor: `/persist/my-podcasts/semafor-cache/`
- Antiwar RSS: `/persist/my-podcasts/antiwar-rss-cache/`
- Antiwar Homepage: `/persist/my-podcasts/antiwar-homepage-cache/`

All caches use 180-day retention (cleaned up by `_cleanup_old_work_dirs` in consumer). Files are markdown with metadata headers (URL, Published, Source, Category/Region).

**Key module:** `pipeline/source_cache.py` — `sync_semafor_cache`, `sync_antiwar_rss_cache`, `sync_antiwar_homepage_cache`

**CLI:** `uv run python -m pipeline sync-sources`

**Timer:** `sync-sources.timer` — daily at noon ET

**Adaptive lookback:** `pipeline/db.py:days_since_last_episode()` queries the latest episode date per feed. `pipeline/consumer.py:_compute_lookback()` computes `min(max(2, days_since + 1), 14)`.

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
