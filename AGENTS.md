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
   - The Rundown: `https://podcast.mohrbacher.dev/feeds/the-rundown.xml` (created on first episode)
   - FP Digest: `https://podcast.mohrbacher.dev/feeds/fp-digest.xml`
   - Aaronson: `https://podcast.mohrbacher.dev/feeds/aaronson.xml`
   - ChinaTalk: `https://podcast.mohrbacher.dev/feeds/chinatalk.xml`
   - Papers (one-off arXiv reports): `https://podcast.mohrbacher.dev/feeds/papers.xml`

## Docs TOC

- Domain guides:
  - `pipeline/AGENTS.md`
- Agent skills:
  - `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
  - `.opencode/skills/monitoring-my-podcasts-pipeline/REFERENCE.md`
  - `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`
  - `.opencode/skills/resetting-errored-daily-jobs/SKILL.md`
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
  - `assets/podcast/cover-dwarkesh.jpg`
- Artwork served via worker URLs:
  - `https://podcast.mohrbacher.dev/cover-general.jpg`
  - `https://podcast.mohrbacher.dev/cover-levine.jpg`
  - `https://podcast.mohrbacher.dev/cover-yglesias.jpg`
  - `https://podcast.mohrbacher.dev/cover-silver.jpg`
  - `https://podcast.mohrbacher.dev/cover-things-happen.jpg`
  - `https://podcast.mohrbacher.dev/cover-dwarkesh.jpg`

## Core Paths

- Email parser: `email_processor/`
- Pipeline + feed generation: `pipeline/`
- Email ingest worker: `workers/email-ingest/`
- Podcast serving worker: `workers/podcast-serve/`
- The Rundown pipeline: `pipeline/rundown_writer.py` (script generator), `pipeline/exa_client.py` (Exa wrapper)
- ChinaTalk transcript report path: `pipeline/transcript_detect.py` (shared detector), `pipeline/chinatalk_writer.py`, `pipeline/chinatalk_report.py` (called from `pipeline/processor.py`)
- One-off episodes (source adapters): `pipeline/sources.py` (adapter registry + dispatch), `pipeline/document.py` (`Document` model), `pipeline/substack.py` (Substack API ingest + HTML normalization), `pipeline/arxiv.py` (arXiv paper adapter), `pipeline/report_writer.py` (style-keyed report writer)

## Where To Start

- Daily podcast operations and incident response: `pipeline/AGENTS.md`
- Whole-system health checks: `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
- Stuck or errored Rundown / FP Digest jobs: `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`
- Resetting errored jobs via CLI: `.opencode/skills/resetting-errored-daily-jobs/SKILL.md`
- Rundown-specific writer / collection behavior: `.opencode/skills/operating-things-happen-digest/SKILL.md`

## The Rundown Pipeline

Daily current-affairs digest covering business, technology, AI, law, media, science, and culture (excluding foreign policy). Fully automated, no human-in-the-loop. Triggered by systemd timer Mon-Fri at 4:30 AM ET.

**Sources (all co-equal, read from persistent caches with adaptive lookback):**
- Matt Levine's "Things Happen" links (extracted from email, cached to `/persist/my-podcasts/levine-cache/`)
- Semafor RSS (`semafor.com/rss.xml`) — articles classified by Gemini Flash-Lite at cache sync time into `fp`, `th`, `both`, or `skip` via `Routing:` header
- Zvi Mowshowitz / "Don't Worry About the Vase" (`thezvi.substack.com/feed`) — AI roundup sections split by topic, essays kept whole. Persistent cache at `/persist/my-podcasts/zvi-cache/` (180-day retention).

**Subscription:** `https://podcast.mohrbacher.dev/feeds/the-rundown.xml`

**Category:** News

**Flow:**
1. Systemd timer triggers, or CLI: `uv run python -m pipeline the-rundown [--date YYYY-MM-DD] [--lookback N] [--dry-run]`
2. `things_happen_collector.py` reads Levine links from cache, Semafor from cache, syncs Zvi cache — all within adaptive lookback window. Routes FP-flagged links to `/persist/my-podcasts/fp-routed-links/` for FP Digest.
3. `things_happen_editor.py` (Gemini Flash-Lite) triages into 3-5 themes, selects 8-12 stories, writes `plan.json`
4. Exa enrichment for paywalled articles
5. `rundown_writer.py` generates script via opencode-serve (synchronous, no agent session)
6. `things_happen_processor.py` runs TTS (`ttsjoin`) + publishes to R2 + updates feed

**Key modules:**
- `pipeline/opencode_client.py` — shared HTTP client for the opencode-serve API
- `pipeline/rundown_writer.py` — synchronous script generator via opencode-serve
- `pipeline/things_happen_collector.py` — article collection, Semafor integration, Zvi integration, FP routing
- `pipeline/things_happen_editor.py` — Gemini AI for themed research plan (story selection, priority, FP flagging)
- `pipeline/zvi_cache.py` — Zvi RSS fetch, roundup splitting, persistent cache
- `pipeline/exa_client.py` — Exa search API wrapper
- `pipeline/rss_sources.py` — RSS source definitions, `SEMAFOR`, `categorize_semafor_article()` (legacy fallback)
- `pipeline/source_cache.py` — Persistent cache sync for Semafor (with LLM routing), Antiwar RSS, and Antiwar homepage

