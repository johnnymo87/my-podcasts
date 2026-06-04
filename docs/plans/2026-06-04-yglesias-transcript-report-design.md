# Yglesias / The Argument Transcript Report Path — Design

Date: 2026-06-04

## Problem

Matt Yglesias's Slow Boring occasionally publishes the newsletter form of
*The Argument* podcast hosted by Jerusalem Demsas (regular weekly episodes
with Matt, and one-off live events with guests). For paying subscribers the
email body contains the **full verbatim transcript** — ~80 minutes of TTS.

Today the pipeline *suppresses* these posts entirely: `yglesias_filter.should_skip`
matches a fixed set of footer markers and `processor.process_email_bytes`
short-circuits with `skipped=True` (no audio, no feed entry).

Two problems with the status quo:

1. **Dropping is the wrong outcome.** The user would rather get a useful
   *report* on the conversation (like the ChinaTalk transcript path) than
   nothing at all.
2. **The current detector is too narrow.** Its markers are tuned to the
   regular weekly-episode footer (`WATCH THE EPISODE HERE`, `Time stamps:`,
   `Subscribe: Apple Podcasts | ...`, `New episodes post every Thursday.`,
   `TheArgumentMag.com`). A real live-event post
   (`/tmp/Can-AI-Cure-Cancer.eml`, 2026-06-04) contains **none** of those
   markers, so it slips the net entirely and would be read aloud verbatim.
   `TheArgumentMag.com` is also matched case-sensitively while the email uses
   lowercase `theargumentmag.com`.

## Goals

- Replace the *skip* behavior with a *report* behavior, mirroring the
  ChinaTalk transcript path.
- Detect Argument transcript posts deterministically (no LLM classifier),
  robustly across both the regular-episode and live-event formats.
- For transcript posts, replace the TTS body with an AI-written spoken
  briefing about the conversation and publish it to the existing `yglesias`
  feed with a `Report: ` title prefix.
- Fail safe: any failure in detection or report generation falls back to the
  standard reading. The listener always gets an episode.

## Non-goals

- No new feed, preset, or DB schema.
- No LLM-based detection (the user chose deterministic transcript-shape
  detection).
- No retry/queue infrastructure; runs inline in the existing email flow.
- No "force report" / "force reading" override.
- No backfilling of previously-skipped posts.
- No multi-speaker TTS.

## Detection: transcript-shape, not footer markers

Rationale: the thing we actually care about is "is this body a podcast
transcript." A transcript is structurally a sequence of named speaker turns.
That signal survives Substack footer/boilerplate changes and is near-impossible
to trigger on a normal essay.

`is_argument_transcript(body) -> bool`:

- Scan for line-start speaker labels: a capitalized name (1–4 capitalized
  words, allowing `.`/`'`/`-`) immediately followed by `:` and whitespace.
  Regex sketch: `(?m)^\s*([A-Z][\w.'’\-]*(?:\s+[A-Z][\w.'’\-]*){0,3}):\s`.
- Tally occurrences per distinct label.
- **Fire only if at least 2 distinct labels each recur at least 5 times** as
  line-starts (`_MIN_SPEAKERS = 2`, `_MIN_TURNS_PER_SPEAKER = 5`).

On `Can-AI-Cure-Cancer.eml`: `Jerusalem Demsas:` ×61, `Kelsey Piper:` ×53 →
fires. A normal Slow Boring essay has no repeated `Name:` line-starts → does
not fire. Timestamp lines (`0:00-...`) start with a digit and never match.
Single incidental labels (`Show Notes:`, `Time stamps:`) appear once and never
reach the threshold.

The detector is feed-agnostic and pure (returns a bool, raises nothing in
normal operation). The feed gate lives in the orchestrator, mirroring
`chinatalk_report`.

## Architecture

Yglesias emails currently flow:

```
process_email_bytes
  → resolve_preset → yglesias
  → SubstackAdapter.clean_body
  → if should_skip: mark_processed + return skipped=True   ← REMOVED
  → maybe_rewrite_chinatalk (no-op for yglesias)
  → ttsjoin → R2 → episode insert → feed regen
```

New flow — the skip early-return is deleted and replaced by a rewrite hook
alongside the ChinaTalk one:

```
... clean_body
  → maybe_rewrite_yglesias(body, title, feed_slug, subject_raw)
       if feed_slug == "yglesias" and is_argument_transcript(body):
         report = generate_report(body, subject)   # opencode-serve
         body  = report.script
         title = "Report: " + title
       (any exception → log, return inputs unchanged)
  → maybe_rewrite_chinatalk(...)   # mutually exclusive by feed_slug
  → ttsjoin → R2 → episode insert → feed regen
```

The two rewrites are mutually exclusive by `feed_slug`, so ordering is
irrelevant; they sit together for readability.

## Components

### `pipeline/yglesias_filter.py` (repurposed)

Remove `_PODCAST_MARKERS`, `_MIN_MARKER_HITS`, `is_demsas_podcast`,
`should_skip`. Add:

```python
def is_argument_transcript(body: str) -> bool: ...
```

Deterministic transcript-shape detector as described above. Module docstring
rewritten to describe the report path (it no longer "skips").

### `pipeline/yglesias_writer.py` (new)

Mirrors `pipeline/chinatalk_writer.py` exactly in plumbing:

