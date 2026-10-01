# T6 prerequisites: manual-publish voice default + diagnosable omission verdicts

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Two small changes that must land before The Rundown is flipped to Gemini (T6, `my-podcasts-9p3.7`):
(1) manual publishing renders the feed's configured voice instead of always OpenAI `nova` (`my-podcasts-9p3.16`);
(2) a production omission verdict can be diagnosed afterwards: what the script said, what the ASR heard, and the actual
audio the verifier rejected (`my-podcasts-9p3.13`).

**Architecture:** (1) is a default change from `"nova"` to `None` in `publish_script` and the two CLI `--voice`
options; `None` already means "use `FEED_VOICES`" in `tts.resolve_render_config`. (2) adds diagnostic fields to the
Gemini phase's per-attempt ASR record (spans with what was heard, token counts, the transcript on omission), puts a
one-line omission summary into the `second_omission` failure detail and the fallback alert, and keeps the rejected
audio of every omission attempt as an mp3 next to the manifests (60-day retention, like manifests). **None of this
changes what is flagged** -- alignment, thresholds and normalization are untouched, so `VERIFIER_VERSION` stays `"2"`
and the render cache key is unchanged.

**Tech stack:** Python 3.12, click, pytest. Worktree `.worktrees/tts-t6-prereqs`, branch `tts-t6-prereqs`.

**Rules for every task:**
- TDD: failing test first, watch it fail, then the code.
- Tests are hermetic: no network, no API keys, no `/persist` (autouse guards in `pipeline/conftest.py` enforce this; use `tmp_path`).
- Run `uv run pytest pipeline/tts -q` (and the specific `pipeline/test_*.py` you touched) before each commit;
  `uv run ruff check . && uv run ruff format --check .` must be clean. The full suite (`uv run pytest -q`, ~2.5 min) before the last commit.
- **Telemetry never costs a render** (existing rule, `pipeline/AGENTS.md` "Gemini phase"): every new diagnostic
  path is best-effort, wrapped so that any exception degrades the record and never fails or delays the episode audio.
- Commit after each task with a plain-English message. Run `git commit` bare (no identity flags).

---

## Task 1: Manual publish follows `FEED_VOICES` (`my-podcasts-9p3.16`)

**Why:** the documented consumer-down recovery is `the-rundown --dry-run`, then `publish-script`, then
`jobs complete`. Today `publish-script` defaults `--voice nova`, and any voice override forces OpenAI. After T6 flips
The Rundown to Gemini, the recovery path would silently publish a different voice and provider than the consumer.

**Files:**
- Modify: `pipeline/script_processor.py` (`DEFAULT_VOICE = "nova"` at ~line 88, `publish_script(voice=...)` ~186)
- Modify: `pipeline/__main__.py` (`--voice` options at ~814 `publish-script` and ~934 `episode`; the dry-run echo `voice={voice}` ~864)
- Tests: `pipeline/test_feed_voices.py` (two tests pin `nova` at ~143 and ~170), `pipeline/test_script_processor.py`, `pipeline/test_publish_script_cli.py`
- Docs: `pipeline/AGENTS.md` ("TTS Renderer" bullets "Overrides force OpenAI" and "`publish_script` and the CLI still default to `nova`"), `AGENTS.md` (`episode` CLI synopsis `[--voice nova]`)

**Behavior:**
- `publish_script(..., voice: str | None = None, ...)`. `None` -> `tts.resolve_render_config(feed_slug)` (FEED_VOICES).
  Remove `DEFAULT_VOICE` if nothing else uses it (grep first).
- Both CLI options: `@click.option("--voice", default=None, type=str, help="TTS voice. Default: the feed's configured voice (FEED_VOICES). Any value forces OpenAI with that voice.")`.
- An explicit `--voice nova` (or any value) still forces OpenAI with that voice -- unchanged `resolve_render_config` behavior.
- The `publish-script --dry-run` echo must say what will actually render, e.g.
  `Running TTS (dry run, openai tts-1-hd/onyx)...`: describe `config.primary` (provider, model, voice) of the resolved config.
  Resolve the config once and pass the same object to `render_episode`.
- This is an **intentional manual-default change**: manual publishes on `fp-digest` become `onyx`, `levine` `ash`, etc.
  (whatever `FEED_VOICES` says). Unknown slugs still get `nova` via `DEFAULT_RENDER_CONFIG`.

