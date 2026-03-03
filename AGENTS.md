# My Podcasts Agent Guide

Quick start and navigation for humans and coding agents.

## Quick Start

1. Sync dependencies:
   - `uv sync`
2. Run tests:
   - `uv run pytest`
3. Check consumer service:
   - `sudo systemctl status my-podcasts-consumer --no-pager`
4. Subscription URLs:
   - Main: `https://podcast.mohrbacher.dev/feed.xml`
   - Levine: `https://podcast.mohrbacher.dev/feeds/levine.xml`
   - Yglesias: `https://podcast.mohrbacher.dev/feeds/yglesias.xml`
   - Silver Bulletin: `https://podcast.mohrbacher.dev/feeds/silver.xml`
   - Things Happen: `https://podcast.mohrbacher.dev/feeds/things-happen.xml` (created on first episode)

## Docs TOC

- Architecture and operator context:
  - `CLAUDE.md`
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

When a Levine email with a "Things Happen" section is processed, the consumer **immediately** queues and launches an AI agent — there is no 24-hour delay.

**Flow:**
1. Email parsed → links extracted by `things_happen_extractor.py`
2. Consumer calls `things_happen_agent.py` which launches an `opencode serve` agent on port 5555
3. Agent enriches headlines using **Exa** (full-text article search) and **xAI/Grok** for analysis
4. Agent writes the briefing script to `/tmp/things-happen-<job_id>.txt`
5. Script handed off to existing TTS + publish pipeline (`ttsjoin` → R2 upload → feed update)

**New modules:**
- `pipeline/things_happen_agent.py` — agent launcher; contains `build_agent_prompt()`
- `pipeline/exa_client.py` — Exa search API wrapper
- `pipeline/xai_client.py` — xAI/Grok API wrapper

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
