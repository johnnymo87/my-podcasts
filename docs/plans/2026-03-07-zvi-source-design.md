# Zvi Substack as Things Happen Source

## Summary

Add thezvi.substack.com ("Don't Worry About the Vase") as a first-class source for Things Happen, with a persistent cache and two reading modes: day-of articles for the roundup, and keyword search across the full cache for AI enrichment.

## Source Characteristics

- RSS: `https://thezvi.substack.com/feed` — 20 entries, full HTML in `<content:encoded>`, no tags/categories
- Publish frequency: 3-5 posts per week, all AI-related
- Two post types:
  - **AI roundups** (`AI #\d+` in title): 30-40 `<h4>` sections each, 100K-225K chars. Each section is a distinct AI topic.
  - **Focused essays** (everything else): 3-20 `<h4>` sections, 30K-80K chars. Sections are chapters within one coherent argument.
- No category routing needed (all content relevant to Things Happen).

## Architecture

### New module: `pipeline/zvi_cache.py`

Two public functions:

**`sync_zvi_cache(cache_dir: Path) -> list[Path]`**

Fetches the Zvi RSS feed and processes any posts not already cached. Returns the list of newly-written file paths.

- Cache location: `/persist/my-podcasts/zvi-cache/`
- Filename format: `{YYYY-MM-DD}-{slug}.md` for essays, `{YYYY-MM-DD}-{post-slug}--{section-slug}.md` for roundup sections
- **Roundups** (title matches `AI #\d+`): split on `<h4>` tags. Each section becomes a separate cached file. Text extracted by stripping HTML tags from the section content.
- **Essays** (everything else): kept whole as one file. Text extracted via trafilatura from the full `<content:encoded>`.
- Idempotent: skips posts whose date-slug files already exist in cache.
- 180-day retention (same policy as work directories).

**`search_zvi_cache(query: str, cache_dir: Path, max_results: int = 5) -> list[dict]`**

Keyword search across all cached files. Returns matches ranked by relevance, each with headline, file path, and a context snippet.

Uses the same tokenize-and-score approach as `_keyword_score` in `rss_sources.py` — tokenize the query, score each file by title + content token overlap, return top N.

### File format in cache

```
# Section Title (or Post Title for essays)

Post: Original Post Title
URL: https://thezvi.substack.com/p/...
Published: 2026-03-05
Type: roundup-section | essay

[extracted text content]
```

### Changes to `things_happen_collector.py`

**Phase 1c: Zvi articles** (after Semafor, before trailing window copy):

1. Call `sync_zvi_cache(cache_dir)` to ensure cache is up to date.
2. Copy today's posts from cache into `{work_dir}/articles/zvi/`. "Today" means published within the last 24 hours (handles timezone drift and late-night publishing).
3. Add these to `headlines_with_snippets` so the editor sees them as first-class candidates alongside Levine links and Semafor articles.

**Phase 3 AI enrichment change:**

Replace `search_rss_sources(directive.ai_query, sources=AI_SOURCES)` with `search_zvi_cache(directive.ai_query, cache_dir)`. This searches the full 6-month persistent cache instead of re-fetching the RSS feed on each run. Results written to `enrichment/rss/{slug}-ai.md` in the same format as before.

### Cleanup

- Remove `AI_SOURCES` from `rss_sources.py` (Zvi was its only entry).
- Remove the `search_rss_sources(..., sources=AI_SOURCES)` call from the collector.
- Add Zvi cache cleanup to `_cleanup_old_work_dirs` in `consumer.py` (180-day retention).

## Data Flow

```
Zvi RSS feed
    |
    v
sync_zvi_cache() --- writes to ---> /persist/my-podcasts/zvi-cache/
    |                                     |
    |  (today's posts)                    |  (keyword search for is_ai directives)
    v                                     v
articles/zvi/ in work_dir           enrichment/rss/*-ai.md in work_dir
    |                                     |
    v                                     v
Editor AI sees as candidates        Agent sees as enrichment context
```

## What the editor sees

On a pipeline run, the editor receives `headlines_with_snippets` containing:
- Levine "Things Happen" links (from email)
- Semafor business/tech articles (from RSS)
- **Zvi posts published in the last 24 hours** — roundup sections as individual candidates, essays as single candidates

The editor triages all of these together and produces a research plan. Zvi content competes on equal footing with other sources.

## Testing

- `test_zvi_cache.py`: unit tests for `sync_zvi_cache` (roundup splitting, essay handling, idempotency, cleanup) and `search_zvi_cache` (keyword matching, ranking, max_results)
- Update `test_things_happen_collector.py`: mock `sync_zvi_cache`, verify day-of articles appear in work dir, verify enrichment uses cache search
