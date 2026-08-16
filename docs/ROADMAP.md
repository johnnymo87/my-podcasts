# my-podcasts Roadmap

The durable spine for multi-session work. **Beads are the source of truth for
issue detail; this file is the source of truth for order and rationale.** It
exists so that after a context compaction — or six months away — the next
session can pick up the thread without re-deriving why the work is sequenced
this way.

Keep it short. If a section stops being true, fix it or delete it.

---

## How a piece of work is executed

Every numbered piece below runs the same loop. Skip a step only with a reason.

1. **Compact** before starting, so the piece gets a full context window.
2. **Oracle consult** (`oracle-fable`) — optional; use it when there is a real
   design fork, not for mechanical work.
3. **Measure before designing.** This project's plans have been wrong about
   their own premises more often than not (see "Hard-won lessons"). Establish
   the numbers first.
4. **Plan** (`superpowers:writing-plans`) into `docs/plans/YYYY-MM-DD-*.md` for
   anything multi-task.
5. **Adversarial review of the plan** (`adversarial-reviewer-fable`) *before*
   writing code. This has caught a blocker in every piece so far.
6. **SDD** (`superpowers:subagent-driven-development`) — one implementer per
   task, TDD, mutation-test every behavioral change.
7. **Adversarial review of the diff** (`adversarial-reviewer-fable`).
8. **PR**, CI green, merge.
9. **Deploy = `sudo systemctl restart my-podcasts-consumer`.** Merging does not
   deploy. Check for in-flight jobs first.
10. **Update this roadmap and the beads.** File follow-ups for anything found
    and descoped. `bd dolt push`.

Work in a fresh worktree (`.worktrees/<name>`), never in the shared checkout.

---

## Where we are

**Done (2026-08-15), the Rundown content-acquisition arc:**

| | Bead | PR | What it bought |
|---|---|---|---|
| Piece 0 | `llm` | #6 | CI unblocked (red since June) |
| Piece 1 | `6yo` | #7 | Repaired the dead Exa enrichment path |
| Piece 2 | `vxd`, `nkd` | #8 | Funnel reporter → Telegram + `run-stats.jsonl` |
| Piece 3 | `85c`, `kyk` | #9 | Open-access substitution for paywalled sources |
| Piece 4 | `78b` | #10 | Enqueue-only daily CLI — stops the double-publish |

**Pending verification:** the first production run exercising Pieces 1-4 is the
next weekday 04:30 ET run. A self-wake is scheduled for 2026-08-17 ~06:00 ET.
`/persist/my-podcasts/run-stats.jsonl` did not exist as of 2026-08-15. On that
run expect **exactly one** episodes row per feed, a funnel report in Telegram,
and the timer units completing in seconds rather than minutes.

---

## The spine

Ordered. The rationale for the order matters more than the order itself — if a
reason stops holding, re-order.

### ~~1. Fix the daily double-publish race — `my-podcasts-78b`~~ — DONE (PR #10)

Shipped 2026-08-15. The CLI is now enqueue-only and the consumer is the single
executor; the race is gone by construction rather than coordinated around. The
16 duplicated keys were resolved against the **live R2 objects** (newest matched
15/16; the 16th was byte-identical, so keep-newest held) and the feeds
regenerated — 115/116 items, one per date, enclosure lengths verified.

Two lessons worth keeping:

- The atomic-claim option was rejected on purpose. It needs lease/timeout
  machinery to survive a crashed runner — real complexity spent protecting a
  duplicate code path that should not exist. Deleting the second executor was
  both smaller and stronger. Prefer removing a participant to coordinating one.
- **The fix nearly reintroduced its own bug.** The first date guard used a bare
  `strptime`, which accepts `2026-8-5` — a string distinct from `2026-08-05`, so
  `UNIQUE(date_str)` would not collide them and the feed would carry two
  episodes for one day. Caught by adversarial review of the diff.

### 1. Stop the writer silently dropping stories — `my-podcasts-a3x` (P2)

**Why here:** it undermines the instrumentation everything else now depends on.
`build_rundown_prompt` iterates `plan.themes` and takes
`articles_by_theme.get(theme, [])`, so a directive whose `theme` doesn't exactly
match a plan theme has its text dropped from the prompt — while
`writer_inputs.json` still counts it as "with text". The funnel can therefore
report a healthy `paywalled+exa` for a story the model never saw. Fixing this
before trusting a fortnight of `run-stats.jsonl` is cheaper than distrusting the
data later.

### 2. Unify directive→article matching — `3yb` + `5m3` + `4pw` (P2/P3)

**Why grouped:** one root cause, three symptoms. Levine headlines come from
sentence extraction and can carry a double space that Gemini normalizes when it
echoes them back; anything matching on raw headline equality loses those.
Measured: 3 of 38 selected directives. Piece 3 already fixed the third instance
(the Exa trigger) by matching on slug.

- `3yb` — the Exa trigger and the delivery resolver use *two different joins*
  that agree today only by coincidence.
- `5m3` — FP routing writes an empty url/snippet on a miss.
- `4pw` — `_slugify` is duplicated byte-identically in two modules; drift would
  silently orphan every enrichment file.

Likely collapses into one helper. Check before doing them separately.

