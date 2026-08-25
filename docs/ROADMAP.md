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

**Done (2026-08-18), off-spine but adjacent:**

| Bead | PR | What it bought |
|---|---|---|
| `ne0` | #15 | One `report_engine.py`; silver joins the transcript path |
| `xlf` | #16 | Daily writers migrated; consolidation actually finished |
| `tgb` | #18 | FP RSS teasers (361 chars) → full articles; 5.4x measured |

PR #15 also backfilled the four historical Silver Bulletin transcripts as
reports (30-75 min literal reads → 10-15 min briefings) and deleted the
literal-read rows. It opened `xlf` (spine piece 1), `9r5`, and `52k`.

**Verified in production 2026-08-17** (Monday 04:30 ET, the first weekday run on
all five pieces): exactly **one** episode row per feed, **0** duplicated `r2_key`s
overall, timer units completing in **3-4 seconds** (enqueue-only holds), funnel
`0 dropped` with no miss reasons and 0 shadow hits, 6/6 selected stories delivered
with text, and the first line ever written to `/persist/my-podcasts/run-stats.jsonl`.
No traceback in the journal. Both feeds serve the new episode (116/117 items,
matching the DB).

**Open-access substitution (`85c`) was finally measured on 2026-08-25**, the
first Levine-bearing run since the funnel existed. It had gone unmeasured for
eight days for two compounding reasons: the early runs had zero Levine stubs,
and then Levine went on vacation 08-17 through 08-21, so `run-stats.jsonl`
contains exactly **one** Levine-bearing line in its whole history.

```
FETCH  levine 11: live 1, http_error 10
EXA    8 flagged -> 8 hit
WRITE  12 selected -> 12 with text (4 cache, 8 http_error+exa), 0 dropped, 8 +open-access
```

**8 flagged, 8 hit, 8 appended, 0 dropped, and all 12 selected stories reached
the writer with text.** On a day when 10 of 11 Levine fetches failed, the
episode still went out at 2979 words. That is the feature doing exactly the job
it was built for, and it is the first day that could possibly have shown it.

Two cautions on reading this. It is **one** day — the roadmap's own scale note
(~1.2 stubs per episode across 8 runs) says a single run cannot establish a rate,
and 8 stubs is far above that average. And the failing tier is `http_error`, not
`paywalled`: all 10 were `bloomberg.com` URLs, so the `paywalled:` domain
histogram that was built to name publishers worth routing around **stays empty
on exactly the publisher that matters most**. Worth a look when trending.

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

### 1. Writer robustness — ~~`xlf`~~ DONE (PR #16) + `qd5` + `98p` (P2)

**Promoted to the top after `ne0` fired in production on 2026-08-18**, shipping a
2636-byte FP Digest episode (a normal one is ~3 MB). The model emitted a
placeholder `<script>...</script>` before the real script; the non-greedy regex
matched it, and the empty-check passed `...` through to TTS. Fixed in PR #14 for
the two daily writers, plus a plausibility floor at the TTS boundary.

**PR #15 then built the real fix in one place.** `pipeline/report_engine.py` now
owns the extraction and the publish-boundary refusals: longest `<script>` block,
unclosed-final-block recovery, case-insensitive tags, and a leaked-markup guard.
`transcript_report.py` (chinatalk, yglesias, silver) and `report_writer.py`
delegate to it. `ne0` is closed.

**`xlf` closed the consolidation in PR #16**, and the "exactly one
implementation" claim is now true and grep-verified rather than asserted.
`report_engine` was split into `fetch_report_text` (session lifecycle) and
`parse_report` (extraction + every refusal), with `run_report_prompt` as their
composition; the daily writers compose the halves themselves so they can persist
raw output between the two.

Two things that PR proved, both of which had been *stated wrongly* beforehand:

- **Migration alone never fixed the no-tag fallback.** The engine's fallback was
  always "return the text," merely tidier — measured 6522 chars in, 6521 out. Raw
  model reasoning reached TTS on every path, and `min_chars` cannot catch it
  because raw output is long. `require_tags` is what closed it, and that was new
  work, not migration.
- **`<covered>` was never in the leaked-markup guard.** Only the daily writers
  emit that block, so the engine had never had to handle it.