## FP Digest Pipeline

Daily foreign policy podcast. Fully automated, no human-in-the-loop.

**Sources (all read from persistent caches with adaptive lookback):**
- antiwar.com homepage (~49 curated external links across 13 regions)
- 3 antiwar.com RSS feeds + Caitlin Johnstone feed
- Semafor RSS — articles with `Routing: fp` or `Routing: both` (classified by Gemini at cache sync time)
- Routed FP links from The Rundown (via `/persist/my-podcasts/fp-routed-links/`)

**Flow:**
1. Systemd timer triggers daily at 4:30 AM ET (08:30 UTC)
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
- `pipeline/rss_sources.py` — RSS source definitions, Semafor category routing (legacy fallback)
- `pipeline/source_cache.py` — Persistent cache sync for all sources

**CLI:** `uv run python -m pipeline fp-digest [--date YYYY-MM-DD] [--lookback N] [--dry-run]`

## Content Routing

Foreign policy content is exclusively routed to FP Digest, not The Rundown:

- **The Rundown editor** classifies each link with `is_foreign_policy: bool`
- FP-flagged links are written to `/persist/my-podcasts/fp-routed-links/{date}-{job_id}.json`
- FP Digest collector reads routed files within the lookback window
- **Semafor** articles are classified by Gemini Flash-Lite at cache sync time via a `Routing:` header (`fp`, `th`, `both`, or `skip`). Legacy cache files without `Routing:` fall back to category-based routing via `categorize_semafor_article()`
- Routed link files are cleaned up after 7 days

## Source Caching

All external sources are cached daily to persistent storage by the `sync-sources` timer (4:00 AM ET daily). Podcasts read from caches instead of fetching live, with an adaptive lookback window based on days since the last episode (min 2, max 14 days).

**Caches:**
- Zvi: `/persist/my-podcasts/zvi-cache/` (also synced on-demand by The Rundown collector)
- Semafor: `/persist/my-podcasts/semafor-cache/`
- Antiwar RSS: `/persist/my-podcasts/antiwar-rss-cache/`
- Antiwar Homepage: `/persist/my-podcasts/antiwar-homepage-cache/`

All caches use 180-day retention (cleaned up by `_cleanup_old_work_dirs` in consumer). Files are markdown with metadata headers (URL, Published, Source, Category/Region). Semafor cache files also include a `Routing:` header (`fp`, `th`, `both`, or `skip`) set by Gemini Flash-Lite at sync time.

**Key module:** `pipeline/source_cache.py` — `sync_semafor_cache`, `sync_antiwar_rss_cache`, `sync_antiwar_homepage_cache`

**CLI:** `uv run python -m pipeline sync-sources`

**Timer:** `sync-sources.timer` — daily at 4:00 AM ET

**Adaptive lookback:** `pipeline/db.py:days_since_last_episode()` queries the latest episode date per feed. `pipeline/consumer.py:_compute_lookback()` computes `min(max(2, days_since + 1), 14)`.

## Blog Polling

WordPress and other blog sources are polled via RSS for new posts. Each new post is adapted for audio (via Gemini Flash), converted to speech, and published as a standalone episode.

**Polling interval:** Every 6 hours (inside the consumer loop)

**CLI:** `uv run python -m pipeline poll-blogs [--dry-run]`

**Sources:**
- Scott Aaronson / Shtetl-Optimized: `https://scottaaronson.blog/?feed=rss2` -> feed slug `aaronson`

**Key module:** `pipeline/blog_poller.py` -- RSS fetch, AI adaptation, TTS + publish

**Source definitions:** `pipeline/blog_sources.py` -- `BlogSource` dataclass, `BLOG_SOURCES` tuple

## One-Off Episodes (Source Adapters)

Turn any supported source URL (Substack post, arXiv paper, ...) into a one-off episode via the generic `episode` command. The command resolves the URL through a source-adapter registry into a normalized `Document`, then either generates a spoken **report** (briefing, default) or a faithful **read** (full reading via Gemini adaptation). Manual/operator-run, not automated. This **replaces** the old `substack` command — Substack URLs and bare numeric Substack post ids are auto-detected and routed to the substack adapter.

**CLI:** `uv run python -m pipeline episode --url <url-or-id> [--source {arxiv,substack}] --mode {report|read} --feed-slug <slug> [--style {interview,paper}] [--title ...] [--voice nova] [--category ...] [--date YYYY-MM-DD] [--script-file PATH] [--dry-run]`

