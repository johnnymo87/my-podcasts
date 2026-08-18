# Silver Bulletin transcript reports (and one report engine)

Date: 2026-08-18

## Problem

Nate Silver's Silver Bulletin newsletter sometimes carries the verbatim
transcript of a conversation between a Silver Bulletin writer and a guest.
Today those ship as a literal read: the 2026-08-17 "Why does everyone hate
data centers?" episode is 75 minutes of transcript narration. Two other feeds
already solve this — a transcript is rewritten into a spoken briefing and the
episode title gains a `Report: ` prefix — but the `silver` feed is not wired
into that path.

Adding a third feed would create a third copy of a module pair that differs
only in prompt text. It would also create a **fourth** copy of the
opencode-serve session mechanics, which already exist verbatim in
`chinatalk_writer.py`, `yglesias_writer.py`, and `report_writer.py`.

## Evidence

Detection reuses the existing deterministic detector
(`pipeline/transcript_detect.looks_like_transcript`): at least two distinct
line-start `Name:` speaker labels, each recurring at least five times. No LLM,
no network.

The question is whether that detector is safe on the `silver` feed. It was
answered by replaying **every archived Silver Bulletin email** (57 of them,
2026-04-05 through 2026-08-17) from R2 through the real production path —
`EmailProcessor(raw).parse()` then `SubstackAdapter.clean_body` then the
detector. Not through the web fetcher: `substack.resolve_post` +
`html_to_clean_text` is a different extractor on different input, and for
`only_paid` posts it sees a truncated preview, so measurements taken there do
not transfer.

Result: **4 transcripts, 53 essays, zero false positives, zero false
negatives.**

| date | verdict | speaker labels |
| --- | --- | --- |
| 2026-04-29 How much can Trump screw with the midterms? | transcript | Eli 15, Nathaniel 15 |
| 2026-07-25 What is agency — and how can you get more of it? | transcript | Nate 42, Cate 40 |
| 2026-08-03 What Trump's latest slump means for the midterms | transcript | Nate 49, Eli 47 |
| 2026-08-17 Why does everyone hate data centers? | transcript | Nate 42, Jasmine 41 |

The corpus contains the shapes that would most plausibly produce a false
positive, and none did:

