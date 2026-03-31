# Publish Script: One-Off Podcast from Pre-Written Script

## Problem

The pipeline supports automated email-based podcasts and daily digests, but has no way to produce a one-off episode from a pre-written script with show notes. This is needed for ad-hoc deep-dive episodes on specific topics.

## Approach

New `publish-script` CLI command + thin `script_processor.py` module. Follows the existing processor pattern (TTS -> upload -> insert episode -> regenerate feeds) but strips away email parsing, AI collection, and source management.

## CLI Interface

```
uv run python -m pipeline publish-script \
    --script-file PATH      # required: markdown or plain text script
    --title TEXT             # required: episode title
    --feed-slug TEXT         # required: which feed to publish on
    --show-notes PATH        # optional: markdown show notes file
    --voice TEXT             # default: nova
    --category TEXT          # default: Technology
    --date TEXT              # default: today (YYYY-MM-DD)
    --dry-run                # flag: run TTS but don't publish to R2/DB
```

## Data Flow

```
1. Read script markdown
2. Strip markdown formatting (headings, rulers, bold/italic, end marker) -> plain text
3. ttsjoin (tts-1-hd model) -> MP3 in temp dir
4. ffprobe -> duration measurement
5. Upload MP3 to R2 at episodes/{feed_slug}/{date}-{slug}.mp3
6. If --show-notes provided:
   a. Extract "## Episode Summary" section as plain text summary
   b. Convert full markdown to HTML via `markdown` library
7. Insert Episode row into SQLite
8. regenerate_and_upload_feed()
```

## Data Model Change

Add `show_notes_html` column to the `episodes` table (nullable TEXT). This stores pre-rendered HTML for episodes with rich show notes that don't fit the structured `articles_json` format.

In `feed.py`, when generating `<content:encoded>`: if `show_notes_html` is set, use it directly; otherwise fall back to building HTML from `summary` + `articles_json` (existing behavior for automated pipelines).

Migration follows the established pattern in `StateStore._migrate_schema`.

## Script Text Preparation

Strip markdown formatting for TTS input:
- Markdown headings (`# `, `## `, `### `)
- Horizontal rules (`---`)
- Bold/italic markers (`**`, `*`)
- `*[END OF SCRIPT]*` marker

Simple regex/string operations, no markdown library needed for this step.

## Show Notes Handling

- `summary` field: extracted from `## Episode Summary` section of the show notes markdown (plain text for `<description>`)
- `show_notes_html` field: full markdown converted to HTML via Python `markdown` library with `tables` and `fenced_code` extensions (for `<content:encoded>`)

## Files Changed

| File | Change |
|------|--------|
| `pipeline/script_processor.py` | New. ~100 lines. Core logic. |
| `pipeline/__main__.py` | Add `publish-script` Click command (~30 lines) |
| `pipeline/db.py` | Add `show_notes_html` to `Episode` + migration |
| `pipeline/feed.py` | Prefer `show_notes_html` over built HTML |
| `pyproject.toml` | Add `markdown` dependency |
| `pipeline/test_script_processor.py` | New. Tests for text stripping, show notes parsing. |

## What Stays Untouched

- All existing processors (email, Rundown, FP Digest)
- Consumer service
- Existing feed generation for automated episodes
- The `articles_json` code path
