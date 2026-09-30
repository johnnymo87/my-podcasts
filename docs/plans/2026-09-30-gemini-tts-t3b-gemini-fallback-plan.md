# T3b — bounded Gemini phase, per-chunk verification, whole-episode OpenAI fallback — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** `render_episode` can render a Gemini primary. Synthesis and verification run in a spawned
child process that is killed at a hard 6-minute deadline. Any Gemini problem discards all Gemini
audio and renders the whole episode with the OpenAI fallback in the parent. **No feed is flipped:**
every `FEED_VOICES` entry stays OpenAI, so production audio does not change.

**Bead:** `my-podcasts-9p3.11` (epic `my-podcasts-9p3`), plus `my-podcasts-9p3.12` folded in.
**Design:** `docs/plans/2026-09-30-gemini-tts-design.md`. Its "Amendments (T3 consult)" section
overrides the inline text. This PR adds one more amendment (Task 5).
**Oracle detail:** `/persist/my-podcasts/tts-eval/t3/t3b-oracle-extras.md`. REST facts:
`/persist/my-podcasts/tts-eval/T3-FACTS.md`.

**Commands:**
- Single file: `uv run pytest pipeline/tts/test_gemini_phase.py -q`
- Full suite: `uv run pytest -q -x` (~6 min, ~1270 tests; use a 900 s timeout)
- Lint: `uv run ruff check . && uv run ruff format --check .`

---

## Decisions — do not relitigate

1. **Process boundary.** `multiprocessing.get_context("spawn")` with a module-level target in the
   new `pipeline/tts/gemini_phase.py`. Only picklable data crosses: chunk strings, the
   `GeminiConfig`, the absolute deadline, the scratch dir path, the parent pid and the private
   factories. `GeminiProvider` and `GeminiTranscriber` are built **inside the child**.
   - The consumer runs as `python -m pipeline consume`. Spawn does not re-import a main module
     whose name ends in `.__main__`, so the child does not run the CLI.
2. **Deadline.** The budget is `GEMINI_BUDGET_SECONDS = 360.0`. The parent computes one absolute
   `time.monotonic()` deadline *before* `Process.start()`. On Linux, `CLOCK_MONOTONIC` is
   system-wide, so the child compares against the same number. The child respects it
   cooperatively, and the parent enforces it by `kill()`.
   - Reap margin: `REAP_TIMEOUT_SECONDS = 5.0` for `join` after `kill`.
   - Guarantee: the Gemini phase returns within budget + spawn + reap. The smoke measures spawn.
3. **Child.**
   - A `ThreadPoolExecutor(max_workers=min(4, n_chunks))` runs one chunk job per chunk. Each job
     does synth, then verify, sequentially.
   - The main thread waits for the first failure or for all successes. It **writes the terminal
     `result.json` first, then calls `os._exit(0)`**. It never waits for executor shutdown,
     because an in-flight request thread can hold the process open.
   - A daemon watchdog thread calls `os._exit(3)` if `os.getppid()` differs from the parent pid
     it was given, or if the deadline has passed.
   - Any exception not handled in the child (a bug) becomes a terminal
     `{"status": "failed", "reason": "child_error"}` with the exception text, then `os._exit(0)`.
4. **Per-chunk budget: one 3-call counter.** It covers transient retries and the one omission
   re-render, so a chunk makes at most 3 TTS calls and 2 ASR calls.
   - `content` or `infra` error: record it, then back off `2 s` before call 2 and `8 s` before
     call 3. The backoff waits on an abort `threading.Event`, so it ends early when another chunk
     has failed. If the remaining time is shorter than the backoff, the chunk fails with reason
     `deadline`.
   - `fatal` error: the chunk fails at once with reason `fatal`.
   - Counter used up: the chunk fails with reason `exhausted`.
   - After a successful synth, the chunk is verified:
     `verify_audio(pcm_to_wav(pcm), "audio/wav", chunk, transcriber=make_transcriber(min(remaining, 90)))`.
     Build a fresh transcriber per call, because its timeout is fixed when the client is built.
     - `pass`: the chunk is done.
     - `omission`: the first time, re-render with no backoff (if the counter allows). A second
       omission fails the chunk with reason `second_omission`.
     - `unavailable`: the chunk fails with reason `asr_unavailable`. ASR is not retried in T3b.
   - Before every synth, ASR call and backoff, check the deadline and the abort event.
   - Synth timeout is `min(remaining, 90.0)`.
   - One chunk's failure sets the abort event, and the main thread writes the terminal failure
     straight away.
