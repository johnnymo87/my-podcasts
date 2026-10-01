# Recall floor on short tail chunks (verifier v3) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Stop the omission detector's recall floor from false-alarming on short chunks
(Levine's 34-token sign-off, FP's 11-token date sign-off, Rundown tails of 70-200 tokens),
without changing any verdict on chunks of 300+ tokens. Bead `my-podcasts-9p3.14`, a T6
blocker (epic `my-podcasts-9p3`).

**Architecture:** The recall floor (0.95) is a fraction. T5 calibrated it only on chunks of
306+ tokens, and on short chunks one or two mismatched names push recall below it. The ASR
mishears the same names on a re-render, so the chunk fails twice and the whole episode falls
back to OpenAI with an alert. The rule changes from "recall < floor" to "**padded recall** <
floor": unmatched tokens are measured against `max(n_tokens, recall_min_tokens)`, with
`recall_min_tokens = 300`. For n >= 300 the computation is the same expression as v2, so
every T5 verdict is unchanged by construction. Below 300 tokens the floor becomes an
absolute bound: more than 15 unmatched tokens. `Analysis.recall` and
`ChunkProjection.recall` keep reporting true recall.

The span rules (`net_deficit_min=6`, long-and-mostly-gone) are untouched. A contiguous
omission of 6+ net tokens in a short chunk is still caught. This change gives up only
**scattered** small losses in chunks under 300 tokens that total 15 tokens or fewer. The
evidence doc quantifies that by text simulation and says so.

**Tech Stack:** Python 3, pytest, ruff, `uv run`.

**Method source:** an oracle consult (oracle-opus; astra was down) recorded on the bead
(`bd show my-podcasts-9p3.14`, note 2026-10-01). The chunker (option c) is out of scope
unless the paid evidence shows edge effects on short clips. A chunker change would need a
`RENDERER_VERSION` bump and would break the T5 corpus rebuild.

**Interaction with PR #29** (`tts-t6-prereqs`, open): that PR adds `Span.heard` and
`Verdict.transcript` to `verify.py`. Whichever PR merges second rebases. The conflict is
small (different functions), but the rebase must keep both changes.

---

## Rules for the implementer

- Work in `/home/dev/projects/my-podcasts/.worktrees/tts-tail-recall`, branch
  `tts-tail-recall`. Commit with bare `git commit` (no identity flags).
- TDD: write the failing test, watch it fail, implement, watch it pass.
- Run `uv run ruff check . && uv run ruff format --check .` before each commit; ruff is
  the CI gate. The full suite (`uv run pytest -q`, about 2.5 min) must pass before the last
  commit of each task.
- Tests stay hermetic: no network, no `/persist`. The autouse guards in
  `pipeline/conftest.py` enforce this.
- Do not touch the chunker, `RENDERER_VERSION`, the ASR prompt or `ASR_POLICY`.

---

### Task 1: `recall_min_tokens` and one recall-failure helper

**Files:**
- Modify: `pipeline/tts/verify.py` (`VerifyThresholds`, `_analysis`, `project_chunks`,
  module docstring)
- Test: `pipeline/tts/test_verify.py`

**Behavior**

- `VerifyThresholds.recall_min_tokens: int = 300`. It is the last field, so positional
  construction elsewhere is unaffected. Check whether anything builds `VerifyThresholds`
  positionally (`grep -rn "VerifyThresholds(" pipeline`).
  - Validation: `>= 1`, else `ValueError("recall_min_tokens must be >= 1, got ...")`.
  - `recall_min_tokens=1` reproduces v2 exactly. That is the replay switch used for
    comparisons.