- **Mailbag posts.** `SBSQ #31` (a recurring "Silver Bulletin Subscriber
  Questions" series) reads as an essay. The speaker-turn regex does match a
  bare `Q:`, so a `Q:`/`A:` mailbag was the leading suspect; this one does not
  label its answers, so only one label could recur and the two-speaker floor
  holds.
- **Poll, model, and ranking tables.** `A tour of the 7 key Senate races`,
  `PELE International Football Rankings`, `Goodbye, Iowa. Hello, Michigan.`
  produce name-shaped line-start labels (`Texas:`, `Ohio:`, `Maine:`) but each
  appears **once**, far below the five-turn floor.
- **Multi-part structured posts.** `2026 World Cup Predictions` yields
  `Part II:`, `Part III:` — again one occurrence each.

The false-positive consequence is severe and silent (a real essay is replaced
by an AI summary and shipped with no alarm), which is why this was measured on
the whole archive rather than a sample.

### The essay preamble

Silver transcript posts open with original prose before the conversation
starts. That prose is the highest-value content in the post, so its size
decides whether one whole-post report is acceptable or whether the body must be
split. Measured:

| post | body chars | preamble chars | share | preamble words |
| --- | --- | --- | --- | --- |
| Trump screw midterms | 29,132 | 1,996 | 7% | 331 |
| What is agency | 48,731 | 5,328 | 11% | 931 |
| Trump slump | 57,784 | 2,483 | 4% | 412 |
| data centers | 72,230 | 2,962 | 4% | 510 |

4–11% — the same order as the "short editor's framing" the Yglesias prompt
already anticipates. One report over the whole post is therefore the chosen
behavior, with the Silver prompt instructing proportionate coverage of the
opening essay. Splitting the body at the first speaker turn (essay read
verbatim, conversation reported) was considered and rejected as buying little
at the cost of a boundary heuristic that can misfire.

Note also that 2026-04-29 is Eli McKown-Dawson and Nathaniel Rakich with **no
Nate Silver present**. The prompt must not hardcode "Nate and a guest."

## Design

### `pipeline/report_engine.py` (new, shared)

Owns the opencode-serve mechanics that exist in four places today:

```
ReportOutput                      # frozen dataclass: script, summary
_extract_script(text)             # LONGEST <script> block, not the first
_extract_summary(text)
run_report_prompt(instruction, *, label) -> ReportOutput
    # create_session / send_prompt_async / wait_for_idle(timeout=900) /
    # get_messages / get_last_assistant_text / delete_session in finally
    # RuntimeError on timeout, on empty script, and on an implausibly short
    # script. `label` interpolates into those messages.
```

Two of those behaviors are **fixes, not ports**. Commit 39589e3 (2026-08-18)
established that a non-greedy first-match `<script>` extraction can select a
3-character planning placeholder over the real script — it shipped a 2636-byte
FP Digest episode — and that emptiness is the wrong guard, plausibility is the
right one. That fix landed only in `rundown_writer.py`. `chinatalk_writer.py`,
`yglesias_writer.py`, and `report_writer.py` still carry the defective version.
Consolidating is the occasion to fix all three at once: `report_engine` takes
the longest block and enforces the same minimum-length floor. The transcript
path needs this at least as much as the daily path — there, a placeholder
script would replace an 80-minute transcript and be narrated as the episode.

### `pipeline/transcript_report.py` (new)

```
TRANSCRIPT_FEEDS: dict[str, str]   # feed_slug -> prompt template
    "chinatalk": <existing template, byte-identical>
    "yglesias":  <existing template, byte-identical>
    "silver":    <new>

build_report_prompt(*, body, subject, feed_slug) -> str
generate_report(*, body, subject, feed_slug) -> ReportOutput
    # delegates to report_engine.run_report_prompt(label=feed_slug)
maybe_rewrite_transcript(*, body, title, feed_slug, subject_raw) -> (body, title)
```

The gate is unchanged in behavior from the two it replaces:

- `feed_slug not in TRANSCRIPT_FEEDS` → passthrough.
- `not looks_like_transcript(body)` → passthrough.
- otherwise generate; on any exception, log and **re-raise**. A confirmed
  transcript never degrades to a literal read. The exception leaves
  `process_email_bytes`, the consumer does not ack, and the email is
  redelivered.
- on success → `(report.script, f"Report: {title}")`.

Prompts stay separate string constants so a Silver prompt edit cannot reach the
ChinaTalk feed. That is the only shared-blast-radius concern the merge creates,
and constants contain it.

### `pipeline/report_writer.py`

Keeps its style registry and its two templates; its `generate_report`,
`_extract_script`, `_extract_summary`, and `ReportOutput` delegate to
`report_engine`. This is what takes the mechanics from four copies to one — a
consolidation that stopped at three would have left the same
second-drifted-implementation shape that caused `my-podcasts-78b`.

### `pipeline/processor.py`

The two sequential calls at lines 124–136 (`maybe_rewrite_yglesias` then
`maybe_rewrite_chinatalk`, mutually exclusive by feed gate) collapse into one
`maybe_rewrite_transcript` call at the same wiring point: after
`adapter.clean_body`, before TTS.

### Deletions

`chinatalk_writer.py`, `chinatalk_report.py`, `yglesias_writer.py`,
`yglesias_report.py`, `yglesias_filter.py`, and their five test files.
`transcript_detect.py` is untouched.

## Testing

- **Golden equivalence.** For a fixed fixture, assert the new
  `build_report_prompt(feed_slug="chinatalk"|"yglesias")` output is byte-equal
  to the retired modules' output. This is what makes the migration provably
  behavior-preserving for two live feeds.
- **Engine.** Session lifecycle including `delete_session` in `finally`;
  `wait_for_idle(timeout=900)`; timeout, empty-script, and too-short-script
  RuntimeErrors; longest-block extraction (the 39589e3 regression, now covering
  the transcript path too); missing-tag fallback.
- **Prompt build**, parametrized over the three slugs: each contains its
  feed-specific marker, the subject, and the body.
- **Gate:** unregistered slug (`levine`) passthrough; essay passthrough;
  transcript rewritten with the `Report: ` prefix; generation failure
  propagates.
- **Real-corpus regression fixtures**, taken from **email-cleaned** bodies, not
  web text: one trimmed Silver transcript asserting `True`, one Silver essay
  asserting `False`, and the SBSQ mailbag asserting `False` (the nearest miss
  in the archive).
- **Processor source assertion** updated to require `maybe_rewrite_transcript`
  **and** the absence of the retired names, replacing the three existing
  `inspect.getsource` assertions.

## Known gaps, deliberately not closed here

- **Unbounded redelivery on deterministic failure.** The email path has no
  failure counter, backoff, or `errored` state, unlike daily jobs. A body that
  fails generation every time redelivers forever at up to 900s per attempt.
  This is pre-existing for `chinatalk` and `yglesias`; `silver` adds a third,
  roughly monthly, source of such messages. The queue's `max_retries`/DLQ
  configuration is Cloudflare dashboard-side and not visible in
  `workers/email-ingest/wrangler.toml`. Tracked separately.
- **The gate is route-tag-dependent.** A Silver email that arrives without a
  recognized route tag falls to the `general` preset and never reaches the
  transcript path. Pre-existing.
- **AGENTS.md correction.** The Yglesias section currently claims the path
  "fails safe: any exception in detection or generation falls back to the
  standard reading." The code re-raises. The doc merge fixes this rather than
  propagating it.
