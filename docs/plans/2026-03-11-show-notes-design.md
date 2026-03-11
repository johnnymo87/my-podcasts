# Show Notes with Article Links — Design

**Date:** 2026-03-11
**Scope:** The Rundown + FP Digest feeds only

## Problem

Episode show notes are empty for The Rundown and FP Digest. The pipeline has rich article URL and theme data available at publish time (in collector article files, plan.json, enrichment files) but none of it flows into the episode record or RSS feed. Listeners who want to read the articles discussed in an episode have no way to find them.

## Approach

Store structured data (summary + article list) in the database at publish time. Render HTML show notes at feed generation time. This allows format changes without regenerating episodes.

## Database Schema Changes

Two new nullable columns on the `episodes` table:

- `summary TEXT` — 2-3 sentence LLM-generated episode summary
- `articles_json TEXT` — JSON array: `[{"title": "...", "url": "...", "theme": "..."}]`

Both nullable so existing episodes and non-digest feeds are unaffected. Added via `ALTER TABLE ADD COLUMN` (no migration framework needed).

Update the `Episode` dataclass with:
- `summary: str | None = None`
- `articles_json: str | None = None`

Defaults ensure all existing code that constructs Episodes is unaffected.

## Writer Changes

Modify `rundown_writer.py` and `fp_writer.py`:

- Add to the prompt: instruct the LLM to write a 2-3 sentence summary wrapped in `<summary>...</summary>` tags before the script
- Parse the `<summary>` block from the response, return both pieces
- Change return type from `str` to `WriterOutput(script: str, summary: str)` (dataclass or namedtuple)
- Fallback: if summary parsing fails (tags missing, malformed), use empty string — pipeline never fails over show notes

XML tags chosen over JSON output because the script text can contain characters requiring JSON escaping, and Claude handles XML-delimited output reliably.

## Article URL Extraction (new module: `pipeline/show_notes.py`)

Shared utility used by both processors. At publish time:

1. Read `plan.json` — get themes list and directives with `include_in_episode: true`
2. Scan article files in work directory — parse `# Headline` (first line) and `URL: ...` metadata line from each
3. Match directives to article files by headline (case-insensitive, with slugified filename fallback)
4. Build `[{"title": "...", "url": "...", "theme": "..."}]` ordered by theme order from plan, then priority within theme
5. Articles with no matching file or no URL line get `url: null` — include title without link rather than silently dropping

## Processor Changes

In `things_happen_processor.py` and `fp_processor.py`, before `insert_episode`:

1. Call the show_notes extractor with the work directory path
2. Receive the structured article list as JSON string
3. Get the summary from the `WriterOutput`
4. Pass `summary` and `articles_json` to `insert_episode`

## Feed Generation Changes

Update `pipeline/feed.py`:

- Register `content` XML namespace (`xmlns:content`)
- When episode has `summary` and/or `articles_json`:
  - `<description>`: plain-text summary (no HTML)
  - `<content:encoded>`: rich HTML with summary paragraph + themed article links
- HTML format:
  ```html
  <p>Summary text here.</p>
  <h3>Theme Name</h3>
  <ul>
    <li><a href="https://...">Article Title</a></li>
    <li><a href="https://...">Another Article</a></li>
  </ul>
  <h3>Another Theme</h3>
  <ul>
    <li>Article With No URL</li>
  </ul>
  ```
- Episodes without these fields keep current behavior unchanged

## Edge Cases and Failure Handling

- **No backfill**: Existing episodes stay as-is. Work dirs persist 180 days in `/tmp/`, so a backfill script is feasible later but out of scope.
- **Missing plan.json or work dir**: Log warning, publish episode with no show notes (same as today).
- **URL resolution**: Already handled — Levine collector resolves Bloomberg redirects at collection time. Semafor/Antiwar/Zvi caches store direct URLs. No extra resolution needed at show notes time.
- **Non-digest feeds**: Levine, Yglesias, Silver are unaffected — they don't generate plan.json or use this flow.

## Components Changed

| Component | Change |
|---|---|
| `pipeline/db.py` | Add `summary` and `articles_json` columns + Episode fields |
| `pipeline/rundown_writer.py` | Prompt change + return `WriterOutput(script, summary)` |
| `pipeline/fp_writer.py` | Same prompt change + `WriterOutput` return |
| `pipeline/show_notes.py` (new) | Extract articles from work dir, build structured list |
| `pipeline/things_happen_processor.py` | Call show_notes extractor, pass data to `insert_episode` |
| `pipeline/fp_processor.py` | Same integration |
| `pipeline/feed.py` | Render HTML from structured data into `<content:encoded>` + `<description>` |
