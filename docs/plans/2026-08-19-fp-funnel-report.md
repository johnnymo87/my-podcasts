# FP Digest Funnel Report Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Give FP Digest a per-episode funnel report on Telegram and a `run-stats.jsonl` row, rendering only the stages FP actually has data for.

**Architecture:** `RunStats` gains a `feed` field (default `"the-rundown"`). `render_report` selects its line set from that field, omitting the stages FP has no data source for rather than rendering structurally-meaningless zeros. Then the FP branch of `consume_forever` calls the existing `_report_run_stats` with `feed="fp-digest"`. Two commits, each independently safe.

**Tech Stack:** Python 3.12, pydantic v2 (`BaseModel`), pytest, `uv`.

---

## Context You Need Before Touching Anything

**Bead:** `my-podcasts-4uz`. **Roadmap:** `docs/ROADMAP.md` §2 "The FP Digest spine", FP-A. Read both.

### Why the report ships BEFORE the FP bug fixes

Adversarial review refused the alternative (bundle the report with the join/writer fixes). If the report lands after the fixes, its first FP rows describe an already-fixed pipeline and there is no pre-fix baseline to prove the fixes against. Do not "improve" this plan by folding in `98p`/`wfh`/`8m8`.

### Measured today, 2026-08-19 (do not re-derive, but do sanity-check)

Running today's `collect_run_stats` + `render_report` against a real FP work dir (`/tmp/fp-digest-228da585-...`) renders:

```
The Rundown 2026-08-18 (job 228da585) - script stage - lookback 2d

IN     0 = levine 0, semafor 0, zvi 0
DEDUP  0
FETCH  levine 0
PLAN   6 directives = 6 episode, 0 fp-routed
EXA    0 flagged
WRITE  0 selected -> 0 with text, 0 dropped
OUT    1 words, 5 themes, 4 headlines covered
```

