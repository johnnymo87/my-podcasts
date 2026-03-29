# Aaronson Blog Podcast Design

**Date:** 2026-03-29
**Source:** Scott Aaronson's "Shtetl-Optimized" (https://scottaaronson.blog/)
**Feed slug:** `aaronson`
**Feed URL:** `https://podcast.mohrbacher.dev/feeds/aaronson.xml`
**Category:** Technology

## Overview

Add Scott Aaronson's "Shtetl-Optimized" blog as a podcast source. Each new blog post becomes a standalone episode. This is the first RSS/blog-polled source -- all existing single-episode sources (Levine, Yglesias, Silver) arrive via email.

The blog covers quantum computing, complexity theory, AI alignment, and science policy. Posts are long-form, conversational, and irregular (roughly weekly). Content includes math notation, images, blockquotes, and dense hyperlinks, requiring an AI adaptation step before TTS.

## Architecture

A new **blog poller** pipeline component -- a third ingestion path alongside email and digest:

```
RSS Feed (scottaaronson.blog/?feed=rss2)
  |
  v
Blog Poller (in consumer loop, every 6 hours)
  |-- Fetch RSS feed
  |-- Compare post URLs against processed_blog_posts table (dedup)
  |-- For each new post:
  |     |-- Extract HTML from content:encoded
  |     |-- AI adaptation (Gemini Flash) for audio
  |     |-- TTS via ttsjoin
  |     |-- Upload MP3 to R2
  |     |-- Insert episode + regenerate feed
  v
Published at /feeds/aaronson.xml
```

Key decisions:
- **No pending job table.** Posts don't need retry/backoff. The poller checks for new posts and processes synchronously. Failures retry on the next poll cycle (6 hours).
- **Deduplication via source_url.** A `processed_blog_posts` table tracks which post URLs have been processed.
- **Runs inside the consumer process.** Added as a periodic check in the `consume_forever` loop, not a separate process.

## Components

### New files

- `pipeline/blog_poller.py` -- Core module:
  - `fetch_blog_feed(feed_url) -> list[BlogPost]` -- parse RSS XML
  - `adapt_for_audio(html_content, title) -> str` -- Gemini Flash call to rewrite HTML as clean TTS prose
  - `poll_and_process(store, r2_client)` -- main entry point

- `pipeline/blog_sources.py` -- Blog source definitions:
  ```python
  @dataclass(frozen=True)
  class BlogSource:
      name: str           # "Scott Aaronson - Shtetl-Optimized"
      feed_url: str       # "https://scottaaronson.blog/?feed=rss2"
      feed_slug: str      # "aaronson"
      tts_voice: str      # "fable"
      category: str       # "Technology"
  ```

### Modified files

- `pipeline/presets.py` -- Add `aaronson` preset
- `pipeline/consumer.py` -- Add periodic blog poll check in `consume_forever` loop
- `pipeline/db.py` -- Add `processed_blog_posts` table, `StateStore` methods for checking/marking processed posts
- `pipeline/__main__.py` -- Add CLI command `poll-blogs [--dry-run]`
- `pipeline/feed.py` -- Ensure feed generation handles the `aaronson` slug

### Data flow (single post)

1. RSS XML parsed -> `BlogPost(title, url, pub_date, html_content, guid)`
2. Check `processed_blog_posts` table -> skip if already done
3. `adapt_for_audio(html_content, title)` -> clean prose string
4. Write prose to temp `.txt` file
5. `ttsjoin` -> MP3
6. Upload MP3 to R2 at `episodes/aaronson/{date}-{slug}.mp3`
7. Insert Episode row (`feed_slug="aaronson"`, `source_url=post_url`)
8. Mark post as processed in `processed_blog_posts`
9. Regenerate feed XML and upload

## AI Adaptation for Audio

Uses Gemini Flash with a focused system prompt. Input is raw HTML from `content:encoded`. Output is clean prose for TTS.

**Handles:**
- Images: drop entirely
- Math: verbalize (e.g., `x<sup>r</sup> mod N` -> "x to the r mod N")
- Hyperlinks: keep text, drop URLs
- Blockquotes: introduce with attribution
- Horizontal rules: paragraph breaks
- Lists: convert to flowing prose or enumerated points

**Does NOT:**
- Summarize -- full content preserved
- Editorialize -- author's voice stays intact
- Add commentary or framing

**System prompt:**
```
You are preparing a blog post for text-to-speech conversion.
Convert the HTML to clean spoken prose. Preserve ALL content
and the author's voice faithfully. Do not summarize or editorialize.

Rules:
- Remove images entirely, no references to them
- Verbalize math notation naturally (x squared, 2 to the n, etc.)
- Keep hyperlink text, drop URLs
- Introduce blockquotes with attribution where available
- Convert horizontal rules to paragraph breaks
- Output plain text, no markdown formatting
```

## Scheduling, Deduplication, and Error Handling

**Polling:** Every 6 hours inside the consumer loop (tracked via in-memory timestamp). CLI `poll-blogs` for manual triggering.

**Deduplication table:**
```sql
CREATE TABLE IF NOT EXISTS processed_blog_posts (
    source_url TEXT PRIMARY KEY,
    feed_slug TEXT NOT NULL,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**Error handling:** All failures (RSS fetch, Gemini, TTS, R2) skip the post and retry next poll cycle. No post is permanently skipped -- it stays unprocessed until it succeeds. The RSS feed contains ~10 posts (~2-3 month window).

**Initial backfill:** First run processes all posts currently in the RSS feed, using their original `pubDate` for correct ordering. CLI supports `--since YYYY-MM-DD` to limit backfill.

## Voice

TTS voice: `fable` (warm, slightly British -- suits academic/intellectual content). All existing voices: ash (Levine), shimmer (Yglesias), echo (Silver), nova (Rundown), onyx (FP Digest).

## Configuration

- **TTS model:** tts-1-hd (consistent with all other feeds)
- **Feed slug:** aaronson
- **Category:** Technology
- **Preset name:** "Scott Aaronson - Shtetl-Optimized"
- **Route tags:** ("aaronson", "shtetl-optimized")
