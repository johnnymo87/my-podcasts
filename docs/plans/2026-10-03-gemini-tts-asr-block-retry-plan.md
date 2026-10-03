# Gemini TTS: retry a blocked ASR check on the same audio (`my-podcasts-9p3.17`)

## Why

The first production Gemini Levine (2026-10-02, "Money Stuff: The Podcast:
Reintegration With the Default World", one chunk) fell back to OpenAI `ash` with
`asr_unavailable`. Synthesis was fine; the **verification** request came back with
no candidates and `prompt_feedback.block_reason=OTHER`. Today any `unavailable`
verdict fails the Gemini phase at once.

Re-test (2026-10-03, about $0.10, artifacts `/persist/my-podcasts/tts-eval/asr-block/`):
the block is **transient and content-sensitive**. On that text 4 of 13 ASR calls were
blocked; the *same audio bytes* were blocked twice and then passed twice. Zero blocks
in all of T5 and on the 2026-10-02 Rundown/FP chunks.

## Design (owner-approved 2026-10-03; oracle-astra consulted)

1. **Structural classification (`pipeline/tts/asr.py`).** No candidates **and** a real
   `prompt_feedback.block_reason` (anything but `None` / `BLOCKED_REASON_UNSPECIFIED`)
   raises `TranscriptionUnavailable("asr_blocked", "no candidates (block_reason=X)")`.
   No candidates without a block reason stays `asr_empty`. Every other reason
   (`asr_error`, `asr_timeout`, `asr_incomplete`, `asr_empty`) is unchanged.
   `verify_audio` passes the reason through as the verdict's `reasons[0]`; the
   phase matches `verdict.reasons == ("asr_blocked",)` exactly, never text.
   All block reasons are retried, not only `OTHER`: a retry is one cheap ASR call on
   audio that already exists, and a stubborn block still ends in the same fallback.
2. **Retry in the phase only (`pipeline/tts/gemini_phase.py`).** The per-chunk
   counter (`MAX_TTS_CALLS = 3`) now counts **tries**: a try is a TTS call (with its
   first ASR) **or an ASR-only re-check of the retained PCM**. A blocked verdict keeps
   the PCM and schedules a re-check, with the existing backoff indexed by tries used
   (2 s, then 8 s), the deadline (`remaining() < delay` -> `deadline`, no sleep) and
   sibling-abort precedence exactly as for transient synth errors. A fresh
   transcriber is built (and closed) per verification with the timeout recomputed.
   The retained PCM is set only by a block and consumed by the very next try (always
   a re-check), so a new synth never sees it and stale audio can never be re-checked.
3. **Terminal reasons.** Counter used up and the last try was a block ->
   `asr_unavailable` (not `exhausted`), tracked by an explicit variable, not by
   parsing `last_problem`. Any other unavailable reason -> `asr_unavailable` at once,
   as today. The omission counter stays independent and cumulative (second omission
   -> `second_omission`), and the omission re-render keeps its no-backoff behavior.
4. **Records.** A re-check is its own attempt record:
   `{"n": k, "synth": None, "asr": {...}, "outcome": ..., "recheck_of": <n of the
   synth attempt that produced the audio>}`. A blocked attempt (synth or re-check)
   gets outcome `"asr_blocked"`. Existing readers keep working unchanged:
   `render._phase_totals` skips a `None` stage, `audition._verify_summary` reads the
   last attempt's ASR. Omission clips are named by the verdict attempt's own `n`.
   "Attempt" in the docs now means a budgeted try, not a TTS call.
5. **Bound.** Per chunk: synth calls + ASR-only re-checks <= 3; at most 3 TTS calls;
   at most 3 ASR calls; at most 5 external requests (e.g. synth/omission ->
   synth/block -> re-check/pass). Phase budget (360 s) and the parent's kill are
   unchanged.
6. **No version bumps.** `VERIFIER_VERSION`, `ASR_POLICY`/`VERIFIER_POLICY` and
   `RENDERER_VERSION` stay. What passes and what is an omission is unchanged; only
   how often an unavailable verdict is re-asked changes. Previously cached fallback
   entries replay as designed (fallbacks are cached on purpose). `tts-verify` and
   calibration do **not** retry (they are measurement tools); they already pass any
   reason through (exit 3 = unavailable).
7. **Token totals: deferred.** A blocked call raises before any usage is read, so its
   record has unknown tokens and a phase that saw one reports `tokens` as `null`
   (honest unknown, the existing rule). Billing of a blocked prompt is unconfirmed.
   Plumbing usage through `TranscriptionUnavailable` is a follow-up if the totals
   matter.

## Tasks (one implementer, TDD, hermetic fakes)

1. `asr.py`: `asr_blocked` classification + docstring; tests in `test_asr.py` (block
   OTHER/SAFETY -> `asr_blocked`; `UNSPECIFIED`/no feedback -> `asr_empty`; detail
   names the reason). `test_verify.py`: the reason passes through.
2. `gemini_phase.py`: re-check loop, records, terminal reasons, module/function
   docstrings. Tests in `test_gemini_phase.py`: block -> re-check pass (one synth
   call, byte-identical PCM re-sent, 2 s backoff, `recheck_of`); block x3 ->
   `asr_unavailable` with 1 synth + 3 ASR; block -> block -> pass (2 s then 8 s);
   every other unavailable reason still immediate (existing test kept); omission ->
   synth -> block -> re-check pass; omission -> synth -> block -> re-check block ->
   `asr_unavailable`; transient synth error -> synth -> block -> re-check -> pass
   within 3 tries / block -> `asr_unavailable`; block -> re-check omission -> fresh
   synth pass (counter permitting); deadline shorter than the re-check backoff ->
   `deadline` without sleeping; abort during re-check backoff -> aborted; re-check
   recorded `started` before the request; fresh transcriber per re-check. A
   `_phase_totals` regression on a record set with a `synth: None` attempt.
3. Docs: `pipeline/AGENTS.md` (T3b Flow/The bound/Fallback reasons/Reading a
   fallback, the Levine paragraph), design doc Amendment in
   `docs/plans/2026-09-30-gemini-tts-design.md`, `AGENTS.md` only if it states the
   bound.

Full suite + `ruff check` + `ruff format --check` before the PR. No paid smoke is
required (the evidence is the re-test above); optional later: render
`/persist/my-podcasts/tts-eval/asr-block/script.txt` a few times through
`tts-audition`.
