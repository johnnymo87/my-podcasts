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
| Piece 5 | `a3x`, `2sf` | #11 | Writer prompt built from ordered sections |

**Pending verification:** the first production run exercising Pieces 1-4 is the
next weekday 04:30 ET run. A self-wake is scheduled for 2026-08-17 ~06:00 ET.
`/persist/my-podcasts/run-stats.jsonl` did not exist as of 2026-08-15. On that
run expect **exactly one** episodes row per feed, a funnel report in Telegram,
and the timer units completing in seconds rather than minutes.

---

## The spine

Ordered. The rationale for the order matters more than the order itself — if a
reason stops holding, re-order.

### ~~Fix the daily double-publish race — `my-podcasts-78b`~~ — DONE (PR #10)

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

### ~~Stop the writer silently dropping stories — `a3x` + `2sf`~~ — DONE (PR #11)

Shipped 2026-08-16. The two structures that had to agree (a themes list and a
dict keyed by directive theme) are replaced by one ordered section list built in
`_assemble_writer_inputs`; the renderer walks it verbatim. Orphan themes get
their own trailing section and are never fuzzy-matched back onto a plan theme.

Not hypothetical: replaying real work dirs recovered a genuine Semafor story
that had been dropped from two episodes under an invented theme name.

Three lessons:

- **Instrumentation can be decorative without being wrong.** `reached_prompt`
  was first written as `bool(text)` in the same dict literal that set
  `chars = len(text)` — tautologically equivalent, so the canary could never
  fire however badly assembly broke. Derive a check from a *different*
  structure than the one it is checking, or it proves nothing.
- **Fix the limit case, not just the common one.** Suppressing empty per-theme
  headers removed the invitation to fabricate a section; it took review to
  notice that if *every* directive fails to resolve, the model is still asked
  for a briefing and will write one from memory, published unread.
- **Task order can be load-bearing.** Reviewing the plan before coding found an
  ordering that would break the manual-publish path between two commits with
  nothing to detect it — the suite passes, the import succeeds, and the mocks
  were not autospec'd.

### ~~Unify directive→article matching — `3yb` + `4pw` + `mr1`~~ — DONE (PR #12)

Shipped 2026-08-16. One leaf resolver (`pipeline/article_resolver.py`) now owns the
cascade — **exact headline, then _unique_ slug, nothing else** — and delivery, the
Exa trigger, and show notes all call it.

The word-overlap tier is deleted rather than thresholded, because measurement showed
a threshold cannot work: a *wrong* article scored ≥1 query word in **50 of 54** real
directives and **tied the correct one at a perfect 4/4** in one case. Replay against
real work dirs found the tier actively binding directives to unrelated articles —
17.6 KB of a Mets ETF story under "Trade tensions mount ahead of Trump-Xi summit".
All four instances were `include_in_episode=False`, so nothing shipped wrong; the
81 directives that *do* ship resolved identically before and after.

Four lessons:

- **A statistical fix can be unavailable, not just imperfect.** The bead proposed a
  minimum-score threshold. The distributions overlap at the top — a tie at a perfect
  score — so no threshold exists. Measuring the *separation* between right and wrong
  candidates, not just the hit rate, is what revealed that.
- **Ask when a fallback runs, not just how often it's wrong.** This one only ran when
  the earlier tiers missed, and a main cause of that is the correct article being
  absent entirely — where "best match above zero" is *guaranteed* wrong. A tier's
  worst regime is the one it exists for.
- **Fix a defect everywhere it lives, in the same change.** Delivery was fixed and the
  identical coin-flip was left in show notes — then documented as "agrees by
  construction." Review caught it. Grep for the shape, not the symptom.
- **A mutation that doesn't bite is a finding.** Removing the anchoring regex broke no
  test, because the narrowed glob already covered the tested case. The regex was still
  load-bearing for a different filename shape, which now has its own test.

### 1. FP routing gets empty URLs — `5m3` (P3) — **premise corrected**

PR2 of the join work, deliberately split out: it changes **another podcast's input**.

The bead says exact-headline matching loses ~8% of links to a whitespace quirk.
Measurement says otherwise: **all 12** routed links on disk have an empty `url`
(100%), because **16/16** FP-flagged directives are Semafor while the join searches
the Levine-only `articles` list. The bead's one-line slug fix would not have fixed it.
Resolve through `headline_index` (which spans all sources) and read the `URL:` header —
`article_resolver.extract_url` already ships, unused, for this.

Two hazards to handle in that PR: routed links are labelled `levine-routed`
(`fp_collector.py:232,342`), already wrong for 16/16; and filling in URLs enables a
dedup that empty URLs silently disabled, so a `Routing: both` Semafor article could
arrive twice in one run.

### 2. Exa hardening batch — `avf` + `d8w` + `j7f` + `gz4` (P3)

**Why grouped:** all four touch `exa_client.py` or its immediate callers and
share test surface. None is urgent alone; four separate PRs would be waste.
Origin exclusion by registrable domain; non-daemon timeout thread; unguarded
`read_text` + empty-slug glob; FP's ungated Exa reader.

### 3. Writer robustness — `ne0` + `qd5` + `98p` (P2)

Malformed closing tags in `_extract_script`; FP Digest hallucinating a "thin
news day" briefing from an empty plan. Both are "the LLM did something we didn't
expect and we published it anyway" — same family, sensible together.

`98p` (FP Digest has the identical theme-drop bug plus its own hand-rolled
dry-run assembler) belongs here now: the Rundown fix in PR #11 is the template,
and `qd5` is the FP twin of the zero-sections guard that PR added — do them in
one pass over `fp_writer.py` rather than three.

### 4. Set funnel thresholds — `my-podcasts-3qs` (P3) — **calendar-gated**

Do **not** start before ~2026-08-31. Needs ~2 weeks of real `run-stats.jsonl`
weekday history. Guessing thresholds inside a project premised on not yet having
numbers is self-refuting; `include_in_episode` measured 4-5 on ten consecutive
runs, so any obvious rule fires on normal days. Threshold *ratios* against the
day's stub count, never absolute counts — see the scale note below.

### 5. Polish — `2v3`, `cqc`, `cgn` (P3/P4)

Surface open-access URLs in show notes; reconcile the funnel design doc with the rendered report; triage 111
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
- **Measure the separation, not just the rate.** A matcher's hit rate says nothing
  about whether a threshold can save it. The word-overlap tier was killed by
  scoring the best *wrong* candidate per directive and finding it tied the right
  one at a perfect score — a question the hit rate could never have answered.
- **A mutation that fails to bite is a finding, not a formality.** Twice now the
  honest report of "I broke it and no test failed" located a real gap: once a
  missing test, once a fact about the code nobody knew.
- **Verify a bead ID before writing it into anything durable.** Two IDs in this
  file (and in a merged PR description) were invented from memory rather than
  read back from `bd`, and pointed at nothing. `bd create` prints the id —
  capture it. A confident wrong pointer is worse than no pointer, because it
  survives compaction looking authoritative.
- **A check derived from the thing it checks proves nothing.** `reached_prompt`
  as `bool(text)` sat next to `chars = len(text)` and was therefore always
  consistent with it. Instrumentation must be derived from a different structure
  than the one it validates.
- **Measure the remediation, not just the fix.** "Keep the newest row" was
  inherited guidance from a prior cleanup. Re-checking all 16 keys against the
  live R2 objects confirmed it — but the prior measurement had a known 1-in-147
  mismatch, so confirming took one command and assuming would have silently
  pointed a feed entry at the wrong artifact.
