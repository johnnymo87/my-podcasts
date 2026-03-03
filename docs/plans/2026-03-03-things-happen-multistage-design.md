# Things Happen Multi-Stage Architecture

**Goal**: Refactor the Things Happen pipeline into a deterministic multi-stage architecture modeled after `perf-review-collector`, featuring an AI Editor phase, local file caching of all artifacts, and a trailing context window of prior scripts.

## Core Architectural Changes

### 1. The 5-Phase Pipeline
Instead of one monolithic `opencode` agent doing everything, the consumer will orchestrate 5 distinct phases:

**Phase 1: Base Collection (Python)**
- Resolve all Bloomberg redirect links.
- Run `fetch_all_articles()`.
- Create `/tmp/things-happen-<id>/articles/` and write one Markdown file per article.
- Create `/tmp/things-happen-<id>/context/` and symlink the 3 most recent scripts from `/persist/my-podcasts/scripts/things-happen/`.

**Phase 2: Editor AI (Gemini 2.0 Flash)**
- Use `google-genai` direct API call.
- Provide the list of headlines and article snippets.
- Ask Gemini to output a JSON array determining the research plan for each article:
  - `needs_exa`: boolean (true if paywalled and needs open-access alternatives)
  - `exa_query`: string
  - `needs_xai`: boolean (true if public sentiment/discussion is valuable)
  - `xai_query`: string
  - `is_foreign_policy`: boolean (true if relates to war, geopolitics, international relations)
  - `fp_query`: string (for RSS search if `is_foreign_policy` is true)

**Phase 3: Deep Enrichment (Python)**
- Parse the JSON from Phase 2.
- For each item, run the requested searches (`exa_client`, `xai_client`, `rss_sources`).
- Write results to `/tmp/things-happen-<id>/enrichment/{exa,xai,rss}/<slug>.md`.

**Phase 4: Writer AI (Opencode)**
- Launch `opencode serve`.
- Prompt: "Your research has been collected in `/tmp/things-happen-<id>/`. Read `articles/`, `enrichment/`, and the prior scripts in `context/`. If a story is adequately covered in the prior scripts with no new developments, skip it. Present your plan via Telegram. When approved, write `script.txt` to the root of the directory."

**Phase 5: Finalization (Python)**
- Consumer detects `script.txt`.
- Runs TTS and uploads to R2 (if not dry run).
- Copies `script.txt` to `/persist/my-podcasts/scripts/things-happen/YYYY-MM-DD.txt` to serve as context for future runs.
- Cleans up the `/tmp` directory.

### 2. Environment Updates
- The NixOS configuration for `my-podcasts-consumer-start` needs to export `GEMINI_API_KEY`.
- The Python project needs the `google-genai` dependency added.

## Tradeoffs
- **Pros**: Agent session is much faster and simpler (just reading files and writing one file); highly debuggable (all intermediate data is saved to disk); API flakiness (Exa/xAI) is isolated to Python logic rather than crashing the agent; implements a compounding-quality trailing window.
- **Cons**: Slightly more complex orchestration code in the consumer.