**Tests (replace the two `nova` pins, add):**
1. `publish_script` with no `voice` on `fp-digest` renders exactly `tts.resolve_render_config("fp-digest")` (import the expected value from config; do not re-literal the voice).
2. Same for `publish-script --dry-run` via click's `CliRunner` (existing test at ~170 shows the harness).
3. `publish_script(voice="nova")` on `fp-digest` renders `openai_config(model="tts-1-hd", voice="nova")`.
4. `episode` CLI without `--voice` passes `voice=None` to `publish_script` (or renders FEED_VOICES -- whichever the existing `test_publish_script_cli.py` harness supports).
5. **Gemini fall-through:** monkeypatch `FEED_VOICES` (or `resolve_render_config`) so a test slug maps to a Gemini-primary `RenderConfig`; `publish_script` with no voice passes that Gemini config to `render_episode` (fake renderer; nothing spawns). This is the T6 case the change exists for.

**Docs:** rewrite the two `pipeline/AGENTS.md` bullets: overrides still force OpenAI; `publish_script` and the CLI now
default to the feed's `FEED_VOICES` config, so a manual publish matches what the consumer would render (including a
Gemini primary with its OpenAI fallback, once a feed has one); note the manual-default change (fp-digest now onyx etc.).
In `AGENTS.md` change `[--voice nova]` to `[--voice VOICE]`.

Commit: `Manual publish uses the feed's configured voice by default`.

---

## Task 2: Spans record what was heard; verdicts carry the transcript

**Files:** Modify `pipeline/tts/verify.py`; Test `pipeline/tts/test_verify.py`.

**Behavior:**
- `Span` gains a last field `heard: str = ""`: the normalized transcript tokens of the span's gap,
  `" ".join(transcript[transcript_start : min(transcript_end, transcript_start + _EXCERPT_TOKENS)])`. Set in `_align`.
  (Defaulted so existing `Span(...)` constructions elsewhere keep working; grep `Span(` -- `calibrate.py` has its own `SuspectSpan`, unaffected.)
- `Verdict` gains a last field `transcript: str | None = None`, set by `verify_audio` to the raw ASR text (`tr.text`)
  whenever a transcription came back (including the `asr_empty` unavailable case); `None` when the transcriber raised.
- Do NOT change flagging, recall, reasons, or `VERIFIER_VERSION`. Module docstring: one sentence that `heard` and
  `transcript` are diagnostics only.

**Tests:**
1. A dropped span: script `"a b c d e f g h i j k l m n o p q r s t"` style fixture where the transcript replaces a
   middle run with two different words; the flagged span's `heard` equals those two normalized words.
2. `heard` is capped at 30 tokens.
3. `verify_audio` with a fake transcriber returns `verdict.transcript == <fake text>`; with a raising
   `TranscriptionUnavailable` transcriber, `transcript is None`.
4. Existing tests unchanged and passing; `pipeline/tts/test_t5_fixtures.py` still passes (it pins detection on real transcripts).

Commit: `Record what the ASR heard in each verifier span`.

---

## Task 3: The Gemini phase's ASR record carries the diagnosis

