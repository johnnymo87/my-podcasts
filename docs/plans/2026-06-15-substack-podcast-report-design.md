# Generic Substack Post -> Podcast Command (report + read modes)

Beads: my-podcasts-78k
Date: 2026-06-15

## Context

The pipeline can already turn a podcast transcript into a spoken *report*
(briefing) in two feed-specific places — `chinatalk_report.py` /
`chinatalk_writer.py` and `yglesias_report.py` / `yglesias_writer.py` — but
both are wired into the email path (`processor.process_email_bytes`) and only
fire on their own feeds. It can also publish a *one-off* episode from a
pre-written script via `publish-script` (`script_processor.publish_script`),
and read a blog post in full via the RSS-driven `blog_poller` (Gemini
adaptation + TTS).

There is no way to point the pipeline at an arbitrary Substack post URL and get
an episode out of it. The immediate motivating case is a report on the Dwarkesh
podcast episode "David Reich – Why the Bronze Age was an inflection point in
human evolution" (`https://substack.com/@dwarkesh/p-196892360`, canonical
`https://www.dwarkesh.com/p/david-reich-2`), but this is expected to be an
occasional, reusable need.

### What the target looks like

The post is free (`audience: everyone`), ~133 min of audio, a ~21k-word
two-speaker interview transcript. The clean ingestion path is the Substack JSON
API, which returns structured metadata plus the full `body_html` with no
JavaScript, scraping, or paywall issues:

- by id:   `https://<host>/api/v1/posts/by-id/<numeric-id>`
- by slug: `https://<host>/api/v1/posts/<slug>`

Both endpoints were verified against `dwarkesh.com` and return identical
`body_html` (179 KB). Transcript structure in `body_html`: intro `<p>`s, then
`<h*>Sponsors</h*>`, `<h*>Timestamps</h*>` (a TOC of anchor links), then
`<h*>Transcript</h*>`, then `<h*>HH:MM:SS – section title</h*>` headers and
speaker turns shaped as `<p><strong>Speaker Name</strong></p>` followed by
content `<p>`s. (Some other Dwarkesh posts additionally carry an inline
`<em>HH:MM:SS</em>` per turn.)

## Goals

- One reusable CLI command that ingests any Substack post by URL/id and
  publishes a one-off episode.
- Two modes: **report** (briefing) for transcript/podcast posts, and **read**
  (faithful full reading) for essay posts.
- Strip transcript timestamps and boilerplate (Sponsors, Timestamps TOC) from
  the text fed to the model, to reduce token bloat.
- Maximal reuse of existing publish/TTS/feed machinery.

## Non-goals

