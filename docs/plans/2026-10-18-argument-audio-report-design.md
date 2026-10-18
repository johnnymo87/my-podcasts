# The Argument: Report episodes from the free podcast audio

Status: design, owner-approved 2026-10-18; revised after adversarial review
(approve-with-changes) the same day. Revision awaiting owner sign-off.

## Problem

The `yglesias` feed used to turn the weekly Jerusalem Demsas / Matt Yglesias
episode of *The Argument* podcast into a spoken "Report:" briefing. The
transcript arrived as the body of a Slow Boring cross-post every Thursday; the
email path's transcript detector (`pipeline/transcript_detect.py`) fired and
`pipeline/transcript_report.py` rewrote it. 15 such Reports shipped, 2026-06-04
through 2026-09-24. Slow Boring stopped cross-posting after 2026-09-24 (and also
skipped 2026-09-10).

Measured 2026-10-18:

- The episodes now live only on *The Argument* (theargumentmag.com, Substack
  publication 5247799). The weekly episode is its own post (`type: podcast`,
  `audience: only_paid`); the free email is an accompanying essay that says
  "Paid subscribers can ... read the transcript at the end of this post". The
  owner is a free subscriber. Replaying all 75 Argument emails since 2026-05
  through `EmailProcessor` -> `SubstackAdapter.clean_body` ->
  `looks_like_transcript`: 0 transcripts.
- The anonymous Substack API for the paid podcast post exposes only the 44.5 s
  free-preview audio and its transcript. Not usable, and we do not try to get
  around the paywall.
- The full episode audio is public and free at
  `https://audioboom.com/channels/5173020.rss` (Thursdays ~07:00 UTC, 60-97 min,
  ad-supported). The four backfill items carry no transcript tags. GUIDs are
  opaque; enclosure URLs carry mutable tracking query strings. Older items keep
  `Buzzsprout-*` GUIDs (one snapshot cannot prove GUID stability over time).
- `theargument@substack.com` is not in the email-ingest worker's allowlist, so
  none of those emails reach the pipeline anyway.

Owner decisions: want the AI Report (not the full audio); source it from the free
audio via our own ASR (not a paid subscription); publish to the existing
`yglesias` feed; backfill 2026-09-10, 10-01, 10-08, 10-15.

## Approach

Discovery is a poller; execution is a pending-job row that **only the consumer
runs**. RSS -> admit row -> (consumer) download mp3 -> segment -> Gemini ASR in a
killable child -> report engine (new prompt) -> `publish_script` -> complete.

The consumer is the sole publisher. Every publish regenerates every feed from a
read-then-upload snapshot (`pipeline/feed.py:182-202`), so a second publishing
process — even one working a different GUID — can overwrite a newer feed and
drop an unrelated episode. That is the `my-podcasts-78b` lesson in another
shape. The CLI therefore enqueues/resets only; `--dry-run` runs inline and
touches no publishing state.

Rejected: synthesizing a fake .eml into the email path (unbounded redelivery,
detector on machine text). The first draft ran backfill inline from the CLI;
review showed that is a second executor.

## Gate: diarization pilot (before building the poller)