**Files:** Modify `pipeline/tts/gemini_phase.py` (`_asr_record` ~239, the `"started"` placeholder ~379, the module
docstring's progress schema ~22-40, the omission branch ~421-427); Test `pipeline/tts/test_gemini_phase.py`.

**Behavior -- `_asr_record(verdict)` adds:**
- `script_tokens`, `transcript_tokens`, `matched_tokens`: from `verdict.analysis` (None when there is no analysis).
- `max_net_missing`: max `net_missing` over all spans (0 when there are no spans; None without analysis). Recorded on
  pass too: this is the margin to trend in T6 (T5 clean chunks never exceeded 3; the flag is at 6).
- `spans`: a list of span dicts `{script_start, script_end, script_words, transcript_words, net_missing, flagged, excerpt, heard}`:
  - status `omission`: every flagged span, then the largest-`net_missing` unflagged spans until there are at least 3
    (a recall-floor failure has no flagged span, so these are its best clues); cap at `MAX_RECORDED_SPANS = 8`, flagged first.
  - status `pass`: only the single span with the largest `net_missing` (if any spans exist).
  - otherwise (unavailable): `[]`.
- `transcript`: on `omission` only, `verdict.transcript` capped at `MAX_RECORDED_TRANSCRIPT_CHARS = 6000` (cut at the
  end, with `"...[truncated]"` appended when cut); otherwise None. A 3000-char chunk's transcript is ~3000 chars, so the cap rarely bites.
- The `"started"` placeholder dict gains the same keys with `None` / `[]` so a killed request leaves a same-shaped record.

**Behavior -- omission summary:** add `_omission_summary(verdict) -> str`, e.g.
`omission (long_unmatched_span) recall 0.912, max net 24: script "the first twelve tokens of the largest flagged span ..." heard "..."`
(largest-`net_missing` span; excerpt and heard trimmed to 12 tokens each; reasons joined by `,`; recall to 3 decimals or `n/a`).
Use it for `last_problem` in the omission branch, so the `second_omission` `_ChunkFailure` detail -- which already
flows into `result.json`, `PhaseOutcome.detail` and the manifest's `gemini_phase.detail` -- names what was dropped.
It must never raise (wrap; fall back to the old `omission (<reasons>)` text).

**Tests:** extend the in-process chunk tests in `test_gemini_phase.py` (they use fake providers/transcribers; find the
existing omission test and follow its pattern):
1. A chunk whose fake transcriber drops a run of words: the progress file's omission attempt has `spans` with a
   flagged span whose `excerpt`/`heard` are right, `max_net_missing` >= 6, and `transcript` equal to the fake text.
2. A passing attempt records exactly one span (the largest net) and `transcript is None`.
3. A recall-floor-only omission (scattered substitutions, no flagged span) still records 3 spans.
4. Two omissions -> `_ChunkFailure(second_omission)` whose `detail` contains the dropped excerpt.
5. `_asr_record` on an `unavailable` verdict: `spans == []`, `transcript is None`, counts None.
6. Transcript capping.

Commit: `Record flagged spans and the transcript in Gemini-phase omission records`.

---

## Task 4: The fallback alert says what was skipped

**Files:** Modify `pipeline/tts/render.py` (`_deliver_alert` ~496, `_fallback_alert_text` ~534, its call in
`_render_gemini_primary` ~636); Test `pipeline/tts/test_render_gemini.py`.

**Behavior:** `_fallback_alert_text` gains `detail: str = ""` and `failed_chunk: int | None = None`. For reason
`second_omission` only, append after the reason ` (chunk <n>: <detail>)` where detail is whitespace-collapsed and cut to
240 chars. Other reasons keep today's text exactly (their details are tracebacks or SDK errors; the manifest has them).
Pass `outcome.detail` / `outcome.failed_chunk` from `_render_gemini_primary`. The existing "nothing about the alert can
raise into the render" property must hold (a broken detail degrades to today's text).

**Tests:** exact alert text for a `second_omission` outcome with a detail; unchanged text for `deadline`/`fatal`;
a long detail is cut to 240 chars; a detail object whose `__str__` raises does not break the alert.

Commit: `Name the dropped passage in the TTS fallback alert`.

---

## Task 5: Keep the rejected audio of every omission attempt

**Why:** the ASR that flagged an omission cannot settle whether the omission is real (in T5 whisper dropped words in
9 of 10 clips it flagged). Only listening can. Omission attempts are rare, so keeping their audio is cheap.

**Files:** Modify `pipeline/tts/gemini_phase.py`, `pipeline/tts/render.py`, `pipeline/tts/manifest.py`;
Tests `pipeline/tts/test_gemini_phase.py`, `test_render_gemini.py`, `test_manifest.py`.

**Behavior:**
- **Child** (`_render_chunk_inner`, omission branch): best-effort `_atomic_write(scratch / omission_name(i, n), pcm)`
  where `omission_name(i, n) = f"omission-{i:04d}-{n}.pcm"` (`n` = the attempt's `n`); on success set
  `attempt["omission_audio"] = <name>`; on failure log to stderr and continue (never changes the chunk's outcome).
  Write it before the `save()` that records the outcome, so a recorded name always has a file (atomic write -> whole or absent).
- **Runner** (`run_gemini_phase`'s `outcome()` builder): `PhaseOutcome` gains
  `omission_audio: tuple[tuple[int, int, bytes], ...] = ()` -- `(chunk_index, attempt_n, pcm)` for each attempt in the
  progress records whose `omission_audio` is exactly `omission_name(index, n)`, the file exists, is non-empty, even-length
  and at most `MAX_OMISSION_CLIP_BYTES = 24_000_000`; at most `MAX_OMISSION_CLIPS = 4`, in (chunk, n) order. Read before
  the scratch dir is removed (the `finally` already runs after `outcome()` returns). Never raises: any problem yields
  fewer clips. Confirm `_validate_result` ignores unlisted files (it should; it checks only listed chunk files) and add a
  test that an `omission-*.pcm` in scratch does not make a good result `invalid_result`.
- **Parent** (`render.py`): `render_episode` derives `omission_dir = manifest_dir / _safe_component(feed_slug) / "omission-audio"`
  when `manifest_dir` is not None, else None, and passes it to `_render_gemini_primary`. There, **after** the episode
  audio is produced or has failed (use `try/finally` around the ok/fallback tail so clips are saved in both cases, and
  never before the episode audio), `_save_omission_audio(outcome, omission_dir, episode_id, record)`:
  for each clip, `encode_mp3(pcm, omission_dir / f"{safe_id}-{utc stamp with microseconds}-c{i:04d}-a{n}.mp3")`
  (reuse `manifest._safe_component` and the manifest's stamp format; cap the id like `write_manifest` does), and record
  the absolute path as `"omission_audio_file"` on the matching attempt in `record["gemini_phase"]["chunks"]`.
  Any exception -> `record["gemini_phase"]["omission_audio_error"] = "<Type>: <msg>"[:300]` and carry on. It must not
  raise, and must not run when `omission_dir is None` (dry runs keep nothing).
- **Retention:** `prune_manifests` also removes `*/omission-audio/*.mp3` older than `max_age_days` (same mtime rule).

**Tests:**
1. Child: an omission attempt writes `omission-0000-1.pcm` with the rejected PCM and records its name; a failing write
   (monkeypatch `_atomic_write` for that name only) leaves the chunk's outcome unchanged and no name recorded.
2. Runner: clips are collected (index, n, bytes), capped at 4, malformed/odd-length/oversized/mismatched-name files skipped.
3. Render: a canned `PhaseOutcome` with one clip and `manifest_dir=tmp_path` -> an mp3 exists under
   `tmp_path/<feed>/omission-audio/` and the attempt carries `omission_audio_file`; with `manifest_dir=None` nothing is
   written; an `encode_mp3` that raises sets `omission_audio_error` and the episode still renders (both the ok path and the fallback path).
   (Encoding in tests: use the existing fake/real `encode_mp3` pattern from `test_render_gemini.py`; ffmpeg is available in CI if the existing tests already encode.)
4. `prune_manifests` removes an old clip and keeps a new one.

Commit: `Keep the audio of Gemini attempts the verifier rejected`.

---

## Task 6: Docs

**Files:** `pipeline/AGENTS.md` ("Gemini phase and OpenAI fallback (T3b)": the Manifest, alert and "Reading a fallback"
bullets; a new bullet "Diagnosing an omission"), `pipeline/tts/gemini_phase.py` module docstring (progress schema),
design doc `docs/plans/2026-09-30-gemini-tts-design.md` Amendments (one bullet: T6 prerequisites landed, what is recorded).

"Diagnosing an omission" must say: where the spans/transcript live in the manifest
(`gemini_phase.chunks[i].attempts[k].asr`), that `heard` is the ASR's normalized words for the gap, that
`max_net_missing` on passing attempts is the margin to trend (T5: clean <= 3, flag at 6), and that the rejected audio
is at `.../tts-renders/<feed>/omission-audio/*.mp3` (path also in `omission_audio_file`) for 60 days -- **listen to it
before deciding a flagged omission was a false alarm**.

Commit: `Document how to diagnose a Gemini omission`.

---

## Real smoke (after SDD, before the PR; operator step, not a subagent)

One cheap paid run proves the wiring on a real Gemini render: `tts-audition` on a short Rundown script with
`--max-chars 1500`, one Flash-Lite voice, `--out-dir /persist/my-podcasts/tts-eval/t6pre/smoke1` (manifests land in
its `manifests/`). Check the attempt record has `script_tokens`, `max_net_missing`, one span, `transcript: null`.
Forcing a real omission is not needed: Tasks 3-5 are covered by fakes, and a real omission would need a paid cut.
Optional if cheap: re-verify an existing T5 cut chunk by calling `verify_audio` directly to see real `heard` output.