5. **Scratch dir and files.** The parent creates it (`tempfile.mkdtemp`, under an optional
   `scratch_root`).
   - The child writes each verified chunk's PCM to `chunk-NNNN.pcm` atomically (tmp +
     `os.replace`).
   - Each chunk's attempt records go to `progress-NNNN.json` atomically, **before the next
     stage starts**, so they survive a kill.
     - An attempt record is written as started, with `null` tokens, before its request.
     - Usage of a request that was killed stays `null` (unknown), never 0.
     - Synth tokens and ASR tokens are kept separately, including those of discarded omission
       attempts.
   - `result.json` is the done marker, written atomically. It holds `status` (`ok`/`failed`),
     `reason`, `detail`, `failed_chunk` and, on `ok`, `chunks: [{index, file, bytes, sha256}]`.
   - The parent deletes the scratch dir only after the child is dead (in `finally`, after
     kill + join).
6. **Parent runner** `run_gemini_phase(chunks, leaf, *, budget_s, scratch_root=None, _factories=None, _child_bootstrap=None) -> PhaseOutcome`.
   - Poll: `proc.join(timeout=min(0.2, remaining))`, then read `result.json`.
     - A terminal result exists: done. On failure, kill any child still running before
       returning.
     - The process died with no result: reason `child_no_result`.
     - The deadline passed: kill it, reason `deadline`.
     - `Process.start()` raised: reason `spawn_failed`.
   - `finally`: `kill()` if alive, `join(REAP_TIMEOUT_SECONDS)`, `close()`, then remove scratch.
     `KeyboardInterrupt`/`SystemExit` propagate after cleanup and are never converted into a
     fallback.
   - **On `ok`, the parent validates before accepting any audio:**
     - the indices are exactly `0..n-1`;
     - every file exists;
     - byte counts match and are even and greater than 0;
     - the sha256 matches.

     Any mismatch is reason `invalid_result`, and no audio is used.
   - `PhaseOutcome(ok, reason, detail, pcm_parts | None, chunk_records, elapsed_s, spawn_s)`.
     `chunk_records` comes from the progress files, as telemetry only.
   - Fallback reasons, a closed set: `fatal`, `exhausted`, `deadline`, `asr_unavailable`,
     `second_omission`, `child_error`, `child_no_result`, `invalid_result`, `spawn_failed`.
7. **Test factories are a private runner argument, never env or config.**
   `_factories=(make_provider, make_transcriber)` are module-level callables, so they pickle by
   reference.
   - Production default is `GeminiProvider()` and `GeminiTranscriber(timeout_s=t)`, built in the
     child.
   - `_child_bootstrap` is an optional module-level callable the child runs first. Tests pass
     one that makes outbound socket connects raise, because conftest monkeypatches do not
     survive spawn.
8. **`render_episode` wiring.**
   - The early `ValueError` for a Gemini primary goes away. `gemini_phase` is imported lazily,
     only for a Gemini primary, so the OpenAI path still never imports `google.genai`.
   - The cache lookup is unchanged, and a hit returns before any phase: no alert, no child.
   - A Gemini primary is chunked with `GeminiProvider.max_chars`.
     - Phase ok: concatenate the PCM, encode, and store in the cache with
       `provider="gemini"`, `rendered=primary`, `verification="passed"`, `fallback_reason=None`.
     - Phase failed with `fallback=None`: raise `TTSRenderError(f"Gemini phase failed: {reason}")`.
       There is no alert, since the job retry path already reports.
     - Phase failed with a fallback: **discard all Gemini PCM**. Re-chunk the whole text with
       `OpenAIProvider.max_chars` and render it with the existing in-process
       `_synthesize_chunk` loop, with no verification. Store with `provider="openai"`,
       `rendered=fallback`, `verification="not_run_openai"`, `fallback_reason=reason`.
     - Both failed: raise `TTSRenderError` naming both reasons, after writing the manifest.
   - `RenderResult` gains `fallback_reason: str | None = None`. `RENDERER_VERSION` is **not**
     bumped: the OpenAI path is byte-identical and no Gemini entries exist yet.
9. **Manifest.**
   - New fields: `gemini_phase` (`None` for an OpenAI primary), holding `outcome`, `reason`,
     `detail`, `budget_s`, `elapsed_s`, `spawn_s`, per-chunk attempt records, and synth and ASR
     token totals (`null` if any was unknown).
   - Also new: `fallback_reason` and `alert_sent`.
   - `chunks` stays the list of chunks that produced the final audio. Gemini-attempt audio
     seconds are kept separate, inside `gemini_phase`.
