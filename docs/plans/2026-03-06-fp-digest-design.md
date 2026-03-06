# Foreign Policy Digest Podcast

**Goal**: A new daily podcast that synthesizes foreign policy news from antiwar.com (3 RSS feeds + curated homepage links) and Caitlin Johnstone into a 10-15 minute audio briefing, fully automated with no human-in-the-loop.

## Sources

The podcast draws from 5 sources, collected fresh each day:

1. **Antiwar.com Homepage** (`https://www.antiwar.com`) -- 13 region-grouped link sections at the bottom of the page (Frontline, Iran, Israel/Palestine, Lebanon, Europe, Russia, Asia, etc.). These are ~49 curated links to external news sites (Al Jazeera, Middle East Eye, Anadolu Agency, AP, etc.) that are NOT in any RSS feed. Static HTML, parsed via BeautifulSoup.
2. **Antiwar.com Original** (`https://original.antiwar.com/feed/`) -- Op-eds and viewpoints, ~3-4/day.
3. **Antiwar.com News** (`https://news.antiwar.com/feed/`) -- Staff reporting by Dave DeCamp, Jason Ditz, etc., ~8-10/day.
4. **Antiwar.com Blog** (`https://feeds.feedburner.com/AWCBlog`) -- Blog posts, reposts, video show summaries, ~3-5/day.
5. **Caitlin Johnstone** (`https://caitlinjohnstone.com.au/feed/`) -- Already configured in `rss_sources.py`.

## Architecture

The pipeline follows the same staged pattern as Things Happen but is simpler (no async agent session, no Telegram approval):

```
[Systemd Timer, daily 6 PM EST] → Collector → Editor AI → Light Enrichment → Writer AI → TTS → Publish
```

### Phase 1: Collection (`pipeline/fp_collector.py`)

Orchestrates fetching from all 5 sources:

- **Homepage scraper** (`pipeline/fp_homepage_scraper.py`): Fetches the antiwar.com HTML, parses the `class="hotspot"` region headers and their nested link tables, extracts headline + URL + region for each link. Fetches full article text via `trafilatura`.
- **RSS feeds**: Uses `feedparser` + `trafilatura` to fetch and extract articles from all 4 RSS feeds (3 antiwar.com + Caitlin Johnstone).
- **Deduplication**: URLs appearing in both the homepage and an RSS feed are deduplicated by URL (keep the richer version).
- **Trailing window**: Copies the last 3 scripts from `/persist/my-podcasts/scripts/fp-digest/` into `{work_dir}/context/`.

Directory structure:
```
/tmp/fp-digest-{job_id}/
  articles/
    homepage/{region}/{slug}.md
    rss/{source_name}/{slug}.md
  enrichment/
    exa/{slug}.md
  context/
    {date}.txt  (last 3 episode scripts)
```

### Phase 2: Editor AI (`pipeline/fp_editor.py`)

