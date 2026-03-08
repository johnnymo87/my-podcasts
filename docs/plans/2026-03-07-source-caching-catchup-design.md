# Source Caching and Catch-Up Data Gathering Design

## Problem

Both podcasts have data gathering gaps when episodes are missed:

- **FP Digest** skips weekends (Mon-Fri timer), so Monday's episode misses Saturday's routed FP links and any ephemeral homepage content that rotated off.
- **Things Happen** is event-driven (Levine email). If Levine skips several days, Zvi posts and Semafor articles from the gap are missed.

Additionally, antiwar.com homepage and Semafor RSS are ephemeral — content rotates off and is lost if not captured the day it appears. The Zvi cache already solves this for Zvi; the same pattern should apply to all sources.

Date windows also use UTC in some places and Eastern time in others, causing late-evening EST misalignment.

## Design

### 1. Persistent Source Caches

Four persistent caches, all using the same markdown format:

| Cache | Path | Source |
|---|---|---|
| Zvi (existing) | `/persist/my-podcasts/zvi-cache/` | `thezvi.substack.com/feed` |
| Antiwar Homepage | `/persist/my-podcasts/antiwar-homepage-cache/` | `antiwar.com` homepage scrape |
| Antiwar/CJ RSS | `/persist/my-podcasts/antiwar-rss-cache/` | 4 RSS feeds (3 antiwar + Caitlin Johnstone) |
| Semafor | `/persist/my-podcasts/semafor-cache/` | `semafor.com/rss.xml` |

**File format** (consistent with Zvi cache):

```
# Headline

URL: https://...
Published: 2026-03-07
Source: semafor
Category: Technology
Type: article

Full text content here...
```

**Filenames:** `{YYYY-MM-DD}-{slug}.md`. Deduplication by filename — if the file exists, skip it. Idempotent.

**Metadata variations by source:**
- Semafor files include `Category:` for FP/TH routing.
- Antiwar homepage files include `Region:` preserving the region grouping from the homepage.
- Antiwar RSS files include `Source:` identifying which of the 4 feeds the entry came from.

**Retention:** 180 days for all caches, cleaned up in `_cleanup_old_work_dirs`.

### 2. Daily Sync Job

New CLI command: `uv run python -m pipeline sync-sources`

Calls all four sync functions:
1. `sync_zvi_cache()` (existing)
2. `sync_antiwar_homepage_cache()` (new)
3. `sync_antiwar_rss_cache()` (new)
4. `sync_semafor_cache()` (new)

Each follows the `sync_zvi_cache` pattern: fetch, deduplicate by filename, write new files, return list of newly-written paths.

**Systemd timer:** Daily at 12:00 PM ET (noon), with `Persistent=true` for catch-up on boot. Runs well before FP Digest (6 PM ET) and captures the day's content while news cycles are active.

**Dual-mode for Zvi:** The Things Happen collector still calls `sync_zvi_cache()` at collection time in addition to the daily sync, since a Levine email might arrive after the daily sync and we want the freshest posts. The daily timer is a safety net.

### 3. Adaptive Lookback Window

New method in `db.py`:

```python
def days_since_last_episode(feed_slug: str) -> int | None
```

Queries `SELECT MAX(pub_date) FROM episodes WHERE feed_slug = ?`. Returns calendar days since last episode, or `None` if no episodes exist.

**Lookback calculation:**

```python
def _lookback_days(store, feed_slug, default=2, cap=14):
    days = store.days_since_last_episode(feed_slug)
    if days is None:
        return default
    return min(max(2, days + 1), cap)
```

- Minimum: 2 days (today + yesterday, same as current behavior)
- Maximum: 14 days (cap for long absences)
- The `+1` buffer catches edge cases around midnight
- Falls back to `default` if no previous episode exists

**All date windows use `America/New_York` timezone** (fixing the existing UTC issue in FP routed links).

**Passed as parameter:** Lookback days computed by the caller (consumer or CLI) and passed to collectors as `lookback_days: int = 2`. Keeps collectors free of DB dependencies.

### 4. Collector Changes

**FP Digest collector (`fp_collector.py`):**
- Remove live `scrape_homepage()` call — read from antiwar homepage cache
- Remove live `_fetch_rss_articles()` call — read from antiwar RSS cache
- Remove live `_fetch_semafor_fp_articles()` call — read from Semafor cache (filter by FP categories using `Category:` metadata)
- Routed FP links — expand window from 2 days to `lookback_days`, fix timezone to Eastern
- New `lookback_days: int = 2` parameter on `collect_fp_artifacts`

**Things Happen collector (`things_happen_collector.py`):**
- Zvi day-of articles — expand window from 2 days to `lookback_days`
- Semafor TH articles — switch from live fetch to Semafor cache (filter by TH categories using `Category:` metadata), filtered by lookback window
- `sync_zvi_cache()` still called at collection time (dual-mode)
- New `lookback_days: int = 2` parameter on `collect_all_artifacts`

**Consumer (`consumer.py`):**
- Compute `lookback_days` from `days_since_last_episode()` before calling each collector
- Pass `lookback_days` to `collect_fp_artifacts` and `collect_all_artifacts`
- Add three new cache cleanups (180-day retention) alongside existing Zvi cleanup

**Dry run (`__main__.py`):**
- FP Digest `--dry-run` defaults to `lookback_days=2` (no DB access). Optional `--lookback` CLI flag for testing.

### 5. New Module: `pipeline/source_cache.py`

Contains:
- `sync_antiwar_homepage_cache(cache_dir) -> list[Path]`
- `sync_antiwar_rss_cache(cache_dir) -> list[Path]`
- `sync_semafor_cache(cache_dir) -> list[Path]`

Each function fetches from its source, writes new entries as markdown files, and returns newly-created paths. Uses the same `_slugify` pattern as `zvi_cache.py`.

### 6. Summary of All Changes

| Component | Change |
|---|---|
| **New: `pipeline/source_cache.py`** | Three sync functions for antiwar homepage, antiwar RSS, Semafor |
| **New: CLI `sync-sources`** | Calls all four sync functions |
| **New: systemd timer** | `sync-sources.timer` daily at noon ET |
| **`pipeline/db.py`** | Add `days_since_last_episode(feed_slug)` |
| **`pipeline/fp_collector.py`** | Switch from live fetches to cache reads, add `lookback_days`, fix timezone |
| **`pipeline/things_happen_collector.py`** | Switch Semafor to cache, expand Zvi/Semafor windows to `lookback_days` |
| **`pipeline/consumer.py`** | Compute lookback per feed, pass to collectors, add cache cleanups |
| **`pipeline/__main__.py`** | Add `sync-sources` command |
| **`AGENTS.md`** | Document caches, sync job, lookback behavior |
| **Workstation config** | Systemd timer+service for sync-sources |

**Unchanged:**
- `fp_homepage_scraper.py` — still used, called by sync job instead of collector
- `zvi_cache.py` — unchanged, dual-mode (sync job + collector)
- RSS feed parsing logic — same, runs in sync job context
- `categorize_semafor_article()` — same, applied when reading from cache
- All enrichment (Exa, xAI, Zvi keyword search) — untouched
