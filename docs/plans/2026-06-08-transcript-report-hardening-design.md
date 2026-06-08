# Transcript Report Path Hardening — Design

Date: 2026-06-08

## Problem

On 2026-05-26 the ChinaTalk "WarTalk: Ukraine War Tactical Update" email was
published as a literal ~68-minute transcript read instead of a spoken
*Report*. Root cause: Google shut down the `gemini-3.1-flash-lite-preview`
endpoint on 2026-05-25, so every Gemini call 404'd.
`pipeline/chinatalk_classifier.is_transcript` catches **all** exceptions and
returns `False`, so the report path was silently skipped and the listener got
the verbatim transcript with no signal. (Tracked as `my-podcasts-8tw`.)

Two structural weaknesses make this class of failure recur:

1. **Detection depends on a remote LLM.** ChinaTalk transcript detection is a
   Gemini call. Any transient or permanent Gemini outage (404 / network / 5xx
   / rate-limit / missing key) collapses to "not a transcript" and the episode
   ships as a literal read.
2. **Generation failure fails *toward* a literal read.** For a body we *know*
   is a transcript, if the opencode briefing writer fails, both
   `maybe_rewrite_chinatalk` and `maybe_rewrite_yglesias` swallow the exception
   and return the raw transcript for TTS — an 80-minute verbatim read with no
   alert.

## Goals

- Remove the remote-API dependency from ChinaTalk transcript **detection** by
  converging on the deterministic, content-only detector already proven for
  Yglesias.
- Stop silently shipping a literal read when a body is a *confirmed* transcript
  but the briefing **generation** fails: raise instead, so the consumer leaves
  the queue message unacked and the email self-heals on redelivery.
- Keep essays behaving exactly as today (standard reading).

## Non-goals

- No new feed, preset, DB schema, env var, or feature flag.
- No transient-vs-permanent error taxonomy or inline retry loop; we rely on the
  existing queue redelivery for retry.
- No Cloudflare Queues DLQ/max-retries tuning (noted as a limitation below).
- No backfill of historical episodes (the WarTalk episode was already
  reprocessed manually).

## Approach

### 1. Shared deterministic detector

Extract the speaker-label logic out of `pipeline/yglesias_filter.py` into a new
neutral module `pipeline/transcript_detect.py`:

```python
def looks_like_transcript(body: str) -> bool: ...
```

Same rule as today's `is_argument_transcript`: count line-start speaker labels
(1–4 capitalized words, allowing `.`/`'`/`-`, followed by `:` and whitespace);
fire only when at least 2 distinct labels each recur at least 5 times
(`_MIN_SPEAKERS = 2`, `_MIN_TURNS_PER_SPEAKER = 5`). The regex, the apostrophe
handling, and the import-time sanity assert move with it.

`pipeline/yglesias_filter.is_argument_transcript` becomes a thin delegator to
`looks_like_transcript`, preserving its existing imports, tests, and the
`AGENTS.md` reference.

Validated: `looks_like_transcript` already returns `True` on the real WarTalk
`.eml` (qualifying speakers: Rob Lee ×21, Justin ×7, Tony Stark ×5).

### 2. ChinaTalk detection goes deterministic; delete the Gemini classifier

- `pipeline/chinatalk_report.maybe_rewrite_chinatalk` calls
  `looks_like_transcript(body)` instead of `is_transcript(body, subject_raw)`.
- Delete `pipeline/chinatalk_classifier.py` and
  `pipeline/test_chinatalk_classifier.py`. Nothing else imports
  `is_transcript` (verified before deletion).
- The `subject_raw` argument is no longer used for detection but stays in the
  `maybe_rewrite_chinatalk` signature (it is still passed to `generate_report`).

### 3. Raise-on-generation-failure (both feeds)

In both `maybe_rewrite_chinatalk` and `maybe_rewrite_yglesias`:

```python
if feed_slug != "<feed>":
    return body, title
if not looks_like_transcript(body):   # genuine essay
    return body, title
try:
    report = generate_report(body=body, subject=subject_raw)
except Exception:
    logger.exception("<feed> report generation failed for a confirmed "
                     "transcript; not shipping a literal read")
    raise
return report.script, f"Report: {title}"
```

- Detection `False` → standard reading (unchanged; correct for essays).
- Detection `True` + generation raises → log ERROR and **propagate**. This
  bubbles out of `process_email_bytes`; the consumer's per-message handler
  (`consumer.py:256`) does not ack on exception, so Cloudflare Queues
  redelivers the message after the visibility timeout and it reprocesses once
  opencode recovers.

This reverses the prior "rather one over-long episode than nothing" fallback
**only for the confirmed-transcript case**: a `True` detection means the body
is *not* an essay, so reading it verbatim is precisely the bad outcome. Essays
(detection `False`) are never at risk of being dropped or delayed.

## Error Handling

- `looks_like_transcript` is pure and deterministic; the only way it raises is
  a programmer error, which should surface loudly (no swallow).
- `generate_report` failures (timeout, empty output, network) now propagate for
  confirmed transcripts rather than degrading to a literal read.
- No DB writes occur before the rewrite resolves, so a propagated exception
  leaves no partial state. The message is reprocessed cleanly on redelivery.

## Known Limitation

Raise-on-failure relies on queue redelivery. The inbox is a Cloudflare Queues
**pull** consumer (HTTP pull/ack; no DLQ configured in `wrangler.toml`). A
*permanent* upstream outage would cause the message to redeliver until
Cloudflare's max-retries are exhausted, after which it is dropped. This is
acceptable: it is strictly louder than today's behavior (which ships wrong
content immediately and acks it), and a permanent outage requires a code fix
anyway — after which an un-exhausted message self-heals automatically.

## Testing

- `pipeline/test_transcript_detect.py` (new):
  - synthetic 2-speaker ≥5-turn body → True
  - essay (no repeated `Name:` line-starts) → False
  - single speaker, many turns → False
  - two speakers <5 turns each → False
  - empty body → False
  - a ChinaTalk-shaped transcript fixture → True
  - a ChinaTalk essay fixture → False (false-positive guard)
- `pipeline/test_yglesias_filter.py`: keep; `is_argument_transcript` still
  behaves identically via delegation.
- `pipeline/test_chinatalk_report.py` (updated):
  - non-chinatalk feed → passthrough
  - essay (no speaker labels) → passthrough (now exercises the real detector,
    no classifier mock)
  - transcript + generation OK → Report
  - transcript + generation raises → **propagates** (was: fallback)
  - drop `test_classifier_failure_falls_back_to_reading` (no classifier)
- `pipeline/test_yglesias_report.py` (updated):
  - transcript + generation raises → **propagates** (was: fallback)
- Delete `pipeline/test_chinatalk_classifier.py`.
- Full `uv run pytest` green.

## Docs

Update the "ChinaTalk Transcript Report Path" section of the root `AGENTS.md`:
detection is now deterministic (shared with Yglesias, no Gemini dependency),
and a generation failure on a confirmed transcript raises (queue retry / no
silent literal read) rather than falling back to a standard reading.

## Open Questions

None at design time.
