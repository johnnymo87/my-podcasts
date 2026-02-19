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

## Docs TOC

- Architecture and operator context:
  - `PLAN.md` (workspace-level plan)
  - `CLAUDE.md`
- Agent skills:
  - `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
  - `.opencode/skills/monitoring-my-podcasts-pipeline/REFERENCE.md`
  - `.opencode/skills/shipping-my-podcasts-workers/SKILL.md`
  - `.opencode/skills/shipping-my-podcasts-workers/REFERENCE.md`

## Asset Locations

- Podcast artwork source files:
  - `assets/podcast/cover-general.jpg`
  - `assets/podcast/cover-levine.jpg`
  - `assets/podcast/cover-yglesias.jpg`
- Artwork served via worker URLs:
  - `https://podcast.mohrbacher.dev/cover-general.jpg`
  - `https://podcast.mohrbacher.dev/cover-levine.jpg`
  - `https://podcast.mohrbacher.dev/cover-yglesias.jpg`

## Core Paths

- Email parser: `email_processor/`
- Pipeline + feed generation: `pipeline/`
- Email ingest worker: `workers/email-ingest/`
- Podcast serving worker: `workers/podcast-serve/`