Wrong feed name, wrong source keys (FP's sources are homepage/rss/routed/semafor, not levine/semafor/zvi), and five lines of meaningless zeros. `PLAN` and `OUT` are real and correct.

Across all 13 real FP work dirs on disk (those with a `script.txt`):

| class | dirs | directives | script words |
|---|---|---|---|
| healthy | 7 | 5-7 | 1602-2528 |
| writer refused, empty plan | 4 | 0 | 60-111 |
| writer refused, plan NOT empty | 1 (`5d2519dc`) | 7 | 76 |
| published placeholder | 1 (`228da585`) | 6 | **1** |

`directives_episode == directives_total` and `directives_fp_routed == 0` on **all 13** — the episode/fp-routed split is degenerate for FP, which is why the FP `PLAN` line drops it.

**The two-line report already separates three distinct failures**, which is the entire argument for shipping it thin:

- `PLAN 0` + `OUT ~76 words` → the collector produced nothing.
- `PLAN 7` + `OUT 76 words` → the collector worked and *assembly lost every story* (this is `5d2519dc`, and it is direct evidence for `98p`/`wfh`).
- `PLAN 6` + `OUT 1 words` → a placeholder reached publication (the 2026-08-18 incident).

### Hazards that will bite you

1. **Lazy import.** `consumer.py` imports `pipeline.run_stats` *inside* `_report_run_stats` (lines 79, 99, 106-107). After a merge without a restart, old in-memory consumer bytecode calls new on-disk `run_stats`. Therefore: **every new parameter must have a default, and never reorder positional parameters.** Commit 1 must be safe when called by a consumer that knows nothing about `feed`.
2. **`feed` must not land after the first FP append.** `/persist/my-podcasts/run-stats.jsonl` currently holds exactly 2 lines, both with no `feed` key. "Missing ⇒ the-rundown" is a correct reading *only* while FP has never appended. Commit 1 (the field) must precede or accompany commit 2 (the FP call site). Never ship 2 without 1.
3. **`_report_run_stats` swallows everything by design** (`consumer.py:56-114`) so it cannot fail a job. That is a feature, but it also means **a bug here is silent**. Tests are the only safety net; do not rely on seeing it fail in production.
4. **Do not touch the Rundown's rendered output.** Its report is in daily use. Commit 1 must be a strict no-op for `feed="the-rundown"`, and there is a test for exactly that.

### Ground rules

- Work in `/home/dev/projects/my-podcasts/.worktrees/fp-funnel` (branch `fp-funnel`). **Never** in the shared checkout `/home/dev/projects/my-podcasts`.
- **NEVER run** `git stash`, `git reset`, `git checkout --`, `git checkout <ref>`, `git restore`, `git switch`, `git clean`, `git rebase`, `git merge`, `git cherry-pick`, `git commit --amend`, `git push --force`. The checkout is shared with live sessions and this has already destroyed a peer's uncommitted data once.
- Tests hermetic: `tmp_path` only. No network, no real `/persist`, no real `/tmp`.
- Baseline on this worktree: **657 passed**.

---

## Task 1: `RunStats.feed` and a feed-aware report

**Files:**
- Modify: `pipeline/run_stats.py` (`RunStats` ~line 57, `render_report` ~line 441)
- Test: `pipeline/test_run_stats.py`

**Step 1: Write the failing tests**

```python
def test_run_stats_feed_defaults_to_the_rundown():
    """Historical jsonl rows carry no `feed` key and must still parse."""
    stats = RunStats(job_id="j", date_str="2026-08-19")
    assert stats.feed == "the-rundown"
    revived = RunStats.model_validate_json('{"job_id":"j","date_str":"2026-08-19"}')
    assert revived.feed == "the-rundown"


def test_render_report_is_unchanged_for_the_rundown():
    """Commit 1 must be a strict no-op for the feed already in production."""
    stats = _rundown_stats()  # reuse whatever fixture the existing tests use
    report = render_report(stats)
    assert report.startswith("The Rundown 2026-08-19 (job j) - script stage")
    for stage in ("IN ", "DEDUP ", "FETCH ", "PLAN ", "EXA ", "WRITE ", "OUT "):
        assert stage in report


def test_fp_report_header_names_the_feed():
    stats = RunStats(job_id="j", date_str="2026-08-19", feed="fp-digest")
    assert render_report(stats).startswith("FP Digest 2026-08-19 (job j) - script stage")


def test_fp_report_omits_stages_with_no_data_source():
    """Not zeros: FP writes no candidates/tiers/exa/writer_inputs at all.

    Rendering `IN 0 = levine 0, semafor 0, zvi 0` on an FP dir would assert
    FP has Levine and Zvi sources, which it does not. See my-podcasts-8m8.
    """
    stats = RunStats(job_id="j", date_str="2026-08-19", feed="fp-digest")
    report = render_report(stats)
    for absent in ("IN ", "ROUTE ", "DEDUP ", "FETCH ", "EXA ", "WRITE ", "paywalled"):
        assert absent not in report
    assert "levine" not in report
    assert "zvi" not in report


def test_fp_plan_line_drops_the_degenerate_routing_split():
    """directives_fp_routed is 0 on all 13 real FP work dirs -- FP *is* the fp feed."""
    stats = RunStats(
        job_id="j", date_str="2026-08-19", feed="fp-digest",
        directives_total=6, directives_episode=6, directives_fp_routed=0,
    )
    line = [x for x in render_report(stats).splitlines() if x.startswith("PLAN")][0]
    assert line == "PLAN   6 directives"


def test_fp_report_keeps_out_line_that_catches_the_real_incidents():
    """The 2026-08-18 placeholder published as `1 words`; 5d2519dc refused at 76."""
    stats = RunStats(
        job_id="j", date_str="2026-08-19", feed="fp-digest",
        directives_total=6, script_words=1, themes_count=5, covered_headlines=4,
    )
    report = render_report(stats)
    assert "OUT    1 words, 5 themes, 4 headlines covered" in report
    assert "PLAN   6 directives" in report


def test_unknown_feed_falls_back_to_the_full_rundown_shaped_report():
    """A typo'd feed must not silently render an empty report."""
    stats = RunStats(job_id="j", date_str="2026-08-19", feed="wat")
    report = render_report(stats)
    assert "PLAN " in report and "OUT " in report
```

**Step 2: Run to verify they fail**

Run: `uv run pytest pipeline/test_run_stats.py -k "feed or fp_" -v`
Expected: FAIL — `RunStats` has no field `feed`.

**Step 3: Implement**

Add to `RunStats`, immediately after `date_str`:

```python
    # Which podcast this run belongs to. Defaults to "the-rundown" because
    # every run-stats.jsonl row written before this field existed was a
    # Rundown row -- a missing key therefore reads correctly as history
    # rather than as "unknown". That reading is only safe while FP has
    # never appended, which is why this field ships in the same PR as (and
    # no later than) FP's first append. See my-podcasts-4uz.
    feed: str = "the-rundown"
```

Add near `_TOP_DOMAINS`:

```python
# Display names and rendered stage sets, keyed by feed. FP Digest's collector
# writes none of the acquisition artifacts (no per-source candidate counts, no
# tiers.json, no exa_outcomes, no writer_inputs.json), so those stages are
# OMITTED for fp-digest rather than rendered as zeros: a permanent
# "FETCH levine 0" would assert FP has a Levine source it has never had, and
# decorative zeros are how a 93% stub rate went unnoticed for months. When
# my-podcasts-8m8 lands and the FP collector writes those artifacts, add the
# stage names here -- that is the whole change.
_FEED_NAMES = {"the-rundown": "The Rundown", "fp-digest": "FP Digest"}
_FEED_STAGES = {
    "the-rundown": frozenset(
        {"in", "route", "dedup", "fetch", "plan", "exa", "write", "out", "paywalled"}
    ),
    "fp-digest": frozenset({"plan", "out"}),
}
_DEFAULT_STAGES = _FEED_STAGES["the-rundown"]
```

In `render_report`, replace the hardcoded header and guard each stage. An
unknown feed falls back to the Rundown-shaped full report (never to an empty
one) and titles itself with the raw feed value:

```python
    stages = _FEED_STAGES.get(stats.feed, _DEFAULT_STAGES)
    feed_name = _FEED_NAMES.get(stats.feed, stats.feed)
    date_token = f" {stats.date_str}" if stats.date_str else ""
    header = f"{feed_name}{date_token} (job {stats.job_id}) - script stage"
```

Wrap each existing stage block in `if "<stage>" in stages:`. For `plan`, branch
the degenerate FP split:

```python
    if "plan" in stages:
        if stats.feed == "fp-digest":
            lines.append(f"PLAN   {stats.directives_total} directives")
        else:
            lines.append(
                f"PLAN   {stats.directives_total} directives = "
                f"{stats.directives_episode} episode, "
                f"{stats.directives_fp_routed} fp-routed"
            )
```

**Step 3b: Fix the other call site — `run-stats` CLI**

Adversarial review found this; it is the live instance of "a future caller
forgets `feed`". `pipeline/__main__.py:1113-1127` (`run_stats_command`) strips
only the `the-rundown-` prefix and never passes `feed`. After Task 1 it would
still render a Rundown-shaped report for an FP work dir — and `--send` would
**post that mislabeled report to Telegram**. This is not hypothetical: running
it on an FP dir is exactly how this plan's measurements were produced.

Infer the feed from the work-dir name, and add a test:

```python
    name = work_dir.name
    if name.startswith("fp-digest-"):
        feed, job_id = "fp-digest", name[len("fp-digest-"):]
    else:
        feed, job_id = "the-rundown", name.replace("the-rundown-", "")
    stats = collect_run_stats(work_dir, job_id=job_id, date_str="", feed=feed)
```

**Step 3c: Golden-output regression test for The Rundown**

The `startswith` + substring assertions above (and the pre-existing tests) can
all still pass while an indent or spacing slip changes the rendered output —
and hand-wrapping seven render blocks in `if "<stage>" in stages:` is exactly
where that happens. Add one test that builds a **fully populated** Rundown
`RunStats` and asserts the **entire** report string equals an expected
multi-line literal. This is what makes the "byte-identical" claim in the
Definition of Done honest rather than aspirational.

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_run_stats.py -v` — expected: all pass, including every pre-existing Rundown test untouched.

**Step 5: Mutation-test (mandatory, report the result)**

Verify each mutation is caught; **if any survives, say so explicitly** — that is a finding, not a formality:

1. `feed: str = "fp-digest"` (wrong default) → `test_run_stats_feed_defaults_to_the_rundown` must fail.
2. `"fp-digest": frozenset({"plan","out","in"})` → `test_fp_report_omits_stages_with_no_data_source` must fail.
3. `_FEED_STAGES.get(stats.feed, frozenset())` (empty fallback) → `test_unknown_feed_falls_back...` must fail.
4. FP `PLAN` line rendered with the routing split → `test_fp_plan_line_drops_the_degenerate_routing_split` must fail.

**Step 6: Commit**

```bash
git add pipeline/run_stats.py pipeline/test_run_stats.py
git commit -m "feat(run-stats): make the funnel report feed-aware

RunStats gains a `feed` field defaulting to the-rundown, so the two
existing run-stats.jsonl rows (which have no such key) keep reading as
Rundown rows. render_report takes its title and its stage set from that
field.

FP Digest's collector writes none of the acquisition artifacts, so the
IN/ROUTE/DEDUP/FETCH/EXA/WRITE stages are omitted for fp-digest rather
than rendered as zeros -- 'FETCH levine 0' would assert a source FP has
never had. Those stages come back one at a time as my-podcasts-8m8 and
my-podcasts-98p land.

Strict no-op for the-rundown, covered by a regression test. No consumer
change, so a consumer running older bytecode is unaffected."
```

---

## Task 2: FP branch emits the report

**Files:**
- Modify: `pipeline/consumer.py` (`_report_run_stats` ~line 56, FP call site after ~line 757)
- Test: `pipeline/test_consumer.py`

**Step 1: Write the failing test**

Model it on the existing Rundown `_report_run_stats` test. It must assert the FP path calls the reporter with `feed="fp-digest"`, and — separately — that a raising reporter cannot fail the job.

**HERMETICITY — non-negotiable.** The real `_report_run_stats` appends to a
hardcoded `/persist/my-podcasts/run-stats.jsonl` (`consumer.py:101`) and calls
`send_alert`. Any test that drives `consume_forever` down the FP success path
**must patch or capture `pipeline.consumer._report_run_stats`**. An unpatched
drive-through writes the production jsonl from a test run.

```python
def test_fp_digest_emits_run_stats_with_its_own_feed(...):
    """FP's funnel row must be attributable, or run-stats.jsonl silently
    mixes two podcasts under one default feed name."""
    # patch pipeline.consumer._report_run_stats with a capturing fake
    # ... drive consume_forever through a successful FP job ...
    assert captured["feed"] == "fp-digest"


def test_fp_digest_reports_even_when_the_writer_covered_nothing(...):
    """Write this NOW, not conditionally.

    5 of the 13 real FP work dirs are writer refusals with no covered.json --
    the single highest-value class for this report. If the report call is ever
    placed inside `if writer_output.covered_headlines:`, the funnel vanishes on
    exactly the runs an operator needs it for, and every healthy-path test
    still passes.
    """
    # drive an FP job whose writer_output.covered_headlines is empty
    assert captured["feed"] == "fp-digest"


def test_fp_digest_run_stats_failure_cannot_fail_the_job(...):
    """_report_run_stats is total by design; prove it for the FP path too."""
    # patch pipeline.consumer._report_run_stats to raise
    # assert the job still completes and script.txt is still written
```

**Step 2: Run to verify it fails**

Run: `uv run pytest pipeline/test_consumer.py -k "fp_digest and run_stats" -v`
Expected: FAIL — no such call.

**Step 3: Implement**

Give `_report_run_stats` a **trailing, defaulted** parameter (never reorder — see hazard 1):

```python
def _report_run_stats(
    work_dir: Path,
    job_id: str,
    date_str: str,
    reused_collection: bool = False,
    feed: str = "the-rundown",
) -> None:
```

Pass it through to `collect_run_stats(..., feed=feed)`, which requires
`collect_run_stats` to accept and forward a defaulted `feed` onto the
`RunStats` it builds.

Then insert at `consumer.py` after the FP `covered.json` block (~line 757),
immediately before the `# Next loop will pick up the script and run TTS`
comment:

```python
                        _report_run_stats(
                            work_dir,
                            job_id=job["id"],
                            date_str=job["date_str"],
                            reused_collection=reused_collection,
                            feed="fp-digest",
                        )
```

**`reused_collection` is NOT in scope in the FP branch — verified 2026-08-19.**
The Rundown binds it at `consumer.py:514-516`; FP evaluates the identical
condition *inline* inside an `if` at `consumer.py:674` and never names it. So
bind it, mirroring the Rundown exactly:

```python
                        collection_sentinel = work_dir / "collection_done.json"
                        plan_path = work_dir / "plan.json"

                        reused_collection = (
                            collection_sentinel.exists() and plan_path.exists()
                        )
                        if reused_collection:
```

It must be a pure refactor — the branch taken is unchanged, so no existing test
should change behaviour.

**Do not skip the binding and just reference the name.** Adversarial review
corrected an earlier, wrong version of this warning, and the real hazard is
nastier than the one it replaced:

- `reused_collection` is assigned at **exactly one place today**, `consumer.py:514`,
  inside the **Rundown** branch of `consume_forever`. Both branches live in that
  one function, so the name is *function-scoped across feeds*.
- Referencing it from the FP branch without binding it therefore does **not**
  reliably raise. On a pass where a Rundown job ran first, the name is still
  bound, and FP silently reports **the Rundown job's** reuse flag. Wrong data,
  no error, and FP-only tests would never see it because in those the name really
  is unbound.
- Where it does raise, it raises in the **caller** while building the argument
  list — before `_report_run_stats` is entered — so its bare `except` never sees
  it. It propagates to the FP handler at `consumer.py:759`, calls
  `mark_fp_digest_failed`, and burns retry budget on every full-pipeline attempt
  until the job is marked errored.

Control flow was verified: the `else:` arm at 669 is straight-line from 671-674
to the insertion point, the `continue` at 715 exits before the report, and the
`script_file.exists()` branch (638-668) never reaches it. So binding at 674 is
sufficient — but bind it, and do not rely on the name being in scope.

**Step 4: Run tests**

Run: `uv run pytest -q` — expected: 657 + new tests, 0 failures.

**Step 5: Mutation-test (mandatory, report the result)**

1. Drop `feed="fp-digest"` from the call → `test_fp_digest_emits_run_stats_with_its_own_feed` must fail.
2. Move the call inside `if writer_output.covered_headlines:` → caught by `test_fp_digest_reports_even_when_the_writer_covered_nothing`.
3. Change `feed="fp-digest"` to `feed="the-rundown"` in the `run-stats` CLI's fp branch → the CLI test must fail.

**Step 6: Verify the lazy-import gate, then commit**

```bash
uv run python -c "import pipeline.consumer"
uv run python -c "import pipeline.__main__"
uv run ruff check . && uv run ruff format --check .
git add pipeline/consumer.py pipeline/test_consumer.py
git commit -m "feat(fp-digest): emit the funnel report after the script stage

FP Digest has published daily while emitting nothing. It now posts the
same funnel report The Rundown does and appends a run-stats.jsonl row
tagged feed=fp-digest.

The report is thin on purpose -- PLAN and OUT only -- because those are
the only stages FP's collector currently feeds. That is still enough to
separate FP's three observed failure modes: PLAN 0 with a short OUT
(collector produced nothing), PLAN 7 with a short OUT (assembly lost
every story), and PLAN 6 with OUT 1 words (the 2026-08-18 placeholder
that reached publication).

The new `feed` parameter is trailing and defaulted, so a consumer still
running pre-merge bytecode calls this safely."
```

---

## Task 3: Roadmap and beads

**Files:** `docs/ROADMAP.md` (§2, FP-A), beads `my-podcasts-4uz` / `my-podcasts-8m8`.

- Mark FP-A shipped with the PR number; keep the ordering rationale intact.
- Record the measured three-way failure separation — it is the evidence that shipping thin was right.
- Note on `8m8` that the FP stage set in `_FEED_STAGES` is the exact place to extend, so that bead's author does not go looking.
- Note on `8m8` and `3qs`, both from adversarial review:
  - **FP's jsonl rows still carry the structurally-false zeros the report omits.**
    `collect_run_stats` on an FP dir yields `candidates={levine:0, semafor:0, zvi:0}`,
    all-zero `exa_outcomes`, and empty `fetch_tiers`. The *durable record* therefore
    asserts FP has a Levine source with 0 candidates — the same falsehood as
    `FETCH levine 0`, just moved to disk. Harmless while `3qs` filters by feed, but
    a later reader must know these are structural defaults, not measurements.
  - **FP does run Exa.** `fp_collector.py:530-543` performs searches and writes
    `enrichment/exa/*.md`; what it does not write is *outcomes* in the sentinel. So
    real Exa activity is happening unmeasured today, including silent `no_key` and
    error paths. Omitting the EXA line is still right, but "FP has no Exa" is wrong.
- Note on a possible future bead (do **not** fold into this PR): a healthy-day FP
  report is identical every day, which trains an operator to skim it. A cheap later
  mitigation is an anomaly prefix when `script_words` falls below a threshold —
  which is `3qs`'s territory.
- `bd dolt push`.

---

## Definition of Done

- `uv run pytest -q` — 657 baseline + new tests, all passing.
- `uv run ruff check .` and `uv run ruff format --check .` clean (blocking; mypy is not).
- `uv run python -c "import pipeline.consumer"` and `import pipeline.__main__` both succeed.
- Every mutation above reported bitten — or explicitly reported as surviving.
- The Rundown's rendered report is byte-identical to before, proven by a test.
- PR opened. **Do not merge. Do not restart `my-podcasts-consumer`.**
