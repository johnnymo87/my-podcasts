# Pipeline Agent Guide

Quick start and incident-response guide for the two daily podcasts: The Rundown and Foreign Policy Digest.

## Quick Start

1. Check the consumer:
   - `sudo systemctl status my-podcasts-consumer --no-pager`
2. Check recent daily jobs:
   - `uv run python -c "import sqlite3; conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3'); conn.row_factory = sqlite3.Row; [print(dict(r)) for r in conn.execute(\"SELECT 'fp-digest' AS feed, id, date_str, status, process_after, failure_count, last_error FROM pending_fp_digest ORDER BY created_at DESC LIMIT 3\").fetchall()]; [print(dict(r)) for r in conn.execute(\"SELECT 'the-rundown' AS feed, id, date_str, status, process_after, failure_count, last_error FROM pending_the_rundown ORDER BY created_at DESC LIMIT 3\").fetchall()]"`
3. Check recent episodes:
   - `uv run python -c "import sqlite3; conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3'); conn.row_factory = sqlite3.Row; [print(dict(r)) for r in conn.execute(\"SELECT title, feed_slug, pub_date, r2_key FROM episodes WHERE feed_slug IN ('fp-digest','the-rundown') ORDER BY created_at DESC LIMIT 6\").fetchall()]"`
4. Check today’s consumer logs:
   - `journalctl -u my-podcasts-consumer --since today --no-pager`

## Relevant Skills