Adversarial review then found two live silent-publish paths in the fix itself,
both fixed in the same PR: `require_tags` was defeated by a bare *mention* of the
tag in the model's reasoning (3382 chars of reasoning published), and raw-output
persistence was non-atomic, so a truncation mid-`<script>` re-parsed into a
clean-looking **half script** that passed every refusal (3072 chars, mid-sentence).

Remaining in this piece: `qd5`, plus the three beads that PR opened —
`p4c` (mangled tag mid-tail narrates `</scrip>` and trailing chatter — degraded,
not fabricated), `r8a` (`parse_covered` never got the longest-block and
case-insensitive hardening its siblings have), and the still-open `9r5`.

`qd5` is FP Digest hallucinating a "thin news day" briefing from an empty plan —
same family ("the LLM did something we didn't expect and we published it
anyway"), sensible in the same pass.

`98p` (FP Digest has the identical theme-drop bug plus its own hand-rolled
dry-run assembler) belongs here now: the Rundown fix in PR #11 is the template,
and `qd5` is the FP twin of the zero-sections guard that PR added — do them in
one pass over `fp_writer.py` rather than three.

### 2. The FP Digest spine — `4uz` → `98p`+`qd5` → `wfh`+`5m3`+`8m8`

FP Digest has accumulated five open beads across three files. They are ordered
here as one spine because **the order is the whole design** — get it wrong and
the work still lands but proves nothing.

Each piece runs the loop at the top of this file: compact → optional
`oracle-fable` on a real design fork → measure → plan → **`adversarial-reviewer-fable`
on the plan** → SDD → **`adversarial-reviewer-fable` on the diff** → PR → update
this file and the beads. "If applicable" is a real qualifier for SDD and the PR
(a one-commit doc change needs neither); it is **not** a qualifier for the two
reviews or for measuring first.

**FP-A. Ship the report — ~~`4uz`~~ DONE (PR #20).**

FP publishes daily and emits nothing; The Rundown posts a funnel to Telegram and
appends to `run-stats.jsonl`. Two independently-safe commits: `RunStats.feed`
(default `"the-rundown"`) with a feed-aware header, then the FP branch calling
`_report_run_stats(..., feed="fp-digest")` at ~`consumer.py:757`.

**Why first, and this is the load-bearing decision:** *instrument before you
intervene.* Ship the report after FP-B/FP-C and the funnel's first FP rows
describe an already-fixed pipeline — the pre-fix baseline is gone, and the
before/after replay that made PR #11's recovered-story claim *provable* becomes
impossible for FP. Adversarial review refused a proposal to bundle all of this
into one pass for exactly this reason, after I had proposed it.

It is not a consolation prize: **both** of FP's observed incidents are visible in
`PLAN` + `OUT` alone — the 76-word writer refusal in `fp-digest-5d2519dc` (a dry
run, not shipped) and the 3-byte `...` script of 2026-08-18.

**Shipped, and the thin report turned out to separate *three* failure classes,
not two.** Rendered against every real FP work dir on disk:

```
PLAN   6 directives / OUT 2511 words   <- healthy
PLAN   0 directives / OUT   76 words   <- collector produced nothing
PLAN   7 directives / OUT   76 words   <- collector fine, assembly lost EVERY story
PLAN   6 directives / OUT    1 words   <- the 2026-08-18 placeholder that published
```

Rows 2 and 3 look identical at the `OUT` stage and have completely different
causes; `PLAN` alone tells them apart. Row 3 (`fp-digest-5d2519dc`) is standing
evidence for `98p`/`wfh` — the plan held 7 directives and the writer was handed
nothing. Healthy runs sit at 1602-2528 words, so the separation from the 60-111
word refusals is 14x and needs no tuning.

Measured while building it, and worth keeping: `directives_fp_routed == 0` and
`directives_episode == directives_total` on **all 13** real FP work dirs, so the
episode/fp-routed split is degenerate for FP and its `PLAN` line drops it.

**FP-B. Writer robustness — `98p` + `qd5` (P2).** Already piece 1 above; it adds
`writer_inputs.json` and therefore the report's `WRITE` line.

**FP-C. One pass over `fp_collector.py` — `wfh` + `5m3` + `8m8` (P2/P3).** The
joins, the routed-link URLs, and the collector's acquisition counters. Adds
`miss_reason`/`shadow` (only if `wfh` takes the index route — the bead leaves
filesystem-only open) and the `IN`/`DEDUP`/`FETCH`/`EXA` lines.

**`8m8` exists because the tidy story was false.** "The funnel's missing inputs
are exactly what `wfh` + `98p` produce" was checked itemwise and is half wrong:
fetch tiers and the collection counters come from *neither* bead. FP's sentinel
is only `{job_id, completed_at, lookback_days, directives}`, and FP's sources are
homepage/rss/routed/semafor — so today's `collect_run_stats` renders `IN 0 =
levine 0, semafor 0, zvi 0` against an FP work dir. **Omit those lines for `fp`
rather than rendering zeros** until `8m8` lands; a permanent `FETCH levine 0` is
the decorative-instrumentation trap this file already warns about twice.

PR #20 implemented that omission as a per-feed stage allowlist, `_FEED_STAGES` in
`run_stats.py`. **When `8m8` lands, adding the stage names there is the whole
render change.** Two things its author should know, both found by review:

- **The jsonl rows still carry the zeros the report omits.** `collect_run_stats`
  gives an FP dir `candidates={levine 0, semafor 0, zvi 0}` and empty tiers, so
  the durable record asserts FP has a Levine source — the same falsehood, moved
  to disk. Harmless only while readers filter on `feed`; `3qs` must.
- **FP does run Exa** (`fp_collector.py:530-543` writes `enrichment/exa/*.md`).
  What it doesn't write is *outcomes* in the sentinel, so today's Exa activity —
  including its silent `no_key` and error paths — is entirely unmeasured.

Four facts this spine dies without:

- **`consumer.py` imports `run_stats` lazily per call** (`consumer.py:79,99,106`).
  After a merge without a restart, *old in-memory consumer bytecode calls new
  on-disk `run_stats`.* Every new parameter must default; never reorder
  positionals; `RunStats.feed` needs a default so historical rows still parse.
- **The `feed` field must land before or with the first FP append**, never after.
  `run-stats.jsonl` had 2 lines and zero `feed` keys when measured, so "missing
  ⇒ the-rundown" is safe *today* and gets costlier daily. FP rows do not touch
  `3qs`'s clock, which is gated on Levine-present Tue–Fri **Rundown** rows.
- **`/tmp` is reaped per file at 10 days, so old work dirs lie.** Measuring FP's
  join over `/tmp/fp-digest-*` gave 11/67 misses (16%) — but 7 came from one dir
  whose articles had been reaped while its `plan.json` survived. Filter by mtime
  first: the real rate is **4/60, ~7%**, not the 18% two beads quote.
- **Those 4 real misses are editor *reformulations*** — numeral vs spelled-out, a
  theme name glued onto the headline, two sentence-style paraphrases. Exact+slug
  recovers **none** of them. So `wfh`'s FP deliverable is telemetry and
  refusal-on-ambiguity, **not** recovered stories; recovery is `mr1`'s
  normalization question. Do not sell `wfh` as a recovery fix.

`tgb` shipped ahead of this spine in PR #18 (RSS full-text fetch) and is closed.

### 2a. FP routing gets empty URLs — `5m3` (P3) — **premise corrected**

Part of FP-C above. PR2 of the join work, deliberately split out: it changes
**another podcast's input**.

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

**Consider bundling `my-podcasts-wfh`** (filed 2026-08-16): FP's own delivery and Exa
joins disagree with each other, the same disease `3yb` just fixed on the Rundown side.
FP writes no `headline_index.json`, so it needs either an index at collection time or
an explicit decision to stay filesystem-only with anchored, uniqueness-checked globs.
`5m3`, `wfh`, and `98p` all touch `fp_collector.py` — three separate passes over those
files would be waste.

**`my-podcasts-tgb` took one pass over `fp_collector.py` without bundling `wfh`,
and that was deliberate.** `tgb` (RSS full-text fetch for truncated antiwar.com
teasers, PR #18) runs at collection time, before the editor call, so it touches
no directive→article join at all. Bundling `wfh` would have attached the riskier
half of a P2 bug to an otherwise contained collection-phase change.

**That precedent has since been misapplied once, in both directions — watch for
it.** Attaching a *swallow-everything reporter* (`_report_run_stats` cannot fail
a job by construction) to two *job-path* changes that burn retry budget is the
same mistake inverted: it merges their blast radii and buys nothing. The test is
not "same file," it is "same failure mode." `5m3`/`wfh`/`8m8` share both, which
is why FP-C bundles them and FP-A stands alone.

### 3. Exa hardening batch — `avf` + `d8w` + `j7f` + `gz4` (P3)

**Why grouped:** all four touch `exa_client.py` or its immediate callers and
share test surface. None is urgent alone; four separate PRs would be waste.
Origin exclusion by registrable domain; non-daemon timeout thread; unguarded
`read_text` + empty-slug glob; FP's ungated Exa reader.

### 4. Set funnel thresholds — `my-podcasts-3qs` (P3) — **calendar-gated**

Do **not** start before ~2026-08-31, and note the clock is **paused** while
Levine is away (week of 08-17) — his links are the only source producing
paywalled stubs, so a Levine-free week yields zero evidence about the feature
being thresholded. What `3qs` needs is ~2 weeks of **Tue-Fri runs with Levine
present**, not 2 weeks of calendar. Needs ~2 weeks of real `run-stats.jsonl`
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

- **A restart can fail for reasons unrelated to your change, and the service's
  ExecStartPre reaches the network.** On 2026-08-17 a routine deploy restart put
  the consumer in a 40-minute restart loop: the Nix `ExecStartPre` runs
  `nltk.download('punkt_tab')` on *every* start, `raw.githubusercontent.com` was
  unreachable (host IPv6 egress down, plus GitHub rate limiting that the restart
  loop itself sustained), and start-pre blocked past the 90s timeout — for data
  already on local disk. Mitigated by a drop-in at
  `/run/systemd/system/my-podcasts-consumer.service.d/`, **which is cleared on
  reboot** (`/etc` is read-only on NixOS). See `my-podcasts-2h7`; the permanent
  fix is in the workstation Nix definition. If a restart hangs, check
  `journalctl` for start-pre before suspecting your own commit.
- **Deploy is a restart.** `my-podcasts-consumer` runs `uv run python -m
  pipeline consume` against the **live working tree** as a long-lived loop with
  `Restart=on-failure`. Merging to `main` does **not** deploy. Every
  intermediate commit must be independently safe; an import error is a
  crash-loop. Check `pending_the_rundown`/`pending_fp_digest` in
  `/persist/my-podcasts/state.sqlite3` before restarting.
- **Levine is on vacation the week of 2026-08-17** (operator-confirmed, back the
  week of 08-24). Every Rundown funnel that week reads `levine 0` / `EXA 0
  flagged` — correct arithmetic on an empty denominator. A wake fires 08-25 to
  confirm content actually resumes, because **a vacation and a broken delivery
  path look identical from inside the pipeline**: both show `levine 0` with no
  errors, and only the mailbox or the expected return date distinguishes them.
- **Mondays have no Levine content, structurally.** Money Stuff publishes
  **Mon-Thu only** (verified across three consecutive weeks of cache files). A
  Monday's adaptive lookback is 4 days — Fri/Sat/Sun/Mon — which contains no
  edition, and Monday's own arrives after 04:30. Thursday's was consumed by
  Friday's episode, so nothing is lost, but a Monday funnel legitimately reads
  `levine 0` / `EXA 0 flagged`. Never diagnose a broken trigger from a Monday.
  It also means the two weeks of history `3qs` needs is two weeks of **Tue-Fri**.
- **`episodes.pub_date` is RFC822, not ISO** (`Mon, 17 Aug 2026 08:37:39 +0000`).
  `WHERE pub_date LIKE '2026-08-17%'` matches nothing and silently reports zero
  episodes for a day that shipped fine — this exact query, in a wake payload,
  briefly looked like a total publish failure. Count with
  `date(created_at)='YYYY-MM-DD'`. A verification query is an instrument; check it
  returns the right shape on a known-good day before trusting a zero from it.
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
- **Publish-boundary refusals are deliberate; do not add a fallback.** When
  `report_engine.run_report_prompt` rejects a script (empty, below `min_chars`,
  or carrying leaked `<script>`/`<summary>` markup), the correct outcome is **no
  episode**, not a degraded one. The transcript path re-raises so the email is
  redelivered; the daily path fails the job so backoff and the retry-exhaustion
  alert fire. AGENTS.md previously claimed the yglesias path "fails safe: any
  exception falls back to the standard reading" — that was never true of the
  code and must not be restored. The honest cost is `9r5`: the email path has no
  failure counter, so a deterministically-failing body redelivers forever.
- **A guard must demand positive evidence, not a substring.** `require_tags`
  first shipped as "does the reply contain `<script>`" and adversarial review
  broke it the same hour: a reply that merely *mentions* the tag while reasoning
  satisfies it, then falls into the unclosed-tail branch and publishes the
  reasoning. The same shape recurs across this repo — the fuzzy article matcher
  accepted "any word overlap above zero," and `extract_script` once accepted "any
  trailing `<script>`." In each case the fix was to require evidence the thing
  was *actually* what it claimed, and to treat ambiguity as a refusal.
- **Files that a retry re-reads must be written atomically.** `os.replace`, not
  `write_text`. A truncated `raw_writer_output.txt` re-parses into a clean-looking
  half script that clears every refusal, whereas an empty one fails loudly and
  self-heals. Deploying is a `systemctl restart` of the process doing the write,
  so this is a routine trigger, not an exotic one.
- **`min_chars` is per-caller and was mis-inherited once already.** 500 came
  from `rundown_writer`, whose failure path is *bounded* (backoff → errored →
  alert). The transcript path's is *unbounded* redelivery, and its prompts ask
  for 800-1500 words (~4,400-9,000 chars), so 500 was 11% of the prompt's own
  floor — raised to 2000. Re-derive the number from the caller's prompt and
  failure mode; never copy it across.
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
- **An "empty" check is not a "valid" check.** The writer guard rejected an
  empty script, so a 3-character `...` sailed through it, got TTS'd, and
  shipped. Guards on generated content need a plausibility floor, not a
  null test — and the floor belongs on the publish path, not in the generator.
- **Unrealistic test fixtures hide the bug they should catch.** Three processor
  tests published 16-38 character "episodes". Padding them to realistic lengths
  was the fix, not a concession: the old fixtures asserted that a 16-char script
  was publishable, which is exactly the belief that let this ship.
- **Verify a bead ID before writing it into anything durable.** Two IDs in this
  file (and in a merged PR description) were invented from memory rather than
  read back from `bd`, and pointed at nothing. `bd create` prints the id —
  capture it. A confident wrong pointer is worse than no pointer, because it
  survives compaction looking authoritative.
- **A check derived from the thing it checks proves nothing.** `reached_prompt`
  as `bool(text)` sat next to `chars = len(text)` and was therefore always
  consistent with it. Instrumentation must be derived from a different structure
  than the one it validates.
- **Instrument before you intervene.** The reporting for a broken stage must ship
  *before* the fix to that stage, or its first rows describe an already-fixed
  pipeline and there is no baseline left to prove the fix by. I proposed the
  opposite for FP — one tidy pass fixing the joins and lighting up the funnel
  together — and adversarial review killed it on this ground alone. The tell that
  it was rationalization: it also happened to be the more interesting work.
- **A bead is a claim to verify, not a citation.** `tgb`'s own headline numbers
  ("630-char teasers", "7x") were the whole file including its metadata header;
  the body measured 361 chars and the real gain 5.4x. `wfh`'s "18% unresolved"
  was 7% once reaped work dirs were filtered out. Both were written by the same
  hand that later trusted them.
- **Measure the remediation, not just the fix.** "Keep the newest row" was
  inherited guidance from a prior cleanup. Re-checking all 16 keys against the
  live R2 objects confirmed it — but the prior measurement had a known 1-in-147
  mismatch, so confirming took one command and assuming would have silently
  pointed a feed entry at the wrong artifact.
- **"Plans are wrong about their own premises" extends to bead descriptions.**
  `my-podcasts-tgb`'s own headline numbers — "630-char teasers," "7x" — were
  wrong: 630 was the whole cache file including its ~270-char metadata header,
  not the body (which measured 361 chars median), and the real end-to-end gain
  measured 5.4x, not 7x. A bead is a claim to verify, not a citation.