**Adapter model:** `pipeline/sources.py:resolve_document(url, source=None)` dispatches to the first adapter whose `matches(url)` returns True (or to an explicitly forced `--source`). Each adapter returns a `pipeline/document.py:Document` carrying `report_text`, `read_html`, `style`, `byline`, `default_category`, etc.
- **substack adapter** (`pipeline/substack.py`): style `interview`, **read supported**, default category `Technology`. Ingests via the Substack JSON API (numeric id, short link `.../p-<id>`, or canonical slug URL `.../p/<slug>`); paywalled/empty posts rejected. Auto-matches `substack.com`, `/p/`, `/p-`, `.dwarkesh.com`, and bare numeric ids.
- **arXiv adapter** (`pipeline/arxiv.py`): style `paper`, **report-only** (`read_html=None`; `--mode read` errors), default category `Science`. Metadata via the arXiv Atom API using the **versioned** id derived from the Atom entry; body via the `/html` LaTeXML rendering (drops references/math/image bodies and footnotes, collapses figures/tables to their captions). Auto-matches `arxiv.org` hosts and bare/`arXiv:`-prefixed modern ids.

**Report writer:** `pipeline/report_writer.py:generate_report(body, subject, style, byline)` selects a prompt by `style` (`interview` vs `paper`); `--style` overrides the source default. opencode-serve, 900s timeout, rejects empty output; mirrors `chinatalk_writer.py` / `yglesias_writer.py`.

**Publishing a reviewed script:** `--script-file PATH` publishes a pre-written script verbatim, skipping generation. Metadata (title prefix, source `<link>`, show notes) still comes from the resolved `Document`. This is the first-class version of the dry-run-then-publish workflow: review the `--dry-run` artifact, then publish that exact text with `--script-file`.

**Key modules:**
- `pipeline/sources.py` — adapter registry (`Adapter`, `ADAPTERS`) + `resolve_document` dispatch
- `pipeline/document.py` — `Document` model (`byline`, `default_category`, `style`, `report_text`, `read_html`, ...)
- `pipeline/arxiv.py` — arXiv adapter (`matches`, `parse_arxiv_id`, `resolve`)
- `pipeline/substack.py` — `resolve_post` (Substack API), `html_to_clean_text` (HTML normalization)
- `pipeline/report_writer.py` — style-keyed report writer (interview + paper, with byline)
- Read mode reuses `pipeline/blog_poller.py:adapt_for_audio` (Gemini); publishes via `pipeline/script_processor.py:publish_script` (extended with `source_url`)

## ChinaTalk Transcript Report Path

ChinaTalk newsletters are sometimes podcast transcripts rather than essays. For transcripts, the pipeline replaces the TTS body with an AI-written spoken briefing about the conversation, and prefixes the episode title with "Report: ". Essays and articles are read normally.

**Detection:** `pipeline/transcript_detect.looks_like_transcript` — a deterministic, content-only transcript-shape detector (shared with the Yglesias path). It counts line-start speaker labels and fires only when at least two distinct speakers each take five or more turns. No LLM call, so detection never depends on a remote API (the previous Gemini classifier silently returned NO during a 2026-05-26 endpoint outage, shipping an 80-minute transcript as a literal read).

**Generation:** `pipeline/chinatalk_writer.py` (opencode-serve, mirrors `rundown_writer.py`, 900-second timeout, rejects empty output).

**Wiring:** `pipeline/chinatalk_report.maybe_rewrite_chinatalk` is called from `pipeline/processor.process_email_bytes` between body cleaning and TTS. Essays (detection returns False) are read normally. For a *confirmed* transcript, a report-generation failure is logged and re-raised rather than silently degrading to a literal read — it propagates out of `process_email_bytes`, the consumer leaves the queue message unacked, and the email is reprocessed on redelivery.

## Yglesias Argument Transcript Reports

Slow Boring publishes the newsletter form of Matt Yglesias's *The Argument* podcast with Jerusalem Demsas (regular weekly episodes, plus one-off live events with guests). For paying subscribers the email body carries the full verbatim transcript (~80 minutes of TTS). Rather than read that aloud — or drop it, as the pipeline used to — these posts are rewritten into a spoken briefing, mirroring the ChinaTalk transcript report path, and published to the `yglesias` feed with a `Report: ` title prefix.

**Detection:** `pipeline/yglesias_filter.is_argument_transcript` is a deterministic, content-only transcript-shape detector. It counts line-start speaker labels and fires only when at least two distinct speakers each take five or more turns. No LLM call. This survives Substack footer/boilerplate changes (the old marker-based detector missed live-event posts entirely) and is near-impossible to trigger on a normal essay.

**Generation:** `pipeline/yglesias_writer.generate_report` (opencode-serve, mirrors `chinatalk_writer.py`, 900-second timeout, rejects empty output) with a prompt tuned for *The Argument*.

**Wiring:** `pipeline/yglesias_report.maybe_rewrite_yglesias` is called from `pipeline/processor.process_email_bytes` after body cleaning, before TTS (alongside the ChinaTalk hook; the two are mutually exclusive by `feed_slug`). It is yglesias-only and fails safe: any exception in detection or generation falls back to the standard reading, so the listener always gets an episode (a long reading rather than a silently dropped essay).

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

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
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
<!-- END BEADS INTEGRATION -->