- Whole-pipeline monitoring: `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
- Stuck, delayed, or errored daily jobs: `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`
- Resetting errored jobs via CLI: `.opencode/skills/resetting-errored-daily-jobs/SKILL.md`
- Rundown-specific collection / writer behavior: `.opencode/skills/operating-things-happen-digest/SKILL.md`

## Current Hardening

- `pipeline/rundown_writer.py`, `pipeline/fp_writer.py` (identical hardening)
  - 900-second opencode writer timeout
  - both compose `pipeline/report_engine.py`: `fetch_report_text` for the session, then `parse_report` for extraction and every publish-boundary refusal. They call the two halves separately (rather than `run_report_prompt`) precisely so raw output can be persisted between them.
  - raw model output persisted to `work_dir/raw_writer_output.txt` before parsing; retries reuse this file and skip the model call. A parse failure **unlinks** it so the next retry regenerates instead of looping on the same broken content — note the `except` catches `RuntimeError` only, which is every refusal the engine raises today, and a test pins that.
  - refusals at the writer boundary: empty script, script below `min_chars=500`, leaked `<script>`/`<summary>`/`<covered>` markup, and (`require_tags=True`) a reply carrying no *trustworthy* `<script>` markup — a bare mention of the tag in the model's reasoning does not count, see `AGENTS.md`. A refusal fails the job, so the consumer backs off and the retry-exhaustion alert fires — that is the intended outcome, not a degraded episode.
  - the raw-output write is atomic (`report_engine.persist_raw_output`), because a truncated file re-parses into a clean-looking **half script** that passes every refusal
  - `min_chars=500` here is **re-derived, not copied** from the transcript path's 2000: this failure path is *bounded* (backoff → `errored` → alert), which is the regime 500 was originally derived for. The transcript path's unbounded redelivery is what forced 2000 there.
  - a second floor, `_validate_script_length`, still runs at the TTS boundary in `things_happen_processor.py`/`fp_processor.py`. The double floor is intended: that one also guards paths that bypass the writer entirely (`--script-file`, `publish-script`).
  - **The Rundown only:** refuses to generate when no section has any article text. FP Digest has no equivalent guard yet — that gap is bead `qd5`.
- `pipeline/things_happen_collector.py`, `pipeline/fp_collector.py`
  - successful collection writes `collection_done.json`
- `pipeline/consumer.py`
  - retries reuse prior collection when `collection_done.json` and `plan.json` exist
  - writer failures back off instead of retrying every 10 seconds
- `pipeline/db.py`
  - bounded retry backoff: 1m, 2m, 4m, 8m, then 15m cap
  - after about 12 hours of retry budget, daily jobs become `status='errored'`

## TTS Renderer

`pipeline/tts/` (`render_episode`) replaced `ttsjoin`: chunk the text, fetch OpenAI PCM per chunk, one mp3 encode. Used by every processor, `publish-script`, and the blog poller.

- **Completed renders are cached 14 days** under `/persist/my-podcasts/tts-cache/<key>/`, keyed by exact TTS text + the primary/fallback config + `RENDERER_VERSION` (+ `verify.VERIFIER_POLICY` for a Gemini primary). So `jobs reset` on an unchanged script **replays the cached audio**. To force a fresh render of a bad episode, delete that cache entry — its key is the manifest's `cache_key` field.
- **Per-attempt manifests** at `/persist/my-podcasts/tts-renders/<feed>/<episode_id>-<timestamp>.json` (feed and id sanitized to `[A-Za-z0-9._-]` in the filename; the raw id is in the manifest's `episode_id` field): `status` is `rendered`/`cache_hit`/`failed`, with per-chunk attempts and errors. 60-day retention. Manifest/cache failures never fail a render.
- **Retries:** up to 3 attempts per chunk (sleeping 2s, then 8s between them) on retryable OpenAI errors; SDK retries are off. A failed render raises into the existing job retry/backoff path unchanged.
- **`FEED_VOICES` in `pipeline/tts/config.py` is the only place a feed's voice lives.** Presets, blog sources and the daily processors carry none; every call site asks `tts.resolve_render_config(feed_slug, ...)`. An unknown slug gets `nova`. A test (`pipeline/test_feed_voices.py`) pins that every routable slug is a key.
- **Overrides force OpenAI:** `TTS_VOICE`/`TTS_MODEL` env on the email path, `publish_script(voice=)`, and the CLI `--voice` flags. A field the override leaves out comes from the feed's OpenAI config. An explicitly empty override is a `ValueError`.
- **`publish_script` and the CLI still default to `nova` on every feed** (fp-digest included), exactly as before, until `my-podcasts-9p3.7` changes the default to fall through to `FEED_VOICES`.
- **Every feed is OpenAI today.** `GeminiProvider` exists (`providers.py`) but `render_episode` refuses a Gemini primary (`ValueError`, before any cache or manifest work) until `my-podcasts-9p3.11`.
- **Cache entries carry provenance:** `result.json` is schema 2 (`requested`, `rendered`, `verification`, `fallback_reason`). An entry that fails validation is a miss, and a hit whose `rendered` leaf is neither the request's primary nor its fallback is purged and re-rendered. The manifest's `rendered_config` records what actually produced the audio (`null` on a failed render).
- **`RENDERER_VERSION` 2 made the cache cold on deploy.** A `jobs reset` right after that deploy re-buys the audio once; later resets replay the cache as before.
- **Gemini errors (for when it is wired):** an invalid key is HTTP 400 `API_KEY_INVALID`, not 401, so any 400 is fatal (not retried). `GEMINI_API_KEY` is stripped and must be ASCII-printable, else the provider fails fatal without echoing it. A 429 is fatal only with a daily-quota `QuotaFailure`; anything else is retried.
- **Tests never spend money:** the autouse `_block_real_openai_tts` and `_block_real_gemini_tts` guards in `pipeline/conftest.py` fail any test that builds a real client/session.
- **Known gap:** a render that fails partway re-buys the earlier chunks on retry (only completed renders are cached) — bead `my-podcasts-9p3.10`.

### Verifier (T2)

Offline large-omission detector for rendered audio: ASR the audio, align the transcript against the script, report long unmatched spans and recall. **Not wired into `render_episode` yet** (T3b, `my-podcasts-9p3.11`, does that); today it is the `tts-verify` CLI and a library.

- **Claimed scope: large omissions only.** It does not detect changed numbers, negations, repetitions, or added speech.
- **Modules** (`pipeline/tts/`):
  - `normalize.py` — script and transcript to comparable word tokens, with a bounded number grammar (unsupported forms stay as digits).
  - `verify.py` — `difflib` alignment, anchored spans, recall, `project_chunks`, and `verify_audio`, the per-chunk entry point T3 will call. `unavailable` is never a pass: it needs explicit evidence (ASR error/timeout, no candidates, finish reason other than STOP, a transcript with no word tokens), not a length heuristic.
  - `asr.py` — `GeminiTranscriber` (audio only, never sees the script; SDK retries off; context manager, thread-safe).
  - `segment.py` — ffmpeg decode and quiet-point splitting into ~5-minute pieces, CLI only.
- **Usage:** `uv run python -m pipeline tts-verify --audio X.mp3 --script S.txt --save-transcript T.json --json R.json`. Re-tune without paying for ASR again: `... --audio X.mp3 --script S.txt --transcript T.json --min-span-words 20` (also `--anchor-min`, `--max-span-ratio`, `--recall-floor`). `--script` must be the exact TTS input text.
- **Exit codes:** 0 pass, 1 omission, 3 verification unavailable (including ffmpeg failures), 4 unexpected error. A crash never exits 1, so it cannot be scored as a finding.
- **Saved transcripts are replayable evidence** (audio sha256, script sha256, model, prompt version, per-segment bounds/finish reason/tokens/text). The write happens as soon as ASR completes, atomically, and refuses to overwrite an existing file without `--force`. On replay the `audio_sha256` must match `--audio` (hard error); a `script_sha256` mismatch only prints a WARNING, since replaying against an edited script is a legitimate calibration trick. A plain-text `--transcript` is accepted and reported as `asr.source: external`.
- **Per-chunk numbers are projected** from one whole-episode alignment; they are diagnostic and differ from what T3 sees when it verifies each synthesized chunk alone (the production-equivalent check is T5's). `est_start_s` is a token-proportional estimate of where a chunk starts in the audio, not a timestamp; it drifts after an omission or a long pause.
- **Thresholds are placeholders** until T5 calibrates them on real audio-level cuts (`my-podcasts-9p3.5`). Reports echo the thresholds used.
- **`VERIFIER_VERSION` must be bumped** on any change to normalization, alignment, or default thresholds. The ASR half is `asr.ASR_POLICY` (model, prompt version, generation config); bump `ASR_PROMPT_VERSION` on any prompt text change. `cache.cache_key` folds `verify.VERIFIER_POLICY` (both halves, also in every verdict and report as `verifier_policy`), not just the version, for a Gemini primary; an OpenAI primary never imports it.
- **ASR costs money** (Gemini audio input). Tests run offline: the autouse `_block_real_gemini_asr` guard in `pipeline/conftest.py` fails any test that builds a real Gemini client.
- **The SDK timeout is per-read, not wall-clock.** A server that trickles bytes can exceed it many times over; the real bound has to be a kill at the caller (T3's child process).

## How To Read Job State

- `status='pending'`
  - job is still eligible to run when `process_after <= now`
- `status='completed'`
  - job finished successfully
- `status='errored'`
  - retry budget was exhausted; consumer will no longer pick it up automatically
- `failure_count`
  - number of consecutive failures on the current job row
- `last_error`
  - truncated most recent failure message; useful for classifying upstream vs TTS vs publish failures

## Incident Checklist

If The Rundown or FP Digest is missing, late, or stuck:

1. Confirm the consumer is running.
2. Check the pending job row for `status`, `failure_count`, and `last_error`.
3. Check recent episodes to see whether the episode actually published late.
4. Read consumer logs for writer-stage failures, TTS failures, or publish failures.
5. If the job is still `pending`, compare `process_after` to current time before assuming it is stuck.
6. If the job is `errored`, use the admin reset CLI: `.opencode/skills/resetting-errored-daily-jobs/SKILL.md`.