Named attribution is the point of the report ("Demsas argued... Yglesias
pushed back"), and swapped speakers produce fluent, full-length, wrong text that
no length check catches. Cross-piece identification by name is unvalidated.

Pilot on 2026-10-15 (two hosts) and 2026-10-08 (guest Jordan Schneider):
transcribe with the proposed piece size, prompt and model; spot-check by ear the
intro, a middle piece, the final piece and two piece boundaries. Owner reviews
the resulting dry-run scripts. Outcomes:

- names hold across pieces -> proceed as designed;
- names drift -> carry speaker context across pieces (pass the previous piece's
  tail + established speaker map) and re-pilot;
- still unreliable -> the report prompt avoids named attribution where the
  transcript marks identity uncertain.

Either way the ASR prompt permits `Unknown speaker` and forbids inferring names
from viewpoints.

## Components

- `pipeline/podcast_sources.py` — `PodcastSource` (name, rss_url, feed_slug,
  title label, `since` cutoff). One entry: The Argument -> `yglesias`, cutoff
  2026-10-16 UTC (exclusive of earlier pub dates). Cutoff applies to automatic
  discovery only; explicit enqueue bypasses it.
- `pipeline/podcast_transcribe.py` — mp3 -> speaker-labelled transcript, run
  inside a killable child process with a wall-clock deadline (same mechanism as
  `pipeline/tts/gemini_phase.py`), because the Gemini SDK timeout is per-read,
  not wall-clock (`pipeline/tts/asr.py:11-15`) and a trickling connection would
  otherwise stall every feed with no failure counted.
  - Download with size cap and completeness check (Content-Length when given);
    decode with warnings retained, not discarded.
  - Decoded duration must match RSS `itunes:duration` within a tolerance that
    allows dynamic ad insertion; a decodable truncated mp3 is refused.
  - Split at quiet points; send **compressed** pieces (mp3/opus via ffmpeg), not
    raw PCM: 10 min of 24 kHz s16 mono is 28.8 MB, above Gemini's 20 MB inline
    request limit. Ceiling on serialized request size asserted in code.
  - Own prompt and generation config; does NOT reuse or modify
    `tts/asr.ASR_PROMPT` (calibrated for the TTS omission verifier). Shares only
    the client helper.
  - Per-piece manifest: audio sha256, start/end bounds, finish reason, token
    usage, transcript. Require full coverage of the decoded duration and
    `finishReason == STOP` for every piece (`MAX_TOKENS` is a failure). Output
    token budget set and measured in the pilot.
  - Per-piece word-rate floor as a gross-shortfall heuristic only, not a
    completeness guarantee.
  - Peak memory and wall time measured on the longest backfill (2026-09-10,
    97 min) before choosing the deadline.
- `pipeline/podcast_writer.py` — composes `report_engine.fetch_report_text` ->
  `persist_raw_output` -> `parse_report(min_chars=2000, require_tags=True)`, the
  daily-writer pattern (`pipeline/rundown_writer.py:207-252`): a refusal unlinks
  the cached raw output so the retry regenerates. Raw output is keyed by
  transcript sha256 + prompt version, so a reset after re-transcribing never
  reuses a report built from the old transcript.
- `pipeline/transcript_report.py` — new `_ARGUMENT_AUDIO_TEMPLATE` constant and a
  separately exposed instruction builder. The email-path registry/gate is
  unchanged (the existing yglesias prompt says "It ran in the Slow Boring
  newsletter today", wrong here; one constant per source is that file's rule).
  The new prompt tells the writer to skip sponsor reads and to hedge attribution
  where the transcript marks a speaker unknown.
- `pipeline/podcast_poller.py` — fetch RSS, admit new eligible items as
  `pending` rows with their metadata frozen at admission (title, pub date,
  description, enclosure URL, duration, publication identity). Retries survive
  the item later leaving the feed.
- `pipeline/consumer.py` — discovery on its own 6 h tick with its own error
  isolation (not inside the blog poller's try); execution: at most one pending
  podcast row per loop iteration, oldest pub date first.
- `pipeline/__main__.py` — `podcast-report enqueue --source argument --guid G`,
  `podcast-report reset --guid G`, `podcast-report --dry-run --guid G`.
- `pipeline/script_processor.py` — accept an explicit publication identity
  (date_str = RSS pub date, not today; slug derived once at admission and stored
  on the row) via a shared key-building function, so the poller and
  `publish_script` cannot compute it differently.

## State and failure handling

New table `podcast_episodes` keyed by GUID: `source`, frozen RSS metadata,
`status` (`pending`/`in_progress`/`completed`/`errored`), `failure_count`,
`last_error`, `next_attempt_at`, `episode_slug`, `r2_key`.

- Consumer startup resets `in_progress` rows to `pending` (single executor, so
  any `in_progress` at startup is an interrupted run).
- Any failure fails the attempt; a partial transcript never reaches the writer.
- Backoff 6 h; 4th failure -> `errored` + one alert *attempt* (`send_alert`
  returns False on delivery failure without raising; log the outcome).
- **Publish reconciliation.** `publish_script` uploads audio, commits the
  episode row, then uploads feeds and archives (`script_processor.py:231-259`),
  so "episode row exists" does not mean "published". On retry, if a row exists
  for the frozen feed + slug: skip TTS and insert, regenerate/upload feeds, then
  complete. Tests cover failure after audio upload, after DB commit, and during
  feed upload.
- Discovery health tracked separately: consecutive RSS fetch/parse failures,
  an empty feed, or zero items with parseable dates alert after N ticks.
  Future-dated items are not admitted.

Accepted gaps: if Slow Boring resumes cross-posting transcripts, the feed gets
two Reports for one episode. A missed ad read could leak a sentence.

Distribution: the result is an unofficial AI briefing ("Report:" title prefix,
source link to the episode). Raw transcripts stay on the host under `/persist`,
never in R2 or a feed.

## Testing

Fakes only (autouse guards in `pipeline/conftest.py`). RSS parse against a
trimmed real-feed fixture (GUID identity, date, enclosure, cutoff boundary,
future dates, oldest-first); admission freezes metadata; piece coverage, non-STOP
refusal, in-order join, one failed piece fails all; truncated-but-decodable mp3
refused by duration check; passing-length partial transcript refused by
coverage; request-size ceiling; **genuinely blocked child killed at the
deadline** (real spawn, as in `test_gemini_phase_spawn.py`); state transitions
(failure count, 4th -> errored + one alert attempt, startup recovery of
`in_progress`, one row per iteration); publish reconciliation at each of the
three failure points; raw-output reuse keyed by transcript hash and invalidated
on refusal; email-path yglesias prompt unchanged.

## Rollout

0. Diarization pilot (above) on 2026-10-15 and 2026-10-08; owner reviews.
1. Build, merge, pull on host, `sudo systemctl restart my-podcasts-consumer`.
2. `podcast-report enqueue` for 2026-09-10, 10-01, 10-08, 10-15; the consumer
   publishes them one per iteration.
3. Confirm 2026-10-22 publishes unattended (expected by ~13:00 UTC).

Cost: ~$0.20-0.40 ASR + ~$0.20 TTS per episode; ~$2-3 backfill plus pilot, then
< $1/week.