### 3. Exa hardening batch — `avf` + `d8w` + `j7f` + `gz4` (P3)

**Why grouped:** all four touch `exa_client.py` or its immediate callers and
share test surface. None is urgent alone; four separate PRs would be waste.
Origin exclusion by registrable domain; non-daemon timeout thread; unguarded
`read_text` + empty-slug glob; FP's ungated Exa reader.

### 4. Writer robustness — `ne0` + `qd5` (P2)

Malformed closing tags in `_extract_script`; FP Digest hallucinating a "thin
news day" briefing from an empty plan. Both are "the LLM did something we didn't
expect and we published it anyway" — same family, sensible together.

### 5. Set funnel thresholds — `my-podcasts-3qs` (P3) — **calendar-gated**

Do **not** start before ~2026-08-31. Needs ~2 weeks of real `run-stats.jsonl`
weekday history. Guessing thresholds inside a project premised on not yet having
numbers is self-refuting; `include_in_episode` measured 4-5 on ten consecutive
runs, so any obvious rule fires on normal days. Threshold *ratios* against the
day's stub count, never absolute counts — see the scale note below.

### 6. Polish — `2sf`, `2v3`, `cqc`, `cgn` (P3/P4)

Record *why* a directive resolved to nothing; surface open-access URLs in show
notes; reconcile the funnel design doc with the rendered report; triage 111
mypy errors and make the step blocking.

---

## Facts that must survive compaction

Details live in `bd remember` (`bd remember --list`); these are the ones whose
loss would cause an actively wrong decision.

- **Deploy is a restart.** `my-podcasts-consumer` runs `uv run python -m
  pipeline consume` against the **live working tree** as a long-lived loop with
  `Restart=on-failure`. Merging to `main` does **not** deploy. Every
  intermediate commit must be independently safe; an import error is a
  crash-loop. Check `pending_the_rundown`/`pending_fp_digest` in
  `/persist/my-podcasts/state.sqlite3` before restarting.
- **Scale of the open-access feature.** It upgrades **~1.2 of ~4.75 selected
  stories per episode** (per-run stubs `[0,1,0,3,1,2,1,2]`). The widely-quoted
  "93% stub rate" is per *Levine file*, and most Levine files are never
  selected. **`EXA 2 flagged` is a healthy day; 0 is legitimate on a light one.**
  A plan draft that guessed 5-9 was wrong by ~5x and would have caused a healthy
  run to be diagnosed as broken. A single run can neither confirm nor refute the
  feature.
- **Retrieval is not the bottleneck.** Measured 12/12 Exa hit rate on raw
  headlines with no steering, 10/12 with near-full text, 8/12 ranking the
  paywalled origin first. Do not redesign queries on a hunch. If hit rate
  collapses, first suspect is `directive.exa_query` underperforming the raw
  headline — a one-line change.
- **Article markdown goes VERBATIM into the writer prompt.** Metadata belongs in
  sidecars (`tiers.json` is the pattern), never in the article text.
- **Durable data to `/persist/my-podcasts/`**, never `/tmp` (reaped at 10 days).
  `MY_PODCASTS_WORK_DIR_BASE` keeps the test suite out of the host's `/tmp`.
- **Telegram** is only `POST http://127.0.0.1:4731/alert`, plain text, no
  `parse_mode`. Severity stays `info` until `3qs`.
- **Never** run `git stash`/`reset`/`checkout --`/`restore`/`clean` in the shared
  checkout, and say so explicitly to every subagent — one violated it anyway.

## Hard-won lessons

Kept because each cost real time and each recurred.

- **Plans are wrong about their own premises.** The Piece 2 plan was wrong seven
  times, including a line that shipped a permanently-dead flag. The Piece 3 plan
  overstated impact by 5x and contained a `with ThreadPoolExecutor(...)` sketch
  that would have shipped a timeout that did nothing. Verify against the code
  and the data, never against the prose — including prose I wrote.
- **Adversarial review has found a real defect in every piece.** Twice it found
  silently-wrong instrumentation that a mutation-checked suite had passed.
  Review the plan *and* the diff.
- **Prefer the deterministic signal you already have to an LLM's guess.** The
  transcript detectors, and the Exa trigger (`source_tier` vs the editor's
  `needs_exa`, which fired on 4% of directives while 93% needed it).
- **The worst outcome is a feature that looks live and delivers nothing.** This
  project has shipped that twice. When building one, ask what would make its
  absence visible, and build that at the same time.
- **Prefer removing a participant to coordinating participants.** The
  double-publish race had two obvious fixes (a DB claim, a file lock) and one
  better one: delete the second executor. Coordination would have preserved a
  duplicate code path that had already silently drifted behind its twin.
- **Check the recovery path for the bug you just fixed.** The enqueue-only
  change would have left the documented consumer-down procedure
  (`publish-script`) reintroducing the duplicate, because it never closed the
  job row. A fix is not done until the manual workaround around it is also safe.
- **Measure the remediation, not just the fix.** "Keep the newest row" was
  inherited guidance from a prior cleanup. Re-checking all 16 keys against the
  live R2 objects confirmed it — but the prior measurement had a known 1-in-147
  mismatch, so confirming took one command and assuming would have silently
  pointed a feed entry at the wrong artifact.
