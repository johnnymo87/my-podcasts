# T5 calibration evidence: omission detector (verifier v2)

Bead `my-podcasts-9p3.5`. Plan: `docs/plans/2026-09-30-gemini-tts-t5-calibration-plan.md`.
Raw artifacts (audio, transcripts, labels, ledger, reports):
`/persist/my-podcasts/tts-eval/t5/` (reports in `reports/`, notes and ad-hoc scripts in `notes/`).

## Outcome

| | Before (placeholders) | After (frozen in `8e8c080`) |
|---|---|---|
| Span rule | `>= 12` script tokens and transcript side `<= 0.5x` | unchanged, **plus** any span with `net_missing >= 6` |
| Recall floor | 0.85 | **0.95** |
| ASR thinking | model default (`thinking-default`) | **`thinking_level=LOW`** (`thinking-low`) |
| `VERIFIER_POLICY` | `verifier-v1|gemini-3.8-flash|prompt-v1|temp0|thinking-default` | `verifier-v2|gemini-3.8-flash|prompt-v1|temp0|thinking-low` |

**The claim, in the only form the evidence supports:** on a source-disjoint hold-out (6 episodes,
2 each of Rundown, FP Digest and Levine), the frozen policy caught and localized **all 39 labeled
audio cuts (78 ASR runs)** across seven families (start, end, mid-sentence, whole sentence,
paragraph, predictable text, multiple separated cuts) and size bins 10/20/40/80 tokens (multi:
3x8 and 3x16), with **zero false alarms on 23 independently labeled clean chunks (46 runs)**, zero
unavailable ASR, and no confirmed ASR reconstruction of removed words (reconstruction was tested
only on `predictable` cuts, i.e. intervals containing a quotation or a repeated 3-gram, not on
familiar facts or idioms). It is **not** a guarantee
that every omission of 6+ tokens is caught. Zero misses in 39 correlated positives bounds the
miss rate only loosely (the one-sided 95% bound for 39 independent positives is about 7.4%).

**Out of scope, measured:** the smallest contiguous labeled omission tested was 8 tokens (the
3x8 multi cuts); per interval those aligned to a net deficit of 7, so detection clears M=6 by one
token while clean chunks sit three tokens below it. A real 6-7-token skip is untested and often
aligns to 5, which is missed; a contiguous omission of 5 tokens or fewer is not flagged; scattered small losses are caught only through recall (text simulation: 5 separate 5-token
losses caught 22/23 at floor 0.95; 3 separate 4-token losses 0/23).

## Method (what was done, and the substitutions)

