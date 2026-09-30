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
- **Every feed is OpenAI today; no feed is Gemini yet.** `render_episode` can render a Gemini primary (T3b, below), but nothing in `FEED_VOICES` selects one, so production audio is unchanged. A standalone script that calls `render_episode` with a Gemini primary needs an `if __name__ == "__main__":` guard: the Gemini phase runs in a `spawn`ed child, and a spawned child re-imports the launching script's `__main__` (the consumer, run as `python -m pipeline`, is exempt).
- **Cache entries carry provenance:** `result.json` is schema 2 (`requested`, `rendered`, `verification`, `fallback_reason`). An entry that fails validation is a miss, and a hit whose `rendered` leaf is neither the request's primary nor its fallback is purged and re-rendered. The manifest's `rendered_config` records what actually produced the audio (`null` on a failed render).
- **`RENDERER_VERSION` 2 made the cache cold on deploy.** A `jobs reset` right after that deploy re-buys the audio once; later resets replay the cache as before.
- **Gemini errors:** an invalid key is HTTP 400 `API_KEY_INVALID`, not 401, so any 400 is fatal (not retried). `GEMINI_API_KEY` is stripped and must be ASCII-printable, else the provider fails fatal without echoing it. A 429 is fatal only with a daily-quota `QuotaFailure`; anything else is retried.
- **Tests never spend money or touch `/persist`** (autouse guards in `pipeline/conftest.py`):
  - `_block_real_openai_tts`, `_block_real_gemini_tts`, `_block_real_gemini_asr`, `_block_real_genai_clients` (any `google.genai.Client`; a loopback `base_url` is let through), `_block_real_telegram_posts` and `_block_real_article_fetches` each **record** a refusal and fail the test at teardown, so code that swallows the `AssertionError` (`send_alert`, the Gemini phase's `child_error`) cannot turn an attempted real call into a green test. A test that provokes a guard on purpose asserts on, then clears, `_guard_violations`.
  - `_no_api_keys` **deletes** `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY` and `OPENAI_API_KEY` from the test environment (a dummy key is worse than none: it lets code that early-exits without a key proceed to a real request). A test that needs a key sets its own; the spawned child of the Gemini-phase tests gets a dummy from `deny_network`.
  - `_block_persist_writes` refuses any write-open, `mkdir`, `sqlite3.connect`, write-flag `os.open` or `touch` under `/persist`; `@pytest.mark.allow_persist` opts a test out (unused today). `SCRIPT_ARCHIVE_ROOT`, the TTS manifest/cache dirs and `MY_PODCASTS_STATE_DB` are redirected into `tmp_path`. Known gaps are in the guard's docstring.
- **Known gap:** a render that fails partway re-buys the earlier chunks on retry (only completed renders are cached) — bead `my-podcasts-9p3.10`.

### Verifier (T2)

Offline large-omission detector for rendered audio: ASR the audio, align the transcript against the script, report long unmatched spans and recall. `render_episode` calls `verify_audio` per chunk inside the Gemini phase (T3b, below); it is also the `tts-verify` CLI and a library.

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

### Gemini phase and OpenAI fallback (T3b)

A Gemini primary renders through `pipeline/tts/gemini_phase.py`; **no feed uses it yet**. Design: `docs/plans/2026-09-30-gemini-tts-design.md` (Amendments).

- **Flow.** `render_episode` chunks the text with `GeminiProvider.max_chars` and runs `run_gemini_phase` in a `spawn`ed child with a 360 s budget (`GEMINI_BUDGET_SECONDS`). The child synthesizes each chunk (up to 4 in parallel) and verifies it by ASR. **Any problem discards all Gemini audio**, and the parent renders the *whole* text with the OpenAI fallback in-process (no verification; no episode ever mixes two voices). With `fallback=None` a failed phase is a `TTSRenderError` ("Gemini phase failed: <reason>") and no alert.
- **The bound.** The parent kills the child at the absolute deadline: a render is bounded by budget + one 0.2 s poll + one 5 s reap. Child startup (about 1.4 s) is inside the budget. Per chunk: one 3-call counter covers transient retries and the single omission re-render (backoff 2 s then 8 s; ends early if a sibling chunk fails). A second omission, unavailable ASR, or a fatal error fails the phase at once.
- **The parent trusts nothing the child says.** `result.json` is validated against the files (indices `0..n-1`, names, sizes, even and non-empty, sha256) before any audio is used; a mismatch is `invalid_result`.
- **Fallback reasons (closed set):** `fatal`, `exhausted`, `deadline`, `asr_unavailable`, `second_omission`, `child_error`, `child_no_result`, `invalid_result`, `spawn_failed`, `runner_error`. The last is parent-side only (a child that reports it gets `invalid_result`): an exception escaped `run_gemini_phase` itself (a runner bug). It is converted to a failed phase (detail = capped traceback, `chunks` empty, totals unknown), so it falls back and alerts like any other reason; with no fallback it raises `TTSRenderError` chained from the exception. `KeyboardInterrupt`/`SystemExit` still propagate.
- **Cache.** A Gemini render is stored `provider=gemini`, `verification=passed`. A fallback render is stored `provider=openai`, `verification=not_run_openai`, `fallback_reason=<reason>`, so `jobs reset` **replays the fallback audio** without re-running Gemini or re-alerting; delete the entry (manifest `cache_key`) to give Gemini another chance. `RENDERER_VERSION` stays `"2"`.
- **Manifest.** New fields on every manifest: `gemini_phase` (`null` for an OpenAI primary), `fallback_reason`, `alert_sent`. `gemini_phase` holds `outcome` (`ok`/`failed`), `reason`, `detail` (for `child_error` and `runner_error` this includes a traceback), `failed_chunk`, `budget_s`, `elapsed_s`, `spawn_s` (`Process.start()` only, about 1 ms; `null` after a `runner_error`) and `child_started_s` (the real startup, about 1.4 s), `chunks` (per-chunk attempt records: each synth and ASR request with status, error kind, elapsed, finish reason, tokens, and the attempt's outcome), `tokens` (`synth_prompt`, `synth_audio`, `asr_input`, `asr_output`, `asr_thinking`; **`null` if any contributing count is genuinely unknown** - a request that was killed, still in flight, or errored without usage, a chunk with no readable progress, or a runner error makes the total unknown. A *completed* call that reports no thinking count counts as 0: the model reported none. The raw per-attempt value in `chunks` stays as reported), `audio_seconds_generated` (every Gemini attempt, discarded or not; **`null` if any chunk's progress is missing, unreadable or malformed**, or after a `runner_error`) and `audio_seconds_used` (0 unless the phase was ok). The top-level `chunks` is **only the audio that shipped** (the Gemini chunks on success, the OpenAI chunks after a fallback).
- **The alert** (Telegram General via `pipeline.alerts.send_alert`): one per fallback, sent by the parent after the fallback attempt and also when it fails, never on a cache hit, never with `fallback=None`, and not with `notify_fallback=False` (`publish-script --dry-run` passes it). Text: `TTS fallback: <feed> <episode_id>: Gemini <model>/<voice> <reason> -> OpenAI <voice> rendered` (or `FAILED: <error>`). Delivery is bounded twice over: alerts go to **one** lazily started module-level daemon worker through a queue of at most `ALERT_QUEUE_MAX = 8`, and a render waits at most `ALERT_WAIT_SECONDS = 12.0` for its own alert. A stuck sender therefore costs one thread, ever, however many renders fall back. `alert_sent` is `true`, `false`, `"timeout"` (**delivery unknown**: queued but not finished in time; it may still go out late), `"dropped"` (**not sent**: the queue was full behind a stuck sender), or `null` (none attempted). Timed-out and dropped alerts are logged in full (journald has the text). Nothing about the alert can raise into the render: building the text, starting the worker and `send_alert` itself all degrade to `false`.
- **Reading a fallback.** Open the manifest: `fallback_reason` says why; `gemini_phase.reason`/`detail`/`failed_chunk` say where; `gemini_phase.chunks[*].attempts` show which request was in flight (an ASR record stuck at `started` with `null` tokens means the kill landed mid-request). **Telemetry never costs a render:** `gemini_phase` is built from child-written progress files, read defensively; if building it fails the record degrades to the basic fields with `tokens` all `null` and a `telemetry_error` note. A `deadline` usually means a hung request, `fatal` a 4xx/quota/key problem, `asr_unavailable` the verifier could not hear the audio, `second_omission` the model dropped text twice. If the OpenAI fallback also failed, the job retry path reports as it always did - **and each retry re-runs the Gemini phase (up to 6 minutes) and re-alerts**, because only completed renders are cached (the known gap above, `my-podcasts-9p3.10`); a day of retries against a dead Gemini and a failing OpenAI costs a phase and an alert per attempt. A `KeyboardInterrupt`/`SystemExit` leaves a `failed` manifest and propagates; it never falls back. A broken `google.genai`/`verify` import surfaces while computing the cache key, as a failed manifest and the `ImportError` (deliberately not a fallback: nothing has run, and a broken install should be loud).
- **Scratch.** The child works in `gemini-phase-*` directories under the system temp dir, removed after every phase; dirs older than 6 hours (from a killed parent) are swept at the start of the next phase.
- **The episode reaches the child as a file.** The parent writes `input.json` (chunks and the Gemini leaf) into the scratch dir before starting the child, and `Process.start()` carries only a few hundred bytes. (Through the spawn pipe, a child that stopped reading would have blocked the parent inside `start()`, past the deadline.) The child validates the file; unusable input is a `child_error`.
- **Tests.** `test_gemini_phase.py` (in-process child and runner), `test_gemini_phase_spawn.py` (real spawn: blocked synth/ASR killed at the deadline, early fatal, parent death, `KeyboardInterrupt`; about 30 s), `test_render_gemini.py` (wiring, with canned `PhaseOutcome`s).

### Audition (T4)

`python -m pipeline tts-audition` renders one script through the feed's current OpenAI voice (the baseline) and every requested Gemini model x voice, into local mp3s, so the owner can compare by ear. Logic: `pipeline/tts/audition.py`; the CLI is a thin wrapper.

- **Local only.** No publish option; nothing touches R2, the DB, feeds, the render cache or alerts (`audition.py` imports only `pipeline.tts.*`, pinned by a test). Uploading a set for someone to listen to is a manual step.
- **A failure is never substituted.** Gemini variants render with no OpenAI fallback, so a Gemini problem is a `FAILED` line (exit 1), not a silent OpenAI file. Files are named `<feed>--<provider>--<model>--<voice>.mp3` from what `render_episode` reports it rendered; a failed variant leaves no mp3 (with `--force` it also deletes the stale one).
- **Outputs** in `--out-dir`: the mp3s, `script.txt` (the exact text rendered, after any `--max-chars` cut), `manifests/` (the normal per-attempt manifests) and `summary.json` (rewritten after every variant; per variant: requested/rendered config, status, error, audio seconds, wall seconds, per-chunk verify verdicts and recall, token totals). `summary.json` has `complete: false` (plus the `planned` variants) until the last variant finishes, so an interrupted run is recognizable. Existing targets, `script.txt` or `summary.json` are refused without `--force`; an `--out-dir` holding any other `*.mp3` is refused even with it (use a fresh directory). Exit codes: 0 all ok, 1 a variant FAILED, 2 usage error/refusal, 4 unexpected crash.
- **Example:** `uv run python -m pipeline tts-audition --feed the-rundown --script script.txt --voices Kore,Puck --models gemini-3.8-flash-lite-tts --max-chars 3000 --out-dir /persist/my-podcasts/tts-eval/t4/run1` (`--no-openai` skips the baseline; `--style ""` renders with no style prompt; default style `calm, measured news anchor`).
- **Cost.** Needs `GEMINI_API_KEY` (and `OPENAI_API_KEY` for the baseline); a missing one is a usage error before anything renders. Gemini variants also run per-chunk ASR verification, exactly as production would: roughly $1/hour of Flash audio and $0.7/hour of Flash-Lite synthesis, plus ASR.

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
