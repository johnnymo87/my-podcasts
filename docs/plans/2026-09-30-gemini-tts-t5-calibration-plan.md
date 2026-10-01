# T5 Omission-Detector Calibration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement the
> code tasks (1-3, 5) task-by-task. Phase R (the paid runs) is executed by the controller, not an
> implementer.

**Goal:** Replace the placeholder omission-detector policy (`verify.DEFAULT_THRESHOLDS`, and the
ASR's implicit "thinking-default") with a policy chosen on independently labeled, production-
equivalent evidence, before any feed moves to Gemini. Commit the chosen constants with an evidence
summary. No feed changes in T5.

**Bead:** `my-podcasts-9p3.5` (epic `my-podcasts-9p3`); its notes are requirements. **Design:**
`docs/plans/2026-09-30-gemini-tts-design.md`, "Verification" (calibration bullet) and "Testing".

**Oracle consult (astra, 2026-09-30), adopted:** label the audio intervention, not its ASR
consequences; never select a control because the verifier passed it; split by source episode with
an untouched hold-out; start from an additive per-span net-deficit rule; decide the thinking
config by paired measurement; report omission detection, safe rejection and availability
separately; claim only "caught all X labeled hold-out cuts in these size bins and families", never
"catches every omission >= N".

## What we can and cannot do, and the substitutions

- **Nobody on this side can listen.** The oracle asked for every negative-control chunk to be
  listened to (48 chunks, ~2.6 h) and every cut labeled by ear. Substitute: an **independent-vendor
  ASR (OpenAI `whisper-1`, word timestamps)** used only for labeling/screening, never in
  production. It is independent of the evaluated ASR (Gemini), so a Gemini reconstruction cannot
  hide in the labels. Anything the screen cannot settle goes to the **owner as short clips**
  (seconds, uploaded unlisted), not tuned away. This substitution is reported to the owner.
- Probe facts (2026-09-30, `/persist/my-podcasts/tts-eval/t5/probe/`): `whisper-1`
  `verbose_json` + `timestamp_granularities=["word"]` works (156 s WAV, 420 words, 12 s).
  `gemini-3.8-flash` rejects `thinking_level=MINIMAL` (400); `LOW` works and reported no thinking
  tokens (3.6 s) vs default (814 thinking tokens, 5.5 s) on the same audio.

## Binding decisions

1. **Corpus (frozen before any paid run, in `corpus.json`).** 12 source episodes: 4 Rundown, 4 FP
   Digest (daily `.txt` archive under `/persist/my-podcasts/scripts/<feed>/<date>.txt`, which is
   the exact TTS input: Rundown verbatim, FP after `.strip()`), 4 Levine (raw email from R2 through
   the email path's text steps: `EmailProcessor.parse` -> adapter `clean_body` -> `prepend_title`;
   Levine is not a transcript feed). The 2026-09-24 Levine (`in-levine.txt`, email
   `inbox/raw/d9dead7b-c34e-4c82-b3cd-f079c7446a9f.eml`) is one of them.
   - **Split by episode:** 6 dev (2 per feed) and 6 hold-out (2 per feed). The 2026-09-24 Levine
     is dev. Every chunk, voice, render, cut and ASR repeat of an episode stays on its side.
   - **Chunks:** `chunk_text(text, ceiling=GeminiProvider.max_chars)` exactly as production.
     Two per episode, deterministic: index 0 and index `n // 2` (or 1 if n == 2; one if n == 1).
   - **Renders:** each selected chunk on `gemini-3.8-flash-tts` and `gemini-3.8-flash-lite-tts`,
     style "calm, measured news anchor", voice Kore for even corpus index, Charon for odd.
     48 base renders. PCM (the provider's exact output) is saved before anything else.
2. **Base labeling (independent of the evaluated verifier).** Whisper each base. Align whisper
   words to the chunk's normalized script tokens with the production normalizer/aligner.
   Label `faithful` when whisper shows no span with `net_missing >= 6` and whisper recall
   >= 0.95; otherwise `suspect`. Suspect bases go to the owner as clips (the suspect span +/- 3 s);
   the owner's call makes them `faithful`, `natural_omission` (a positive fixture) or
   `defect`. `uncertain` stays reported, never silently dropped. Only `faithful` bases are
   negative controls and cut sources.
3. **Cuts: labeled by construction.** For a faithful base, pick a script-token interval, map its
   first and last tokens to whisper words that the alignment matched exactly, and cut the PCM from
   the midpoint of the inter-word gap before the first word to the midpoint of the gap after the
   last (sample-exact slicing, 16-bit aligned; assert remaining sample count; save the removed
   clip). Label = the script interval (normalized token count and literal text) plus whisper's
   transcript of the removed clip as a sanity check (token counts within 25 percent, else the cut
   is discarded and logged, never relabeled). Labels are frozen before any Gemini ASR of the cut.
   - **Families:** `start`, `end`, `mid_fluent` (inside a sentence), `sentence` (whole
     sentence(s)), `paragraph` (~80 tokens), `predictable` (interval containing a quotation or an
     n-gram of >=3 tokens that also occurs elsewhere in the chunk), `multi` (3 separated cuts of
     ~8 tokens each).
   - **Size bins** (normalized tokens, actual counts reported): 10, 20, 40, 80.
   - ~72 cuts total, balanced across families and across dev/hold-out; seeded, deterministic
     selection recorded in `cuts.json`.
4. **Text-level simulation (dev transcripts only, free).** On real Gemini transcripts of faithful
   dev bases: contiguous deletions at each bin/position, deletion plus local substitution noise
   (including the exact T2 blind spot: 40-token cut + every 3rd of the next 45 tokens substituted),
   multiple separated cuts, repeated-phrase deletions. Used to shortlist rules; not evidence of
   end-to-end sensitivity.
5. **Rule space (small, declared now).** Keep `anchor_min=3` and the existing
   `min_span_words`/`max_span_ratio` path. Add `net_deficit_min` M: a span is also flagged when
   `net_missing >= M`. Grid: M in {12, 16, 20, 24}, `recall_floor` in {0.85, 0.90, 0.93}.
   A sliding-window deficit rule is added only if the `multi` family exposes a consequential miss,
   and then as signed deficit between anchor boundaries (not a sum of per-span `net_missing`).
   Selection criterion on dev: zero false alarms on faithful dev bases (all ASR repeats), then
   maximum dev cuts caught, tie -> the more conservative (smaller M, higher floor).
6. **Thinking policy (dev pilot, paired).** 8 faithful dev bases + 8 dev cuts (>= 3 `predictable`),
   each transcribed twice under `default` and `low` (same bytes, interleaved order). Rank:
   confirmed reconstruction false-passes, false alarms, unavailable/incomplete, verdict
   disagreement across repeats, then latency and cost. Tie -> `low` (cheaper, faster). The choice
   becomes explicit in `asr._generation_config` and `ASR_POLICY` (e.g. `thinking-low`); every
   later run uses it.
   - **Reconstruction check:** for a cut, the removed interval's script tokens matched by the cut's
     transcript (via alignment) beyond what the clean transcript would explain. A confirmed
     reconstruction false-pass stops the calibration: the transcription approach changes and is
     revalidated; thresholds are not lowered to compensate.
7. **Freeze, then hold-out once.** After dev: freeze rule, thresholds, normalizer and ASR policy
   (commit them to the branch, the commit sha recorded in the evidence). Then Gemini-ASR the
   hold-out bases and cuts once. Acceptance: every faithful hold-out base passes and every hold-out
   cut in the declared scope is caught. A hold-out miss fails acceptance; if the policy is then
   revised, that case becomes a dev regression and new hold-out evidence is gathered (a fresh
   episode set), never re-scored as untouched.
8. **Reporting three things separately:** omission detection (cuts caught), safe rejection
   (false alarms on faithful bases), availability (ASR unavailable/incomplete). Unavailable is
   never counted as a detected cut.
9. **Published OpenAI controls (secondary, reported separately).** ~10 published episodes (4
   Rundown, 4 FP, 2 Levine) whose exact TTS text is reconstructable, through `tts-verify`
   (whole-episode segmented, projected) under the final ASR policy and rule. All must pass.
10. **Levine skip reproduction (dev/challenge, not hold-out).** Chunk 0 of the T4 Levine excerpt
    (`/persist/my-podcasts/tts-eval/t4/set/levine/script.txt`, chunked as production) on
    `gemini-3.8-flash-tts`: Charon x3, Puck x3, Kore x2, direct provider calls, PCM saved before
    verification. Gemini ASR (final policy) + whisper each. A render where both ASRs miss the same
    script span is a confirmed natural omission -> fixture; a Gemini-only miss is an ASR false
    positive -> recorded as such. Ambiguous -> owner clip. Identical recall is not evidence of an
    identical span.
11. **Deadline probes.** Full `in-levine.txt` (22.2k chars) through `render_episode` with
    `cache_dir=None`, `fallback=None`, `notify_fallback=False`, manifests under
    `/persist/my-podcasts/tts-eval/t5/deadline/`, final ASR policy, one run each on Flash-Lite and
    Flash (Kore). Report outcome, reason, `elapsed_s`, retries. An early content failure is not a
    fit. Budget changes are T6's decision.
12. **Budget ceiling $15** (planning prices: Flash synth $9.2/M audio tokens, Flash-Lite $6.1/M,
    ~32 tok/s; Gemini ASR $1/M in, $5/M out+thinking; whisper $0.006/min). `ledger.jsonl` records
    each paid call's usage; the harness refuses a new batch when the estimated total would pass
    the ceiling. Unknown usage is costed at the worst case, not zero.
13. **Repo vs /persist.** Repo: harness, tests, the chosen constants, a compact evidence summary
    (`docs/plans/2026-09-30-gemini-tts-t5-evidence.md`), and small regression fixtures. **The repo
    is public:** fixtures use our own generated Rundown/FP script text where possible; a Levine
    fixture is trimmed to a window around the skip (<= 150 words each side). Everything else
    (audio, full texts, transcripts, labels, ledger) lives under `/persist/my-podcasts/tts-eval/t5/`,
    never `/tmp`.
14. **Versions.** Rule/threshold change -> bump `VERIFIER_VERSION`. Thinking change -> new
    `ASR_POLICY` string; `ASR_PROMPT_VERSION` only if prompt text changes (it does not). Both
    change `VERIFIER_POLICY` and so the Gemini render cache key; no feed uses Gemini, so no
    production cache is affected.

## Artifact layout (`/persist/my-podcasts/tts-eval/t5/`)

```
corpus.json                 frozen episodes, split, chunk selection, voices (sha256 of texts)
texts/<episode>.txt         exact TTS text
bases/<base_id>/            chunk.txt, pcm.wav, synth.json, whisper.json, label.json,
                            asr/<policy>-<n>.json (transcript + usage + verdict per rule later)
cuts.json                   frozen cut selection
cuts/<cut_id>/              cut.wav, removed.wav, label.json, whisper-removed.json, asr/...
levine-repro/<attempt>/     pcm.wav, synth.json, whisper.json, asr/...
deadline/                   manifests + summary
published/                  tts-verify reports + transcripts
ledger.jsonl                every paid call: kind, model, usage, estimated $
reports/                    pilot.md, dev.md, holdout.md, published.md
```

## Code tasks

### Task 1 — verifier and ASR knobs (no default behavior change)

`pipeline/tts/verify.py`:
- `VerifyThresholds.net_deficit_min: int | None = None` (None = rule off; validate >= 1).
  `_align` flags a span when the existing test holds **or** `net_deficit_min` is set and
  `net_missing >= net_deficit_min`.
- `Analysis.reasons`: keep `long_unmatched_span` for any flagged span (production code keys on
  it); no new reason string needed. Tests: the exact T2 blind spot (83 vs 43) passes with the rule
  off and is flagged with M=40 or lower; a clean control with small substitutions is not flagged at
  M=12; `net_deficit_min=0` rejected.
- Defaults unchanged, so `VERIFIER_VERSION` stays "1" in this task.

`pipeline/tts/asr.py`:
- `GeminiTranscriber(..., thinking: str = "default")` where `thinking` in
  `{"default", "low"}`; `"low"` sends `ThinkingConfig(thinking_level=LOW)`. The instance exposes
  `policy` (the ASR policy string for its settings). Module `ASR_POLICY` and default behavior are
  unchanged in this task. Unknown value -> `ValueError`.
- Tests: request config matches the declared setting (capture the config passed to a fake
  client); `policy` differs between settings; default path byte-identical to today.

### Task 2 — `pipeline/tts/calibrate.py`: pure core (no network)

Imports only stdlib and `pipeline.tts.*`. Functions (names indicative):
- `cut_pcm(pcm: bytes, intervals: list[tuple[int, int]]) -> tuple[bytes, list[bytes]]`: sample
  intervals, sorted, non-overlapping, in range; returns remaining PCM and removed clips; asserts
  byte math. `wav_bytes`/`read_wav` via `asr.pcm_to_wav` and `wave`.
- `map_words_to_script(script_tokens, whisper_words) -> list[WordMap]`: normalize each whisper
  word to tokens (a word can yield 0..n tokens), align token streams with `difflib`, and return
  for each script token the matched whisper word index and its start/end seconds (or None).
- `screen_base(script_text, whisper, *, total_samples, base_id="", sample_rate=24000) -> BaseLabel` (decision 2). `total_samples` is required and must be the sample count of the PCM the label will cut (whisper's rounded `duration` is not used); `whisper` may be a verbose_json dict or a bare words list.
- `choose_cuts(base, family, size, rng, *, seed=None) -> CutSpec | None` (decision 3): only intervals whose
  boundary tokens are exactly matched *and* trustworthy in time (`boundary_eligibility`: the word and both neighbours last >= 50 ms, nothing overlaps; real whisper timestamps abut, and ~4% of words are zero-length); `mid_fluent` avoids sentence boundaries; `sentence` snaps to
  sentence boundaries; `predictable` requires a quote or a repeated >=3-gram; `multi` is 3
  separated ~8-token intervals. Returns sample intervals and the frozen label.
- `reconstruction(label, script_text, clean_transcript, cut_transcript, *, min_run=3, audio_residue=None) -> dict` (decision 6): levels are per interval (overall = the maximum); `audio_residue` is whisper's transcript of the cut audio and is subtracted from the hits.
- `evaluate(records, thresholds_grid) -> list[dict]` (one result per grid point, with `acceptance_ok`/`acceptance`; `caught_localized` counts a cut only when EVERY removed interval overlaps a flagged span) replaying `verify.analyze` over saved transcripts
  for every grid point; per family/bin/feed/model/split counts; false alarms per faithful base
  and per ASR repeat.
- `simulate(transcript_tokens, script_tokens, spec, rng)` (decision 4) producing synthetic
  transcripts with known deleted script intervals.
- API added after review (all in `calibrate.py`; none changes a signature above):
  `finalize_cut(spec, pcm, *, base_energy=None)` snaps each cut point to the lowest-energy 10 ms frame within
  +/-100 ms (clamped to the midpoints of the adjacent words) and records the nominal and snapped points and each
  snapped frame's RMS relative to the base (`CutSpec.apply` refuses an unsnapped spec);
  `verify_cut_audio(spec, script_text, whisper_cut, *, base=None, window=5)` is the post-cut check on whisper of the
  CUT audio (removed words still audible, kept boundary words missing); `removed_sanity(expected_tokens, whisper_removed)`;
  `replay(record, thresholds) -> Outcome`; `default_grid()`; a cut may not remove more than half the chunk.
- Tests (offline, synthetic PCM and token lists): cut byte math and boundaries, overlapping or
  out-of-range intervals rejected, word mapping with numbers ("$1.5 billion") and a whisper word
  normalizing to 0 tokens, each family's constraints, the exact blind spot through `simulate`, a
  repeated-phrase deletion, reconstruction detection on a crafted transcript, evaluate() counting
  unavailable separately.

### Task 3 — paid collection steps (`python -m pipeline.tts.calibrate <step>`)

A click group in `calibrate.py` behind `if __name__ == "__main__":` (spawn-safe), or a
`tts-calibrate` group in `pipeline/__main__.py` importing lazily — implementer's choice, one of
them. Every step: reads `--root` (default `/persist/my-podcasts/tts-eval/t5`), skips work whose
artifact already exists (resumable, never overwrites), writes atomically, appends usage to
`ledger.jsonl`, and refuses to start when the ledger estimate plus the step's worst case exceeds
`--budget` (default 15.0). Keys from env, only variable names printed.
- `corpus` — build and freeze `corpus.json` + `texts/` (decision 1); `--levine-keys` lists the
  R2 email keys; refuses if `corpus.json` exists.
- `synth` — base renders via `GeminiProvider.synthesize_detailed` with the production retry
  classification (transient -> up to 3 calls; fatal -> record and skip), PCM saved first.
- `whisper` — whisper-1 word timestamps for bases, removed clips, and repro attempts.
- `label` — `screen_base` for all bases -> `label.json`; prints the suspect list with clip paths
  (extracts suspect clips as mp3 for the owner).
- `cuts` — `choose_cuts` over faithful bases -> `cuts.json` + cut/removed WAVs + labels
  (refuses to change an existing `cuts.json`).
- `asr` — Gemini ASR via `GeminiTranscriber(thinking=...)` over a selected set
  (`--split dev|holdout`, `--kind base|cut|repro`, `--policy default|low`, `--repeat n`,
  `--ids`), storing transcript + usage.
- `report` — `evaluate` + markdown tables into `reports/`.
- `levine-repro` — decision 10.
- `deadline` — decision 11 via `render_episode`.
Tests use fake providers/transcribers/clients and `tmp_path` roots; none may touch the network or
`/persist` (the suite's guards enforce it). Include: resumability (second run makes no calls),
budget refusal, fatal synth recorded not raised, ledger worst-case for unknown usage.

## Phase R — controller runs (in order; stop and report on any surprise)

- R1 `corpus` (pick 3 more Levine emails: the most recent Money Stuff emails with published
  episodes; 4 Rundown + 4 FP recent weekdays with published episodes, not the T4 excerpt dates
  except where needed). Commit nothing yet; `corpus.json` sha goes in the evidence.
- R2 `synth` all 48 bases; R3 `whisper` bases; R4 `label`; owner clips for suspects.
- R5 `levine-repro` (+ whisper), inspect.
- R6 `cuts` (all splits, labels frozen); `whisper` removed clips; discard mismatches.
- R7 thinking pilot (`asr --split dev --policy default|low --repeat 2` on the pilot ids); choose
  policy; Task 5a sets it on the branch.
- R8 dev: `asr` all dev bases/cuts under the chosen policy (plus existing pilot transcripts);
  simulation; `report`; choose rule; Task 5b sets it; commit = the freeze.
- R9 hold-out: `asr` hold-out once; `report`; accept or fail per decision 7.
- R10 published controls; R11 deadline probes (after the freeze so they use final policy).

### Task 5 — commit the policy and the evidence

- 5a: explicit thinking in `_generation_config` and `ASR_POLICY`; default of
  `GeminiTranscriber(thinking=)` follows it.
- 5b: `DEFAULT_THRESHOLDS` = chosen values (docstring cites the evidence doc); bump
  `VERIFIER_VERSION` to "2"; update the `VerifyThresholds` docstring (no longer placeholders).
- 5c: regression fixtures in `pipeline/tts/fixtures/` (small): at least one real dev cut per
  family (script chunk from Rundown/FP + saved Gemini transcript + label) asserting
  `analyze(..., DEFAULT_THRESHOLDS)` flags it, two faithful bases asserting pass, and the Levine
  reproduction window if one was confirmed.
- 5d: `docs/plans/2026-09-30-gemini-tts-t5-evidence.md` (corpus sha, split, per-family/bin/split
  tables, false alarms, availability, pilot table, reconstruction findings, published controls,
  Levine repro, deadline, spend, the claim in its honest form, open risks); updates to
  `pipeline/AGENTS.md` ("Verifier" section: thresholds no longer placeholders; thinking policy),
  the design doc Amendments, and `AGENTS.md` if a path changes.

## Acceptance

- Suite green offline; ruff clean.
- Every faithful hold-out base passes under the frozen policy (all repeats run), every declared-
  scope hold-out cut is caught, zero confirmed reconstruction false-passes, published controls pass.
- Spend <= $15, reported with the ledger total.
- Otherwise: report the failure honestly with the evidence; do not ship tuned-away constants.
