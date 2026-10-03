# Gemini TTS: keep token usage for ASR calls that answered but were unusable (`my-podcasts-9p3.18`)

## Why

Since #34 (`9p3.17`) a blocked ASR check is re-asked on the same audio. A blocked
call raises `TranscriptionUnavailable` before its usage is read, so its attempt
record has `null` tokens and `elapsed_s`, and `render._phase_totals` reports every
`gemini_phase.tokens.asr_*` total as `null` for any phase that saw a block, even when
the re-check passed. Levine is where blocks happen, so Levine cost tracking goes dark.

## Evidence: what a blocked response carries

Probe 2026-10-03 (`/persist/my-podcasts/tts-eval/asr-block/usage-probe/`, 8 calls of
the production ASR request on the 10-02 Levine audio, under $0.01): call 8 was
blocked (`candidates` empty, `block_reason=OTHER`) and its `usage_metadata` was
`prompt_token_count 1777` (audio 1740 + text 37), `total_token_count 1777`, **no
`candidates_token_count`** and no `thoughts_token_count`. The 7 passing calls had
the same 1777 prompt tokens plus ~205 candidates tokens. So a block reports its input
in full, and its output is genuinely 0 (`total == prompt`), not unknown.
(Whether Google bills a blocked prompt is still unconfirmed; the record is usage, not
an invoice.)

## Design

1. `asr.py`: `TranscriptionUnavailable` gains an optional `usage` (a small frozen
   dataclass: `elapsed_s`, `input_tokens`, `output_tokens`, `thinking_tokens`),
   default `None`. Every raise **after a response came back** attaches it:
   no candidates (`asr_blocked` and `asr_empty`), `asr_incomplete`, and
   `transcript has no word tokens`. Raises with no response (`asr_error`,
   `asr_timeout`, client construction, no budget) keep `usage=None`: truly unknown.
2. Usage values: `input_tokens = prompt_token_count`;
   `output_tokens = candidates_token_count`, or, when that is absent, derived as
   `total - prompt - (thoughts or 0)` only when `total` and `prompt` are both present
   (0 for the probed block); otherwise `None`. `thinking_tokens = thoughts_token_count`
   as reported (`None` stays `None`; `_phase_totals` already counts a completed
   call's missing thinking as 0). One helper reads usage for both the success path
   and the raises, so they cannot drift.
3. `verify.py` (`verify_audio`): when the exception carries usage, the unavailable
   verdict gets an `AsrInfo` (model, prompt version and policy from the transcriber
   when available, else the defaults; `finish_reason` the response's, or `"NONE"`
   for no candidates; `transcript_chars` 0) instead of `None`. Status, reasons and
   detail are unchanged.
4. Nothing else should need to change: `gemini_phase._asr_record` already copies
   tokens/elapsed from `verdict.asr`, and `render._stage_completed` already treats an
   `unavailable` record with `elapsed_s` as a completed call. Tests pin the chain:
   blocked -> re-check pass gives known `asr_input`/`asr_output` totals; an
   `asr_timeout`/`asr_error` attempt still makes totals `null`.
5. No version bumps: verdict semantics, cache key and retry policy are unchanged; only
   telemetry on unavailable records gains values. `tts-verify` reports gain the same
   usage on an unavailable verdict (harmless; exit codes unchanged).
6. Docs: `pipeline/AGENTS.md` (replace the "known gap" sentence; the `tokens` rule),
   design-doc amendment line, the asr.py docstring.