- **Corpus** (`corpus.json` sha256 `22df3625…`): 12 episodes, frozen before any paid call.
  Rundown and FP from the daily script archive (the exact TTS text); Levine from the raw emails
  through the email path's own text steps (byte-identical to `in-levine.txt` for 2026-09-24).
  Dev: rundown/fp 2026-09-24 and 09-28, levine 09-22 and 09-24. Hold-out: rundown/fp 09-23 and
  09-29, levine 09-28 and 09-29. Two production chunks per episode (index 0 and n//2), each on
  Flash and Flash-Lite (Kore or Charon), style "calm, measured news anchor": 48 base renders.
- **Labels are independent of the evaluated ASR.** Nobody on the agent side can listen, so every
  base was screened by an independent ASR (OpenAI `whisper-1`, word timestamps); 11 suspects went
  to the owner as short clips. **Whisper was the noisy party:** the owner heard every word in 9 of
  the 10 clips judged (whisper had dropped them), and confirmed one real skip (below). One
  recall-only suspect with no usable clip stayed `uncertain` and was excluded. Controls were never
  chosen because the verifier passed them.
- **Cuts are labeled by construction:** a script-token interval whose boundary words whisper timed
  reliably (>= 50 ms, no zero-length or overlapping neighbour), cut sample-exactly from the
  gemini PCM at an energy-snapped point between words. Each cut was then re-whispered:
  68 + 14 supplementary cuts, of which those with a boundary error of more than 3 tokens or a
  removed-clip mismatch were discarded; cuts with a boundary error of 1-3 tokens were kept as
  **approximate** labels (lower bound = nominal minus leftover). Dev 37 cuts (22 exact, 15
  approx), hold-out 39 (28 exact, 11 approx). The supplements (`cuts-supplement-m1/p1.json`)
  were added before any Gemini ASR of any cut, because the strict check had discarded both
  `multi` cuts.
- **Hold-out discipline:** the rule, thresholds and ASR policy were frozen and committed
  (`8e8c080`) before the hold-out was transcribed once (`holdout-run.json`); hold-out reports
  were run at the frozen point only.
- **Spend** (ledger, worst case for any call with unknown usage): **$9.55** of the $15 ceiling.
  About $2 of that is the Flash-Lite deadline probe booked at worst case because two transient
  errors left its token totals unknown; real spend is nearer $7.5.

## Results

### Thinking pilot (dev, 8 clean chunks + 8 cuts incl. 3 predictable, 2 repeats, both policies)

| policy | detection | false alarms | reconstructions | median / p90 s per chunk | thinking tokens (mean / max) |
|---|---|---|---|---|---|
| default | 10/16 at old grid, identical to low | 0/16 | 0 | 5.1 / 9.9 | 846 / 3888 |
| low | identical | 0/16 | 0 | 3.3 / 3.9 | 0 / 0 |

Low was chosen: equal on safety, faster, cheaper, less variable; the worst clean margin was also
smaller under default (one Levine number-heavy chunk at `net_missing` 4 vs 2).

### Margins (policy low)

| | clean chunks: max `net_missing` | clean chunks: min recall | cuts: min `net_missing` |
|---|---|---|---|
| dev (46 runs / 74 runs) | 2 | 0.973 | 8 |
| hold-out (46 / 78) | 3 | 0.973 | 8 |

### Dev grid (selection), policy low

Only `net_deficit_min = 6` caught and localized all 74 dev cut runs; 8 localized 72/74; 10 or more
missed every 10-token cut. No grid point produced a false alarm on dev. The declared grid
(`M` 12-24, floor 0.85-0.93) was extended down to `M` 6 and floor 0.96 on dev after the pilot
showed the gap between clean chunks and cuts; that is selection on dev, not the hold-out. The
floor 0.95 (over 0.93) is a deliberate conservative choice for scattered loss, at a margin of
0.023 above the lowest clean chunk.

### Hold-out (acceptance, frozen point only): **passed**

78/78 cut runs caught and localized (by family: end 10, mid-sentence 14, multi 6, paragraph 4,
predictable 20, sentence 12, start 12), 0/46 false alarms (Flash 22, Flash-Lite 24), 0
unavailable, 0 reconstructions.

### Published OpenAI episodes (secondary, whole-episode projected, verifier v2)

10/10 pass: rundown and fp 09-23/24/28/29, levine 09-22 and 09-28; whole recall 0.990-0.995,
max `net_missing` 1, min projected chunk recall 0.975. One Levine run first returned
`unavailable` (Gemini ASR HTTP 503, "high demand") and passed on retry; in production that
first outcome would have been an `asr_unavailable` fallback to OpenAI.

### The real Levine skip is reproducible, and it is Flash's

The 2026-09-24 Levine chunk 0 on `gemini-3.8-flash-tts` dropped text in **5 of 9 renders**
(corpus base + 8 repro attempts; Charon, Puck, Kore all affected), every one confirmed by both
ASRs and, for the corpus base, by the owner by ear: 57 tokens twice (the original skip:
"…100 similar companies that are not on the list…"), 24 tokens twice (a list item, "Companies
that you've been meaning to call…"), 32 tokens once. Every one is flagged by the frozen policy
(trimmed fixtures `pipeline/tts/fixtures/t5/levine-*.json`). The skips cluster on list-like,
repetitive passages. Flash-Lite did not skip this chunk in the corpus.

### Deadline (full 2026-09-24 Levine, 22.0k chars, 9 chunks, production `render_episode`, no fallback, no cache)

| model | outcome | Gemini phase | retries |
|---|---|---|---|
| Flash-Lite, Kore | ok | 175 s of 360 | 2 (chunk 1 `finishReason=OTHER` twice, the known content-transient paragraph) |
| Flash, Kore | ok | 127 s of 360 | 0 |

One run each: feasibility, not tail latency.

## Open risks for the rollout (T6+)

- **Flash skips on Levine-like text, often.** The detector catches it, so the cost is a re-render
  and, after a second skip, an OpenAI fallback; Levine is already scheduled last.
- **Gemini ASR availability:** one 503 in about 320 ASR calls; each is a whole-episode fallback.
- **5-token-or-smaller contiguous omissions** and small scattered losses below about 5% of a chunk
  are not claimed.
- **Short tail chunks were not sampled** (chunks 0 and n//2 only). The recall floor is a fraction,
  so on Levine's ~34-token sign-off chunk two mismatched names fail it; windowed replay of the
  clean transcripts puts recall below 0.95 in about 6% of 34-token windows and 3% of 70-token
  windows. Bead `my-podcasts-9p3.14`, before T6.
- **Currency normalization is asymmetric:** `$15.51` normalizes to dollars-and-cents words, but a
  transcript that drops the `$` reads "fifteen point five one", and Gemini ASR is not consistent
  about `$`. A price list can produce a false omission. Rundown/FP scripts spell numbers out
  (no `$`+digit in 132 recent Rundown scripts); Levine does not. Bead `my-podcasts-9p3.15`, before Levine flips.
- **Production omission verdicts are not diagnosable yet:** the Gemini phase records only status,
  reasons and recall, not the flagged span or transcript. Bead `my-podcasts-9p3.14`, before T6.
- **Correlated evidence:** 12 source episodes, two voices, 3-minute chunks. Feeds with different
  text shapes (e.g. transcripts, tables) are untested.