- One module-level helper, used by **both** `_analysis` and the per-chunk loop in
  `project_chunks` (no second copy of the rule):

  ```python
  def _recall_fails(matched: int, total: int, th: VerifyThresholds) -> bool:
      """The recall-floor rule (v3): unmatched tokens measured against at least
      ``recall_min_tokens``. For ``total >= recall_min_tokens`` this is exactly
      ``matched / total < recall_floor`` (the v2 rule, same float expression),
      so long-chunk verdicts are unchanged; below it the floor is an absolute
      bound of ``(1 - recall_floor) * recall_min_tokens`` unmatched tokens."""
      if total <= 0:
          return False
      padded = max(total, th.recall_min_tokens)
      return (padded - (total - matched)) / padded < th.recall_floor
  ```

  For `total >= recall_min_tokens`, `padded - (total - matched) == matched` (integers), so
  the float expression is `matched / total`, byte-identical to v2's `n_matched /
  len(script)`. Keep it in that form. Do **not** rewrite it as
  `unmatched > (1 - floor) * n`, because `1 - 0.95` is not exact in floating point.
- `_analysis`: `if _recall_fails(n_matched, len(script), th): reasons.append("recall_below_floor")`.
  The reason names stay the same, so reports and calibration code keep working.
- `project_chunks`: `bad = flagged > 0 or (toks and _recall_fails(n, len(toks), thresholds))`.
  The `chunk_recall_below_floor` reason is unchanged.
- Update the comment on `Analysis.reasons` and the module docstring. Recall is still true
  recall. The floor is applied to padded recall, and short chunks get an absolute bound.

**Tests (write first, see them fail)**

1. `test_recall_min_tokens_defaults_to_300_and_is_validated`: default 300; `0` and `-1`
   raise `ValueError`.
2. `test_long_chunks_keep_the_v2_rule_exactly`: exhaustive over `total` in 300..1200 and
   every `matched` in 0..total. `_recall_fails(m, n, DEFAULT_THRESHOLDS) == (m / n < 0.95)`.
   Import the private helper; this pins the byte-identity claim.
3. `test_recall_min_tokens_1_is_the_v2_rule_for_every_length`: for total 1..400 and every
   matched, `_recall_fails(m, n, replace(DEFAULT_THRESHOLDS, recall_min_tokens=1)) == (m / n < 0.95)`.
4. `test_short_chunk_bound_is_15_unmatched_tokens`: for total in (11, 34, 70, 150, 299),
   15 unmatched passes and 16 fails (when `total >= 16`). Also: total 11 with 11 unmatched
   passes the recall rule (a wholly missing 11-token chunk is a span-rule matter, see test 6).
5. `test_short_signoff_with_two_misheard_names_passes` (`analyze` level): a 34-token script
   in the shape of a sign-off (write your own neutral sentence, not Bloomberg text) and a
   transcript with 2 substituted words. v3 passes with reasons `("ok",)`; under
   `replace(DEFAULT_THRESHOLDS, recall_min_tokens=1)` the same pair is an omission with
   `recall_below_floor`. `a.recall` is the true recall (32/34).
6. `test_short_chunk_contiguous_drop_is_still_flagged_by_the_span_rule`: a ~70-token script
   with 8 contiguous tokens dropped is an omission via `long_unmatched_span`. An empty
   transcript against an 11-token script is also flagged by the span rule.
7. `test_project_chunks_uses_padded_recall_per_chunk`: an episode of one long chunk
   (`SCRIPT`, the existing fixture) plus a ~34-token tail chunk, with 2 tail words
   substituted. The tail's projection is `pass` and the whole is `pass`; under
   `recall_min_tokens=1` the tail projects `omission` and the whole gets
   `("chunk_recall_below_floor",)`. The tail's `ChunkProjection.recall` is its true recall.

**Commit:** `Measure the recall floor against at least 300 tokens so short chunks stop false-alarming`

---

### Task 2: verifier v3, the tts-verify flag, pinned strings

**Files:**
- Modify: `pipeline/tts/verify.py` (`VERIFIER_VERSION = "3"`; the `VerifyThresholds`
  docstring adds one paragraph on `recall_min_tokens` and its v3 origin)
- Modify: `pipeline/__main__.py` (`tts-verify`: `--recall-min-tokens` INT option, added to
  `overrides`)
- Modify tests that pin v2: `pipeline/tts/test_verify.py` (`VERIFIER_VERSION == "2"`
  assertions, `test_calibrated_defaults_are_frozen` adds `recall_min_tokens=300`,
  `test_production_policy_strings_are_pinned` becomes `verifier-v3|...`),
  `pipeline/test_tts_verify_cli.py` (threshold dicts gain `"recall_min_tokens": 300`;
  `verifier_version` becomes `"3"`), `pipeline/tts/test_t5_fixtures.py` (see below). Grep
  for any other hardcoded `verifier-v2`: `grep -rn "verifier-v2\|VERIFIER_VERSION ==" pipeline`.

**`test_t5_fixtures.py`:** the fixtures are T5 evidence captured under v2. Rename
`test_fixtures_are_small_and_the_calibration_is_verifier_v2` to
`..._and_v3_keeps_every_t5_verdict` and assert `VERIFIER_VERSION == "3"`. Update the
module docstring: v3 differs from v2 only on chunks under `recall_min_tokens` tokens, and
every T5 chunk had 306+ tokens. Add one test: for every fixture,
`analyze(script, transcript, DEFAULT_THRESHOLDS)` and the same with `recall_min_tokens=1`
give equal `status` and `reasons`. The fixture's own expectations still hold unchanged. Do
not edit any fixture JSON.

**Test for the CLI flag:** `--recall-min-tokens 1` is echoed in the report thresholds;
`--recall-min-tokens 0` exits 2 (BadParameter via the existing `ValueError` path).

**Cache:** `cache.cache_key` folds `VERIFIER_POLICY`, so a Gemini-primary cache entry made
under v2 becomes a miss. No feed is on Gemini, so nothing in production is affected. Check
that `pipeline/tts/test_cache.py` still passes; it reads `VERIFIER_VERSION` dynamically.

**Commit:** `Verifier v3: the recall floor change bumps VERIFIER_VERSION`

---

### Task 3: docs

Done after the evidence steps below, with their numbers. The controller supplies the numbers
to the resumed implementer.

**Files:**
- `docs/plans/2026-09-30-gemini-tts-t5-evidence.md`:
  - New section "Short tail chunks (verifier v3, bead 9p3.14)": rule, T5 replay identity,
    windowed replay, tails corpus, paid results under v2 and v3, what v3 gives up
    (scattered-loss simulation), spend.
  - Edit the "Outcome" table: add a v3 row/column for the recall floor.
  - The open-risk bullet "Short tail chunks were not sampled" becomes resolved, with a
    pointer.
  - The stale bullet "Production omission verdicts are not diagnosable yet" says PR #29 /
    `9p3.13` only if that PR has merged by then; otherwise leave it.
- `pipeline/AGENTS.md`, "Verifier (T2)":
  - The claimed-scope bullet replaces "Short tail chunks ... were not in the calibration
    sample" with the v3 rule and its limit.
  - The thresholds bullet adds `recall_min_tokens=300`.
  - Update `verifier-v2` mentions that describe the current policy. Historical T5 text
    stays v2.
  - The `tts-verify` usage line lists `--recall-min-tokens`.
- `docs/plans/2026-09-30-gemini-tts-design.md`, Amendments: one dated line for v3.

**Commit:** `Document verifier v3 and the tail-chunk evidence`

---

## Evidence steps (controller, not the implementer)

Artifacts go under `/persist/my-podcasts/tts-eval/tail-recall/`, scripts under `notes/`.
The paid step uses `calibrate_run`'s `Ctx`, `Ledger`, `synth_one`, `asr_one` and
`whisper_one` (same write-ahead ledger, no-clobber, lock and wall-clock bounds as T5) from
a standalone script with an `if __name__ == "__main__":` guard. `tts-calibrate` itself
cannot take this corpus: its `corpus` step requires exactly 4/4/4/6 episodes and selects
chunks 0 and n//2.

**E1. Free replay (after Task 1).**
- Replay every T5 record (bases and cuts, policies low and default, dev and hold-out) under
  v3 and under `recall_min_tokens=1`. Every `calibrate.replay` outcome must be identical.
  Then regenerate `report --split dev|holdout --policy low --deficit 6 --floor 0.95` under a
  new `--name` (`*-v3`). The acceptance numbers must equal the v2 reports.
- Windowed replay of the 92 clean low-policy transcripts at windows 11, 21, 34, 47, 70, 100,
  150 and 200 tokens. Expect v2 fires at roughly the bead's rates and **0 v3 fires**.
- Scattered-loss text simulation on the real tails (below): k separated 3/4/5-token
  deletions. Report detection under v2 and v3; this is what v3 gives up.

**E2. Freeze the tails corpus before paying** (`tails.json`, refuses to regenerate).
The exact TTS text uses the same rules as T5: the Rundown archive as-is, FP `.strip()`,
Levine from `t5/texts/`. Take the last chunk of
`chunk_text(text, ceiling=GeminiProvider.max_chars)`. 18 tails (token counts measured in
`notes/tail-lengths.txt`):

| bin | tails |
|---|---|
| < 60 | fp 2026-09-03 (11), fp 05-14 (21), fp 07-09 (21), levine 09-22 sign-off (34), fp 05-21 (47), fp 08-19 (56) |
| 70-100 | rundown 09-23 (70), fp 08-25 (79), rundown 04-03 (80), rundown 03-19 (99) |
| 100-150 | fp 09-30 (109), rundown 09-03 (111), rundown 09-28 (116), fp 07-21 (138) |
| 150-200 | rundown 09-18 (150), fp 09-24 (176), levine 09-29 headline list (184), rundown 09-29 (198) |

The voice alternates Kore/Charon by index (as in T5). Style: "calm, measured news anchor".

**E3. Paid renders:**
- **Synth:** each tail on Flash and Flash-Lite, 2 synth repeats, plus 3 more repeats of the
  Levine sign-off per model. That is 78 synths.
- **Gemini ASR:** 2 low-policy runs per render, 156 calls.
- **Whisper:** on every render that either Gemini ASR run flags under v2 or v3, or whose
  first/last 3 script tokens are unmatched.
- **Cost:** expected about $1.2; ledger budget cap $2.0.

**Acceptance (on renders judged faithful by whisper agreement):**
- 0 v3 omissions;
- 0 `unavailable`;
- no span with `net_missing >= 3`;
- no clipped first/last 3 tokens.

Also report v2 fire counts.

**What a failed acceptance means:**
- **A v3 flag that whisper confirms is a real omission, not a false alarm.** Record it; it
  is evidence about short-clip synthesis, not against the rule.
- **Edge clipping or an unconfirmed v3 flag** means stop and reconsider (chunker balancing,
  option c) before shipping.

**E4.** The Levine headline-list result goes to bead `my-podcasts-9p3.15` as a note.

## Review and ship

- Spec-reviewer, then code-reviewer, per task (SDD). Fixes go through the implementer's
  resumed task_id.
- Pre-PR adversarial review on the full diff plus the evidence. Run `astra-probe` first; if
  DOWN, use `adversarial-reviewer-opus` and say so in the PR.
- PR via the `shepherding-pull-requests` skill. Bead notes, then `bd dolt push`. The bead
  closes at merge.