10. **Alert.**
    - Sent by the parent only, once per fallback attempt, **after** the fallback attempt,
      including when that attempt fails. Never sent on a cache hit, never when
      `fallback=None`.
    - `render_episode(..., notify_fallback: bool = True)`. Local tools pass `False`.
    - Delivery is bounded: a daemon thread runs `send_alert`, and the parent `join`s it for at
      most `ALERT_WAIT_SECONDS = 12.0`. After that it moves on and the thread is abandoned. This
      addresses the T3a review finding that `send_alert` is per-read, not wall-clock.
    - `alert_sent` records True, False or `"timeout"`.
    - Text: `TTS fallback: <feed> <episode_id>: Gemini <model>/<voice> <reason> -> OpenAI
      <voice> <rendered|FAILED: ...>`.

## Tasks

### Task 1 — test hygiene (`9p3.12`) and child sandbox helpers

- `pipeline/conftest.py`: add an autouse fixture that redirects
  `pipeline.script_processor.SCRIPT_ARCHIVE_ROOT` to `tmp_path / "scripts"`. Remove the
  now-redundant per-file redirect in `pipeline/test_feed_voices.py`, but only where it duplicates
  this; leave explicit test-local patches that assert on the path.
- Add a cheap autouse guard that fails a test which **opens a file for writing** or **makes a
  directory** under `/persist`. Patch `pathlib.Path.mkdir`, `Path.write_text`,
  `Path.write_bytes`, `builtins.open` in write modes, and `os.makedirs`. It needs an escape
  marker `allow_persist` (unused today). Reads stay allowed (some tests may read fixtures). If
  the guard proves too invasive (breaks >3 existing tests for legitimate reasons), stop and
  report instead of weakening it silently.
- New importable, non-test module `pipeline/tts/_phase_testing.py`:
  - `deny_network()`: the child bootstrap. It patches `socket.socket.connect` and
    `socket.create_connection` to raise `OSError("network denied in test child")`, and sets
    `GEMINI_API_KEY` to a dummy value.
  - Module-level fake factories used by the real-spawn tests, parameterized by a JSON behavior
    spec read from a file path in the scratch dir or passed through `functools.partial` of
    module-level functions (pickles fine). Behaviors: `ok`, `fatal_on_chunk(i)`,
    `block_synth_forever(i)` (writes a `entered-synth-i` marker file, then sleeps 3600),
    `block_asr_forever(i)`, `omission_on_chunk(i)`.
  - It must not import pytest.
- Tests: the guard fires for a write under `/persist`; the `SCRIPT_ARCHIVE_ROOT` redirect is in
  effect.

### Task 2 — the in-process child worker (`gemini_phase.py`, part 1)

Implement the child side as plain functions that the tests call **in-process**, with injected
fakes and a fake clock or sleep where useful:

- `_render_chunk(i, chunk, leaf, deadline, scratch, provider, make_transcriber, abort, sleep)`:
  Decision 4, including persisting progress records.
- `_child_main(chunks, leaf, deadline, scratch, parent_pid, factories, bootstrap)`: the
  module-level spawn target. It runs the bootstrap, starts the watchdog, builds the provider in
  the child, runs the executor, writes the terminal result, then `os._exit`. Split the part
  before `os._exit` into `_run_child(...) -> dict` so in-process tests can call it without
  exiting.
- Atomic writes via one helper (tmp in the same dir + `os.replace`).

Tests (`pipeline/tts/test_gemini_phase.py`, in-process, fakes only):
- All chunks pass: `ok`, and the PCM files match.
- Content error then success: 2 calls, 1 backoff of 2 s. Two transient errors then success:
  backoffs of 2 s and 8 s. Three transient errors: `exhausted`.
- Fatal on the first call: reason `fatal` with no retry, and the abort event is set.
- Omission then pass: 2 TTS and 2 ASR calls, no backoff. Omission, omission:
  `second_omission`. Two transient errors, success, omission: `exhausted`, with 3 TTS calls and
  1 ASR call.
- `unavailable`: `asr_unavailable`, and ASR is not retried.
- Deadline shorter than the backoff: `deadline`, no sleep past the deadline. The synth timeout
  passed is `min(remaining, 90)`, and the transcriber is built with `min(remaining, 90)`.
- The abort event set during a backoff ends it early.
- Progress records: a record is written as started, with `null` tokens, **before** the
  provider is called (assert from inside the fake), and holds tokens after. ASR tokens are
  separate from synth tokens. A discarded omission attempt's tokens are kept.
- `_run_child` with an early fatal while a sibling chunk is blocked (the fake blocks on an
  event): the result is written without waiting for the sibling. Release the event in teardown.
- An unexpected exception in a chunk job gives `child_error` with the detail.

### Task 3 — the parent runner and real-spawn tests (`gemini_phase.py`, part 2)

- `run_gemini_phase` per Decisions 2, 5 and 6, plus `PhaseOutcome` and result validation
  (`_validate_result`).