- No automatic polling/scheduling — this is manual and on-demand.
- No transcript auto-detection (`transcript_detect.looks_like_transcript`
  matches `Name:` colon turns, not Dwarkesh's bold-name turns); the operator
  picks the mode explicitly.
- No paywall circumvention — paywalled posts are detected and rejected.
- No reuse of the original podcast audio; report mode produces new TTS audio.

## Approach

A generic `substack` module + CLI command. Ingestion and HTML normalization are
shared; the `--mode` flag selects the script-generation strategy; both modes
publish through the existing `publish_script` path.

```
URL/id ─▶ resolve_post ─▶ SubstackPost(body_html, title, …)
                              │
                   html_to_clean_text (strip timestamps/sponsors/TOC)
                              │
              ┌───────────────┴────────────────┐
       mode=report                        mode=read
   substack_writer.generate_report   blog_poller.adapt_for_audio
        (opencode-serve)                  (Gemini)
              │                                │
              └──────────── script ────────────┘
                              │
                     publish_script(script, title, feed_slug,
                                     source_url=canonical_url, …)
                       (TTS -> R2 -> feed regen -> archive)
```

### Components

1. **`pipeline/substack.py`** (new)
   - `resolve_post(url_or_id: str) -> SubstackPost`: accepts a numeric id, a
     short link (`substack.com/@user/p-<id>`), or a canonical slug URL
     (`<host>/p/<slug>`); calls the appropriate API endpoint via `requests`.
   - `SubstackPost` dataclass: `title, subtitle, description, canonical_url,
     body_html, slug, host, audience, wordcount`.
   - Paywall guard: if `audience != "everyone"` and the body is truncated
     (`should_send_free_preview` true / `truncated_body_text` present / missing
     `body_html`), raise a clear error.
   - `html_to_clean_text(body_html: str) -> str`: bs4 (`html.parser`)
     normalization. Drops the Sponsors and Timestamps TOC sections (content
     under those headers until the next header), strips `HH:MM:SS [–-]` prefixes
     from section headers and any inline per-turn timestamps, renders
     `<strong>` speaker turns as clean labeled paragraphs, keeps the intro
     prose. Pure function -> unit-testable against a saved HTML fixture.

2. **`pipeline/substack_writer.py`** (new)
   - `generate_report(*, body, subject) -> ReportOutput`, mirroring
     `yglesias_writer.py`: opencode-serve session, 900 s timeout,
     `<summary>`/`<script>` tag extraction, empty-output guard. Prompt tuned for
     a long-form interview podcast (host + guest, claims/evidence with
     attribution, disagreements, concrete detail; write for the ear).

3. **`pipeline/blog_poller.adapt_for_audio`** (reuse) for read mode.

4. **`pipeline/script_processor.publish_script`** (extend)
   - Add optional `source_url: str | None = None` (backward compatible). When
     set, the inserted `Episode.source_url` is populated so the feed item
     `<link>` points at the original post. No change for existing callers.

5. **`pipeline/__main__.py`** (extend) — `substack` command:
   ```
   uv run python -m pipeline substack \
     --url <substack-url-or-id> \
     --mode {report|read}        # default: report
     --feed-slug <slug>          # required, mirrors publish-script
     [--title <override>]        # default: post title; report mode prefixes "Report: "
     [--voice nova] [--category Technology]
     [--date YYYY-MM-DD] [--dry-run]
   ```
   Orchestration: `resolve_post` -> `html_to_clean_text` -> mode branch ->
   write script to a temp file + auto-built show-notes markdown (subtitle +
   "Original post" link) -> `publish_script(..., source_url=canonical_url)`.
   `--dry-run` stops after script generation and prints the script path.

### Title / metadata conventions

- report mode: episode title = `Report: <post title>` (matches chinatalk /
  yglesias). read mode: `<post title>`. `--title` overrides.
- `source_url` = canonical post URL. show notes = subtitle + original-post link.
- Default voice `nova`, category `Technology` (overridable), matching
  `publish_script` defaults.

### Error handling

- Network/API failure on fetch -> raised with the offending URL/id.
- Paywalled/truncated body -> explicit error (don't ship a partial transcript).
- Empty model output -> already guarded inside the writer (raises).
- Read mode with no `GEMINI_API_KEY` -> `adapt_for_audio` returns None ->
  command errors clearly rather than publishing empty audio.

### Testing

- `html_to_clean_text`: fixture-based unit tests — timestamps and Sponsors/TOC
  removed, speaker labels and intro retained, no markdown artifacts.
- `resolve_post` URL/id parsing: id, short link, slug URL (mock `requests`).
- Paywall guard: truncated payload raises.
- CLI mode wiring: report vs read selects the right generator and title prefix
  (mock opencode, Gemini, ttsjoin, R2 — mirroring `test_yglesias_report.py` and
  `test_blog_poller.py`).
- `publish_script` `source_url`: populates `Episode.source_url`.

## Rollout

1. Implement modules + CLI + tests; `uv run pytest` green; ruff clean.
2. Dry-run the Dwarkesh Bronze Age episode (`--mode report --feed-slug
   dwarkesh --dry-run`); review the generated script.
3. On approval, publish to production (R2 + `feeds/dwarkesh.xml`).
4. Update root `AGENTS.md` with the new command and module map.

## Alternatives considered

- **Dwarkesh-specific module** (like chinatalk/yglesias): rejected — Substack
  API ingestion is publication-agnostic, so a generic command is nearly free.
- **Scrape rendered HTML/markdown**: rejected — needs JS, hits paywalled
  truncation, and is far messier than the JSON API.
- **Extract a shared report writer** for chinatalk/yglesias/substack: deferred —
  the codebase deliberately duplicates these writers; a refactor is out of scope
  here. `substack_writer.py` follows the established duplication pattern.
