# T7: flip FP Digest and Levine to Gemini, plus currency normalization (9p3.8, 9p3.15)

Owner decisions (2026-10-01): go straight to Gemini everywhere they chose, without the
5-episode wait after T6. Flash-Lite on every feed; voices fp-digest **Alnilam**, levine
**Enceladus** (picked from Google's previews, auditioned on our text below). Style unchanged
(`calm, measured news anchor`). The currency fix (9p3.15) lands in the same PR because the
design required it before Levine flips. The Rundown (T6, Kore) deployed 2026-10-01 20:17 EDT.

## Changes

1. **Verifier v4 (9p3.15).** `normalize.py`: a dollar amount normalizes to its bare number
   (`$15.51` -> `fifteen point five one`, `$1.8 billion` -> `one point eight billion`) and
   `normalize_tokens` drops `dollar`/`dollars` on both sides. Gemini ASR writes `$15.51`,
   `15.51` or `15.51 dollars` for the same audio; under v3 each price without the `$` was a
   1-4 token deficit and a 4-price list reached `net_missing` 8 (a false omission).
   Accepted cost: a transcript spelling out "fifteen dollars and fifty one cents" no longer
   matches `$15.51` (ASR writes digits), and a dropped "dollars" is never counted missing.
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
  0.996, max net 2 -> 1); FP 09-29 dipped 0.985 -> 0.979 (two matched "dollars" gone).
- **Audition** (T4 scripts, Flash-Lite, production verification, `/persist/my-podcasts/tts-eval/t7aud/`):
  - FP Alnilam: ok, 5:46, both chunks verified first call (recall 0.991, 0.994).
  - Levine Enceladus: first run **failed** (chunk 1: 3x `finishReason OTHER`, no audio);
    second run ok, 6:10, chunk 1 on its second call. The same chunk took 3 calls for Kore and
    Charon and exhausted Puck in T4. It is content-triggered, not voice-specific. In
    production a Levine episode with such a paragraph falls back to `ash` with an alert.
- **Fixture.** `fixtures/t5/cut-multi.json` (a Rundown chunk with "dollars") intervals moved
  down by one to v4 coordinates, from the remapped root.

## Deploy

The consumer runs from the main checkout. After merge: `git pull` + restart. Rollback:
`git revert` this commit (restores v3 and the OpenAI entries), restart.

## Watch (with T6's 5-episode review, now across three feeds)

Fallback rate per feed (Levine especially: `finishReason OTHER` exhaustion), retries, omission
flags, cost, and the v3 short-chunk item (unmatched tokens on chunks under 300 tokens;
revisit if a faithful chunk exceeds 12).