- In-process tests for `_validate_result`: a missing index, an extra index, a missing file, a
  byte mismatch, odd bytes, zero bytes, a sha mismatch, and an unparseable or partial
  `result.json` (all give `invalid_result`, or `child_no_result` when the file is absent and the
  child is dead).
- Real-spawn tests (`pipeline/tts/test_gemini_phase_spawn.py`, marked `slow` only if the repo
  already has such a marker; otherwise plain). Each uses `_child_bootstrap=deny_network` and
  `_phase_testing` factories with a small budget (2-4 s):
  - **Mandatory:** synth blocked forever. The runner returns `deadline` within
    budget + reap + 2 s slack, and the child is dead (`not proc.is_alive()`, and the pid is gone
    via `os.kill(pid, 0)` raising `ProcessLookupError`).
  - **Mandatory:** ASR blocked forever. Same checks.
  - **Mandatory:** early fatal on chunk 0 while chunk 1 is blocked. It returns `fatal` well
    before the budget ends (under half), and the child is dead.
  - The child exits with no result (a factory calling `os._exit(1)` at once):
    `child_no_result`.
  - Happy path: 3 chunks come back `ok`, with the PCM in order and equal to the fakes' bytes.
  - **Mandatory, parent death:** a helper process starts the runner and is then SIGKILLed. The
    grandchild's watchdog exits within about 1 s, checked by the pid the helper wrote to a
    file.
  - The scratch dir is removed after each run.
  - `KeyboardInterrupt` in the parent (simulate by patching the poll to raise) kills the child
    and propagates.
- Keep the whole spawn suite under ~30 s wall.

### Task 4 — wire into `render_episode`, cache, manifest, alert

Per Decisions 8-10. The tests go in `pipeline/tts/test_render.py`, or in a new
`test_render_gemini.py` if cleaner. Monkeypatch `pipeline.tts.gemini_phase.run_gemini_phase` to
return canned `PhaseOutcome`s, so there is no spawn.
- Gemini ok: the result is gemini/passed and the cache entry is `verification="passed"`. A
  second call hits the cache with no phase call.
- Gemini failed with a fallback: the OpenAI fake renders the **whole** text, re-chunked at 4096.
  No Gemini PCM is in the output (assert on the encoded input bytes, using distinct fake PCM
  byte patterns). The cache entry is openai/`not_run_openai`/`fallback_reason`, the result has
  `fallback_reason`, and exactly one alert is sent.
- A replay of that cached fallback: `rendered` is the fallback, and no alert or phase call
  happens.
- Gemini failed with `fallback=None`: `TTSRenderError`, no alert, a failed manifest.
- Both failed: `TTSRenderError` naming both reasons, one alert (FAILED text), a failed
  manifest.
- `notify_fallback=False`: no alert.
- An alert that hangs: `render_episode` returns within `ALERT_WAIT_SECONDS` (patch it to 0.2)
  and `alert_sent == "timeout"`.
- The manifest's `gemini_phase` fields, and `chunks` reflecting the final audio only.
- **No OpenAI ASR:** the OpenAI path and the fallback path never import or call the
  transcriber. Assert `pipeline.tts.asr` is not imported by an OpenAI-primary render in a fresh
  subprocess (`python -c`), which pins the import-cost guarantee.
- Every existing OpenAI render test passes unchanged.
- Remove the "not wired yet" test and the `ValueError`. Replace them with the tests above.

### Task 5 — docs

- Design doc "Amendments": add the alert bound (daemon thread plus a bounded join), the closed
  fallback-reason set, the parent-validates-result rule, and `RENDERER_VERSION` staying at 2.
- `pipeline/AGENTS.md` "TTS Renderer": remove the "refuses Gemini" line. Describe the Gemini
  phase, the fallback, the manifest `gemini_phase` fields, the alert text, and how to read a
  fallback. State that no feed is Gemini yet.
- Root `AGENTS.md` Core Paths: mention `gemini_phase.py`.

### Task 6 — real smoke (controller, not implementer)

Standalone guarded harness `/persist/my-podcasts/tts-eval/t3/smoke_t3b.py`. Artifacts go under
`/persist/my-podcasts/tts-eval/t3/smoke-t3b/`. Budget a few dollars.
1. A real Gemini Flash-Lite/Kore render of `in-rundown.txt` with `fallback=None`,
   `cache_dir=None`, a local `manifest_dir` and `notify_fallback=False`. Record wall time,
   `spawn_s`, chunk verdicts, tokens and the resulting mp3 duration.
2. A forced fallback: an invalid Gemini voice with an OpenAI fallback, on a short text. The
   expected reason is `fatal`, the output is OpenAI, and the alert is suppressed.
3. An offline hard deadline: the real spawn with a blocking fake and a 5 s budget. Record the
   wall time.