- `generate_report(*, body, subject) -> ReportOutput` (own `ReportOutput`
  dataclass with `script`/`summary`).
- opencode-serve lifecycle: `create_session` → `send_prompt_async` →
  `wait_for_idle(timeout=900)` → `get_messages` → `get_last_assistant_text`,
  `delete_session` in `finally`.
- `<summary>` / `<script>` tag extraction; empty-script guard raises
  `RuntimeError`.

Only the prompt differs. The Argument-tuned `PROMPT_TEMPLATE` tells the writer
that the input is a transcript of *The Argument*, a debate/conversation podcast
hosted by Jerusalem Demsas (sometimes with Matt Yglesias, sometimes guests),
that the post may open with a short editor's framing before the transcript
begins, and to produce a 5–10 minute spoken briefing covering participants,
topics, key claims with attribution, and notable disagreements — same shape as
the ChinaTalk briefing.

### `pipeline/yglesias_report.py` (new)

Mirrors `pipeline/chinatalk_report.py`:

```python
def maybe_rewrite_yglesias(
    *, body: str, title: str, feed_slug: str, subject_raw: str
) -> tuple[str, str]:
    if feed_slug != "yglesias":
        return body, title
    try:
        if not is_argument_transcript(body):
            return body, title
        report = generate_report(body=body, subject=subject_raw)
        return report.script, f"Report: {title}"
    except Exception:
        logger.exception("yglesias report path failed; falling back to reading")
        return body, title
```

### `pipeline/processor.py` (modified)

- Drop `from pipeline.yglesias_filter import should_skip`; add
  `from pipeline.yglesias_report import maybe_rewrite_yglesias`.
- Delete the skip early-return block (current lines ~125–143).
- Insert the `maybe_rewrite_yglesias(...)` call next to the existing
  `maybe_rewrite_chinatalk(...)` call.
- Remove the `skipped: bool = False` field from `ProcessResult` and its
  docstring note.

### `pipeline/__main__.py` (modified)

Remove the `if result.skipped:` branch in `process_command` (now dead).

## Data Flow

1. Email arrives → email-ingest worker → R2 + queue.
2. Consumer picks up the message → `process_r2_email_key` →
   `process_email_bytes`. (The consumer ignores the return value; it relies on
   `store.mark_processed` inside the processor and on exceptions.)
3. Body extracted and cleaned by `SubstackAdapter`.
4. **NEW**: `maybe_rewrite_yglesias` runs. For a transcript it opens an
   opencode session, sends the prompt, extracts the script, and rewrites
   `body` + `episode_title`.
5. `maybe_rewrite_chinatalk` runs (no-op for yglesias).
6. TTS runs on whatever `body` now contains.
7. Episode inserted, feed regenerated. The episode looks like any other
   yglesias episode aside from the `Report: ` title prefix.

## Error Handling

- `is_argument_transcript` is pure; the orchestrator still wraps everything in
  `try/except Exception` to defend against future leaks.
- `generate_report` may raise (timeout, empty output, network). The
  orchestrator catches it and returns the inputs unchanged.
- **Conservative fallback differs from ChinaTalk only in consequence:** a
  missed transcript or a writer failure degrades to *reading the transcript in
  full* (one long episode), never to dropping a real essay. This matches the
  prior filter's "rather one over-long episode than a silently dropped essay"
  philosophy — we've only changed the default from *drop* to *read*.
- No DB writes happen before the rewrite resolves, so there is no partial
  state to roll back.

## Configuration

- Reuses the `OPENCODE_*` env vars consumed by `opencode_client`.
- No new env vars, no new feature flags. (No `GEMINI_API_KEY` dependency — the
  detector is deterministic.)

## Testing

Unit:

- `pipeline/test_yglesias_filter.py` (rewritten)
  - synthetic transcript with 2 speakers ≥5 turns each → True
  - normal essay (no repeated `Name:` line-starts) → False
  - one speaker only, many turns → False (needs ≥2 distinct)
  - two speakers but <5 turns each → False (threshold edge)
  - empty body → False
  - timestamp/`Show Notes:` style incidental colons → False
- `pipeline/test_yglesias_writer.py` (new, mirrors `test_chinatalk_writer.py`)
  - mocks the `opencode_client` functions
  - happy path: parses `<script>`/`<summary>`
  - timeout → RuntimeError; session still deleted
  - empty script → RuntimeError
  - missing tags → full text used as script
- `pipeline/test_yglesias_report.py` (new, mirrors `test_chinatalk_report.py`)
  - non-yglesias feed → passthrough
  - yglesias essay (detector False) → passthrough
  - yglesias transcript → body replaced, title prefixed `Report: `
  - writer raises → passthrough (standard reading)
  - detector raises → passthrough (standard reading)
  - processor wiring: source contains `maybe_rewrite_yglesias`, and the skip
    machinery (`should_skip`) is gone

Regression:

- Update/replace the old `test_processor_calls_should_skip` assertion.
- No changes to consumer, DB, or feed tests.

## Docs

Update the "Yglesias Podcast Filter" section in the root `AGENTS.md` to
describe the report path (detection by transcript shape, body rewritten to a
spoken briefing, `Report: ` title prefix) instead of the skip behavior.

## Open Questions

None at design time. Prompt tuning for *The Argument* may need a pass once we
see real output against a couple of historical transcripts.
