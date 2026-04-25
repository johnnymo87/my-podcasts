# ChinaTalk Transcript Detection & Report Path — Design

Date: 2026-04-25

## Problem

The ChinaTalk Substack newsletter sometimes consists of a podcast transcript
rather than an article. Today the pipeline runs every chinatalk email
through the standard Substack adapter and TTS-reads the body verbatim.
The user does not want to listen to a TTS reading of a transcript; they
want a written report on it.

## Goals

- Detect when a chinatalk newsletter is a podcast transcript.
- For transcript newsletters, replace the TTS body with an AI-written
  report (a structured spoken briefing about the conversation) before TTS.
- Publish the report to the existing chinatalk feed, with a "Report: "
  title prefix so the listener can tell what they're getting.
- Fail safe: any failure in detection or report generation must fall back
  to the standard reading. The listener always gets an episode.

## Non-goals

- No new feed, preset, or DB schema.
- No retry/queue infrastructure for the new path; it runs inline in the
  existing email-processing flow.
- No persistent caching of classification decisions.
- No manual override of the classifier.
- No multi-voice synthesis.

## Architecture

ChinaTalk emails currently flow:

```
process_email_bytes
  → resolve_preset → chinatalk
  → SubstackAdapter.clean_body
  → ttsjoin → R2 → episode insert → feed regen
```

We add a branch inside `process_email_bytes`, scoped to
`preset.feed_slug == "chinatalk"`, between body cleaning and TTS:

```
... clean_body
  → if feed_slug == "chinatalk":
       is_transcript = classify(body, subject)   # Gemini Flash-Lite
       if is_transcript:
         report = generate_report(body, subject) # opencode-serve
         body  = report.script
         title = "Report: " + title
       (any exception → log, leave body/title unchanged)
  → ttsjoin ...
```

## Components

### `pipeline/chinatalk_classifier.py` (new)

Single public function:

```python
def is_transcript(body: str, subject: str) -> bool: ...
```

- Calls `gemini-3.1-flash-lite-preview` via `google.genai`, mirroring
  `blog_poller.adapt_for_audio`.
- System prompt asks: "Does this newsletter consist primarily of a
  podcast transcript with multiple named speakers and verbatim dialogue?
  Reply with exactly YES or NO."
- Parses the response: `True` only on a clean YES; everything else
  (NO, ambiguous, exception, missing API key) returns `False`.
- Logs the decision and any errors.

### `pipeline/chinatalk_writer.py` (new)

Mirrors `rundown_writer.py` and `fp_writer.py`:

- Builds a prompt that gives the writer the transcript and the subject
  line, and asks for a structured spoken briefing (~800–1500 words)
  covering: who participated, what topics were discussed, key claims
  and arguments with attribution, notable disagreements, and any
  interesting points of detail.
- Wraps output in `<summary>` and `<script>` tags, parsed the same way
  `rundown_writer` does.
- Uses `pipeline.opencode_client` with a 300-second `wait_for_idle`
  timeout, deletes the session in `finally`.
- Rejects empty script output by raising `RuntimeError`.

Returns a small `ReportOutput` dataclass with `script` and `summary`
fields. The summary is unused in v1 (we don't store show notes for
chinatalk episodes today) but parsing it keeps the prompt structure
consistent with the other writers and leaves a hook for later.

### `pipeline/processor.py` (modified)

In `process_email_bytes`, after the existing `adapter.clean_body(...)`
call, add a guarded block:

```python
if preset.feed_slug == "chinatalk":
    try:
        if chinatalk_classifier.is_transcript(body, subject_raw):
            report = chinatalk_writer.generate_report(body, subject_raw)
            body = report.script
            episode_title = f"Report: {episode_title}"
    except Exception:
        logger.exception("chinatalk report path failed; falling back to reading")
```

No other changes to the function. `body` and `episode_title` are
already local variables that get fed into TTS and the episode row.

## Data Flow

1. Email arrives → email-ingest worker → R2 + queue.
2. Consumer picks up the message → `process_r2_email_key` →
   `process_email_bytes`.
3. Body extracted and cleaned by `SubstackAdapter`.
4. **NEW**: classifier called.
5. If transcript: opencode session opened, prompt sent, script
   extracted, body and title rewritten.
6. TTS runs on whatever `body` now contains.
7. Episode inserted in DB, feed regenerated. The episode looks like
   any other chinatalk episode aside from the title prefix.

## Error Handling

- `is_transcript` itself never raises; it returns `False` on any
  internal error.
- `generate_report` may raise (timeout, empty output, network).
  The caller wraps the entire block in `try/except Exception` and logs.
- On exception: `body` and `episode_title` retain their pre-branch
  values, so the standard reading proceeds unchanged.
- No DB writes happen before the branch resolves, so there is no
  partial state to roll back.

## Configuration

- Reuses `GEMINI_API_KEY` (already required for blog poller and
  editors).
- Reuses `OPENCODE_*` env vars consumed by `opencode_client`.
- No new env vars, no new feature flags.

## Testing

Unit:

- `pipeline/test_chinatalk_classifier.py`
  - mocks `google.genai.Client`
  - YES → True
  - NO → False
  - unparseable response → False
  - missing API key → False
  - exception during call → False
- `pipeline/test_chinatalk_writer.py`
  - mocks `opencode_client.create_session / send_prompt_async /
    wait_for_idle / get_messages / get_last_assistant_text /
    delete_session`
  - happy path: parses `<script>` and `<summary>`
  - timeout → RuntimeError
  - empty script → RuntimeError
  - session is always deleted (assert via mock)

Integration:

- `pipeline/test_processor_chinatalk.py` (new)
  - fixture chinatalk email
  - patches `is_transcript` and `generate_report`
  - transcript path: title starts with `"Report: "`, TTS input file
    contains report text
  - essay path: title and TTS input unchanged
  - classifier exception: falls back to essay path
  - writer exception: falls back to essay path

No changes to existing consumer, DB, or feed tests.

## Out of Scope

- Storing the original transcript alongside the report.
- A "force report" / "force reading" override.
- Backfilling already-published transcript episodes.
- Multi-speaker TTS.

## Open Questions

None at design time. Implementation may surface prompt-tuning needs
once we see real outputs against a few historical chinatalk
transcripts.