Gemini Flash-Lite (same model as Things Happen's editor) receives headlines + 300-char snippets from all sources plus the trailing window scripts. Returns a structured `FPResearchPlan`:

```python
class FPStoryDirective(BaseModel):
    headline: str
    source: str              # e.g. "homepage/iran", "rss/antiwar_news"
    priority: int            # 1-5, where 1 = lead story
    theme: str               # Grouping label, e.g. "Iran War", "NATO Expansion"
    needs_exa: bool          # True if article is paywalled
    exa_query: str
    include_in_episode: bool # Editor's selection

class FPResearchPlan(BaseModel):
    themes: list[str]        # 3-5 major themes across all sources
    directives: list[FPStoryDirective]
```

The editor selects 8-12 articles that best cover the 3-5 dominant themes, avoiding redundant coverage of the same event. It also deprioritizes stories well-covered in the trailing window unless there are new developments.

### Phase 3: Light Enrichment

Only Exa search, only for articles flagged `needs_exa=True` (paywalled sources). Results written to `{work_dir}/enrichment/exa/{slug}.md`. No xAI/Twitter search, no additional RSS search -- the core sources already provide the independent perspective.

### Phase 4: Writer AI (`pipeline/fp_writer.py`)

Synchronous opencode-serve call following the `summarizer.py` pattern (create session, send prompt, wait for idle, extract script, delete session). The writer receives:

- Today's date
- The editor's theme list
- Full article text for `include_in_episode=True` articles, grouped by theme
- Exa alternatives for paywalled articles
- Last 3 episode scripts (trailing window)

Prompt ethos (broad, not rule-based):
- Open with a brief intro, organize by theme, lead with the biggest story
- For each theme: what happened, why it matters, what the independent/antiwar perspective adds
- Conversational and natural, suitable for TTS
- Target 10-15 minutes (~1500-2200 words)
- Build on what the listener heard in previous episodes rather than repeating background
- Close with a brief sign-off

Timeout: 120 seconds. On failure, job stays pending for retry.

### Phase 5: TTS + Publish (`pipeline/fp_processor.py`)

Follows `things_happen_processor.py` exactly:
- Write script to temp file
- `ttsjoin --model tts-1-hd --voice coral`
- Upload MP3 to `episodes/fp-digest/{date}-fp-digest.mp3`
- Measure duration via `ffprobe`
- Insert episode, regenerate feeds
- Copy script to `/persist/my-podcasts/scripts/fp-digest/{date}.txt`
- Clean up work directory (180-day retention)

## Infrastructure

**Preset** (in `pipeline/presets.py`):
```python
NewsletterPreset(
    name="Foreign Policy Digest",
    route_tags=("fp-digest",),
    tts_model="tts-1-hd",
    tts_voice="coral",
    category="News",
    feed_slug="fp-digest",
)
```

**Database** (in `pipeline/db.py`):
New table `pending_fp_digest`:
- `id TEXT PRIMARY KEY`
- `date_str TEXT NOT NULL`
- `status TEXT NOT NULL DEFAULT 'pending'`
- `process_after TEXT NOT NULL`
- `created_at TEXT NOT NULL DEFAULT (datetime('now'))`

Methods: `insert_pending_fp_digest()`, `list_due_fp_digest()`, `mark_fp_digest_completed()`.

**CLI** (in `pipeline/__main__.py`):
```
uv run python -m pipeline fp-digest
```
Creates a pending job for today (if not already exists) and processes it.

**Systemd timer**: Runs daily at 23:00 UTC (6 PM EST). Creates the pending job and triggers processing.

**Consumer integration**: `consume_forever` loop checks `store.list_due_fp_digest()` alongside existing Things Happen job checks.

**Feed URL**: `https://podcast.mohrbacher.dev/feeds/fp-digest.xml`

**Cover art**: `assets/podcast/cover-fp-digest.jpg`

## Changes to Existing Code

**Remove "do not use delve" rules**: Strip the specific word prohibition from `pipeline/summarizer.py` and `pipeline/things_happen_agent.py`. Replace with broad writing-quality ethos (natural, conversational prose). This applies to both the new FP Digest and the existing Things Happen podcast.

**Extend cleanup**: `_cleanup_old_work_dirs` in `consumer.py` extended to also glob `fp-digest-*` directories.

**Update `rss_sources.py`**: Add the two missing antiwar.com feeds (original, blog) to be available as sources.

## New Files

| File | Purpose |
|------|---------|
| `pipeline/fp_homepage_scraper.py` | Scrapes antiwar.com homepage region link groups |
| `pipeline/fp_editor.py` | Gemini Flash-Lite editor -- triages into themes |
| `pipeline/fp_collector.py` | Orchestrates collection from all 5 sources + Exa |
| `pipeline/fp_writer.py` | Generates podcast script via opencode-serve |
| `pipeline/fp_processor.py` | TTS + upload + episode insertion |

## Modified Files

| File | Change |
|------|--------|
| `pipeline/db.py` | New `pending_fp_digest` table + methods |
| `pipeline/consumer.py` | FP digest job loop + extended cleanup |
| `pipeline/__main__.py` | New `fp-digest` CLI command |
| `pipeline/presets.py` | New preset |
| `pipeline/rss_sources.py` | Add original + blog feeds |
| `pipeline/summarizer.py` | Remove "delve" rule, replace with broad ethos |
| `pipeline/things_happen_agent.py` | Remove "delve" rule, replace with broad ethos |

## Tradeoffs

- **Pros**: Fully automated daily briefing from curated independent sources; reuses existing TTS/R2/feed infrastructure; homepage scraper captures the editorial curation that RSS misses; trailing window prevents repetition across episodes.
- **Cons**: Homepage HTML structure is fragile (hand-maintained, could change without notice); trafilatura on ~49 external URLs adds ~2-3 minutes to collection; Gemini Flash-Lite is a lightweight model so theme identification may occasionally miss nuance.
