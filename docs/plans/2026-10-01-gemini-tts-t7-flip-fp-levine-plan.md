# T7: flip FP Digest and Levine to Gemini, plus currency normalization (9p3.8, 9p3.15)

Owner decisions (2026-10-01): go straight to Gemini everywhere they chose, without the
5-episode wait after T6. Flash-Lite on every feed; voices fp-digest **Alnilam**, levine
**Enceladus** (picked from Google's previews, auditioned on our text below). Style unchanged
(`calm, measured news anchor`). The currency fix (9p3.15) lands in the same PR because the
design required it before Levine flips. The Rundown (T6, Kore) deployed 2026-10-01 20:17 EDT.

## Changes

1. **Verifier v4 (9p3.15).** `normalize.py`: a dollar amount normalizes to its bare number
   (`$15.51` -> `fifteen point five one`, `$1.8 billion` -> `one point eight billion`) and
   `normalize_tokens` drops `dollar`/`dollars` on both sides, after rewriting a spelled
   "<number> dollars and <1-99> cents" to "<number> point d d". Gemini ASR writes `$15.51`,
   `15.51` or `15.51 dollars` for the same audio, and our writers spell prices out ("one
   hundred seven dollars and thirty-five cents"); under v3 a price without the `$` was a 1-4
   token deficit and a 4-price list reached `net_missing` 8 (a false omission). Under v4 every
   one of those forms normalizes identically. Accepted cost: a dropped "dollars" is never
   counted as missing speech.
   `VERIFIER_VERSION` 3 -> 4 (so `VERIFIER_POLICY` and every Gemini cache key change: the
   first render of each script after deploy is cold; nothing else).
2. **Flip.** `FEED_VOICES`: `fp-digest` -> Gemini Alnilam, fallback `onyx`; `levine` ->
   Gemini Enceladus, fallback `ash`. A `_gemini(voice, fallback_voice)` helper and
   `GEMINI_TTS_MODEL`/`GEMINI_STYLE` constants replace the T6 literal. Golden tests
   re-literal each entry.

## Evidence

- **T5 replay under v4.** T5 labels are token indices, and v4 tokenizes 16 of 48 base
  chunks differently (Rundown scripts spell out "dollars"; Levine uses `$`). A remapped copy
  of the root (`/persist/my-podcasts/tts-eval/t5-v4/root`, built by `remap.py` there:
  difflib alignment of each chunk's v3 and v4 token streams; audio, ASR and whisper runs
  symlinked unchanged) gives, at the production point (M=6, floor 0.95): **dev 74/74,
  hold-out 78/78 caught and localized, 0/46 false alarms in each, identical to v3.**
  Per-run margins (`margins.py`): every base still passes; worst `max_net_missing` 3 and
  min recall 0.973 under both. The Levine `$` chunk the bead cites improved (recall 0.973 ->
  0.996, max net 2 -> 1); Rundown 09-29 c3 improved slightly; one FP 09-29 run moved 0.988 ->
  0.987. (A first cut of v4 without the spelled-cents rewrite dropped FP 09-29 to 0.979,
  because its script spells "seven dollars and thirty-five cents" where the ASR wrote
  `$107.35`; the rewrite is what fixed it.) Caveat: no 8-24-token T5 cut removed a dollar
  amount, so this shows no regression rather than measuring skipped prices near the threshold.
  `/persist/my-podcasts/tts-eval/t5` itself still holds v3 labels (see its `NOTES-v4.md`); run
  v4 `tts-calibrate report` against `t5-v4/root`.
- **Audition** (T4 scripts, Flash-Lite, production verification, `/persist/my-podcasts/tts-eval/t7aud/`):
  - FP Alnilam: ok, 5:46, both chunks verified first call (recall 0.991, 0.994).
  - Levine Enceladus: first run **failed** (chunk 1: 3x `finishReason OTHER`, no audio);
    second run ok, 6:10, chunk 1 on its second call. The same chunk took 3 calls for Kore and
    Charon and exhausted Puck in T4. It is content-triggered, not voice-specific. The T5
    full-length deadline run (Levine 2026-09-24, 22k chars, Flash-Lite Kore) passed in 175 s,
    but its chunk 1 also used all 3 calls (ReadTimeout, `OTHER`, ok). Real Levine episodes are
    up to ~29k chars (about 10 chunks), so **expect a material Levine fallback rate**: each
    fallback costs up to 6 minutes of Gemini, the OpenAI render (2-4 min), and one alert.
- **Fixture.** `fixtures/t5/cut-multi.json` (a Rundown chunk with "dollars") intervals moved
  down by one to v4 coordinates, from the remapped root.

## Deploy

The consumer runs from the main checkout. After merge: `git pull` + restart.

**Rollback per feed** (the likely case is Levine alone): swap its `_gemini(...)` entry for
`_openai("<fallback voice>")`, update its goldens, commit, pull, restart (see
`pipeline/AGENTS.md`). Reverting this whole commit also undoes verifier v4 and FP.

**Proposed revert criterion (owner's call):** if 2 of the first 5 Levine episodes fall back,
move Levine back to OpenAI `ash` and open a bead for Levine-specific handling (e.g. more calls
per chunk for `OTHER`).

## Watch (with T6's 5-episode review, now across three feeds)

Fallback rate per feed (Levine especially: `finishReason OTHER` exhaustion), retries, omission
flags, cost, and the v3 short-chunk item (unmatched tokens on chunks under 300 tokens;
revisit if a faithful chunk exceeds 12).
