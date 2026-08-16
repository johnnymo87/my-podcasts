# Writer Section Assembly Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make it structurally impossible for a selected story's text to be silently
dropped between assembly and the writer prompt, and make `writer_inputs.json` tell the
truth about what actually reached the model.

**Architecture:** Today two structures must agree — `plan.themes` (a list) and
`articles_by_theme` (a dict keyed by `directive.theme`). `build_rundown_prompt` iterates
the first and silently discards non-matching keys of the second. We replace both with a
single ordered list of `(theme, articles)` sections built once in
`_assemble_writer_inputs`; the prompt builder renders it verbatim and cannot re-derive or
drop anything. This is the repo's own "prefer removing a participant to coordinating
participants" lesson applied to the writer.

**Tech Stack:** Python 3.12, `uv`, pytest, ruff. No new dependencies.

---

## Context you need before starting

Read `docs/ROADMAP.md` first. Then these, which this plan assumes:

- **Deploy is `sudo systemctl restart my-podcasts-consumer`.** Merging does not deploy.
  The consumer runs against the live working tree. **Every intermediate commit must be
  independently safe** — `pipeline/__main__.py` is *lazily* imported by the running
  consumer (`consumer.py:323`), so a broken module fails mid-job with a green
  `systemctl status`, not at startup.
- **Article markdown goes VERBATIM into the writer prompt.** Section headers you emit are
  prompt content the model reads. Do not put metadata in article text.
- Tests must be hermetic: `tmp_path` only, no network, no real `/persist`. A
  `pipeline/conftest.py` autouse fixture severs `pipeline.alerts.requests.post`.
- Baseline before you start: **543 tests passing**, ruff clean. Confirm this yourself
  before task 1 — do not take the number on faith.
- **NEVER** run `git stash`, `git reset`, `git checkout --`, `git restore`, or
  `git clean`. This worktree shares a checkout with other sessions. Read-only git
  inspection only (`git diff`, `git log`, `git show`).

### The two defects being fixed

**a3x — silent drop.** The editor sometimes emits a `directive.theme` not present in
`plan.themes` (an invented near-miss name). Measured across 159 historical plans: 81
selected directives, **2 orphaned (~2.5%)**, both the string `'AI Safety & Regulation'`.
That directive's text never reaches the model, yet `writer_inputs.json` records it with a
`source_path` and `chars > 0`, so the funnel reports it delivered.

**Empty sections.** The loop emits `## {theme}` for every plan theme including ones with
zero articles — a bare header under the literal prompt heading "STORIES BY THEME", in a
system that publishes unread. Measured on the 8 historical plans with realistic theme
names: **2 of 8** render at least one empty header, and one of those is caused by the
orphan bug itself.

**Do NOT fuzzy-match an orphan theme back onto a plan theme.** A near-miss name means the
editor was unsure about grouping; silently reassigning the story to a theme the editor did
not choose trades a visible drop for an invisible miscategorization. Orphans keep their own
name and become their own trailing section.

### Explicitly out of scope (beads already filed)

- `my-podcasts-tj9` — FP Digest has the identical bug in `fp_writer.py:107-115` plus its
  own hand-rolled dry-run assembler. Port this shape there in a later pass.
- `my-podcasts-w6k` — `find_rundown_article_source` accepts `best_score > 0` in its
  word-overlap match, so one shared common word can bind a directive to the wrong article.
  Needs measurement first.

---

## Task ordering is load-bearing

Adversarial review found that the obvious ordering ships a broken production path. Two
rules came out of it, and both are non-negotiable:

1. **The dry-run path must stop hand-rolling assembly BEFORE `generate_rundown_script`'s
   signature changes.** `__main__.py:712-714` calls it with `themes=`/`articles_by_theme=`.
   Changing the signature first leaves that call raising `TypeError`, and **nothing
   catches it**: the full suite passes (the dry-run body is never executed by tests, only
   patched away at `test_daily_enqueue.py:354`), `import pipeline.__main__` exits 0 (it is
   a call-time mismatch, not import-time), and the consumer mocks are not `autospec`'d.
   The path that breaks is exactly the documented consumer-down recovery workflow — the
   one you need during an incident.
2. **`reached_prompt` must be added AFTER the prompt is actually built from sections.** If
   added while the shim still converts sections back to a dict, it records `True` for
   orphan text the old builder still drops — the field designed to expose a3x would mask
   a3x, and its justifying comment would be false the moment it is written.

---

### Task 1: Build ordered sections in assembly

**Files:**
- Modify: `pipeline/consumer.py` (`_assemble_writer_inputs`, 319-360; caller ~503)
- Test: `pipeline/test_consumer_open_access.py`

`_assemble_writer_inputs` returns `(sections, writer_inputs)` where `sections` is an
ordered `list[tuple[str, list[str]]]` of `(theme, article_texts)`. Ordering rules, all
decided in this one loop so nothing downstream can re-derive them:

1. Plan themes in `plan.themes` order, each with the articles that resolved for it.
2. **Themes with zero resolved articles are omitted entirely** — no bare headers.
3. Orphan themes (a `directive.theme` not in `plan.themes`) appended after, in first-seen
   order, under the editor's own name.

**Step 1: Write the failing tests**

Read the top of `test_consumer_open_access.py` and follow its existing fixture style.

```python
def test_assembly_keeps_plan_theme_order(tmp_path):
    """Sections follow plan.themes order, not directive arrival order."""
    # plan.themes == ["Alpha", "Beta"]; directives arrive Beta-first
    # Expect section names == ["Alpha", "Beta"]


def test_assembly_omits_themes_with_no_articles(tmp_path):
    """A plan theme whose directives all resolved to nothing is not a section."""
    # plan.themes == ["Alpha", "Empty"]; nothing resolves for "Empty"
    # Expect section names == ["Alpha"]


def test_assembly_appends_orphan_theme_as_its_own_section(tmp_path):
    """a3x: a directive theme absent from plan.themes must still reach the model."""
    # plan.themes == ["Alpha"]; a directive has theme "Invented Name" with real text
    # Expect section names == ["Alpha", "Invented Name"] and the text IS present


def test_assembly_does_not_reassign_orphan_to_a_similar_plan_theme(tmp_path):
    """Guards the design decision: no fuzzy matching, ever."""
    # plan.themes == ["AI & Machine Learning"]; directive theme "AI Safety & Regulation"
    # Expect a SEPARATE section; its text must NOT be folded into the plan theme
```

**Step 2: Run to verify they fail**

Run: `uv run pytest pipeline/test_consumer_open_access.py -x -q`
Expected: FAIL — `_assemble_writer_inputs` still returns a dict.

**Step 3: Implement**

Keep everything above the return (the `find_rundown_article_source` call and the Exa
append) exactly as it is. Replace the accumulation and return:

```python
    plan_theme_order = {theme: i for i, theme in enumerate(plan.themes)}
    by_theme: dict[str, list[str]] = {}
    orphan_order: list[str] = []

    for directive in plan.directives:
        ...  # unchanged resolution + exa append + writer_inputs.append
        if text:
            if directive.theme not in plan_theme_order and directive.theme not in by_theme:
                orphan_order.append(directive.theme)
            by_theme.setdefault(directive.theme, []).append(text)

    sections: list[tuple[str, list[str]]] = [
        (theme, by_theme[theme]) for theme in plan.themes if by_theme.get(theme)
    ]
    sections += [(theme, by_theme[theme]) for theme in orphan_order]
    return sections, writer_inputs
```

Update the caller at `consumer.py:503` to `dict(sections)` with a
`# TODO(task 3): pass sections directly` comment.

**Why the shim is safe:** `dict(sections)` fed to the unchanged `build_rundown_prompt`
(which iterates `plan.themes` and does `.get(theme, [])`) reproduces *exactly* today's
behavior — orphan still dropped, empty header still emitted. This commit changes no
production output. That is the point: a pure refactor, safe to have running.

**Expected blast radius:** `test_consumer_open_access.py:74,97,116,136` unpack the old
dict and will fail until updated. Update them in this commit.

**Step 4: Run tests.** Run: `uv run pytest pipeline/ -q`

**Step 5: Mutation-check the two rules**

Make each mutation, confirm a specific test fails, then restore the file byte-for-byte:
- `if by_theme.get(theme)` → `if True`: the omit-empty test must fail.
- Append orphans *before* plan themes: the order test must fail.

**Step 6: Commit**

```bash
git add pipeline/consumer.py pipeline/test_consumer_open_access.py
git commit -m "refactor(rundown): assemble ordered prompt sections instead of a theme dict"
```

---

### Task 2: Make the dry-run path use the real assembler

**Files:**
- Modify: `pipeline/__main__.py` (`_the_rundown_dry_run` 664-715; `_find_rundown_article_text` at 27)
- Modify: `pipeline/test_daily_enqueue.py` (stale comment at :222)
- Test: create `pipeline/test_dry_run_assembly.py`

**This must land BEFORE Task 3.** See the ordering note.

`_the_rundown_dry_run:697-703` hand-rolls `articles_by_theme` with
`_find_rundown_article_text`, so it has **no Exa append** and no orphan rescue. This is the
same "second drifted implementation" that caused `my-podcasts-78b`. It matters because the
documented manual-publish workflow is `--dry-run` then `publish-script`, so a
hand-published episode is generated from a materially different prompt than production.

**Step 1: Write the failing test**

```python
def test_dry_run_uses_the_same_assembler_as_the_consumer(tmp_path):
    """Dry-run must get the Exa append, not a private loop that predates it."""
    # Build a work dir with a stub article + enrichment/exa/<slug>.md for the same slug.
    # Assert the assembled text contains _OPEN_ACCESS_HEADING (see consumer.py).
```

**Step 2: Run to verify it fails.** Expected: FAIL — the hand-rolled loop never appends Exa text.

**Step 3: Implement.** Replace the hand-rolled loop with
`_assemble_writer_inputs(plan, work_dir)`, then `dict(sections)` for the still-unchanged
`generate_rundown_script` call — same `# TODO(task 3)` shim as Task 1.

Import `_assemble_writer_inputs` at **module scope**. Verified safe: `__main__.py:12-13`
already imports `pipeline.consumer` at module scope, and `consumer.py:323` imports
`__main__` only lazily inside a function, so this adds no new edge.

Then check whether `_find_rundown_article_text` still has any production caller. If not,
**delete it** and its now-dead tests — it is the drifted duplicate this task exists to
remove. Fix the stale comment at `test_daily_enqueue.py:222` claiming it is "still used by
--dry-run".

Consider also writing `writer_inputs.json` in the dry-run work dir, so
`run-stats --work-dir` works on dry-run dirs. Optional; say so in the commit if you skip it.

**Step 4: Run tests.** Run: `uv run pytest pipeline/ -q`

**Step 5: Verify importability at this commit**

```bash
uv run python -c "import pipeline.consumer" && uv run python -c "import pipeline.__main__"
```
Both must exit 0. This is the crash-loop guard.

**Step 6: Commit**

```bash
git add pipeline/__main__.py pipeline/test_dry_run_assembly.py pipeline/test_daily_enqueue.py
git commit -m "fix(rundown): generate dry-run scripts with the production assembler"
```

---

### Task 3: Render the prompt from sections

**Files:**
- Modify: `pipeline/rundown_writer.py` (`build_rundown_prompt` 74-123, `generate_rundown_script` 175-197)
- Modify: `pipeline/consumer.py` (call site ~503 — remove shim)
- Modify: `pipeline/__main__.py` (dry-run call site ~712 — remove shim)
- Test: `pipeline/test_rundown_writer.py`

`build_rundown_prompt(sections, date_str, context_scripts)` replaces the
`(themes, articles_by_theme, ...)` pair. `TODAY'S THEMES` is derived from the section
names, so announced themes and rendered sections are the same set by construction.

**Both call sites change in this commit.** Missing either is a call-time crash no test catches.

**Step 1: Write the failing tests**

Update the four existing `build_rundown_prompt` tests
(`test_rundown_writer.py:15,32,42,53`) and the ~10 `generate_rundown_script(...)` calls
(83, 111, 144, 178, 260, 353, 433, 471, 511, 537) to the new signature. Add:

```python
def test_prompt_themes_list_matches_sections_exactly():
    """No theme is announced that has no material beneath it."""
    prompt = build_rundown_prompt(
        sections=[("Tech", ["Article about tech"])], date_str="2026-03-10"
    )
    assert "- Tech" in prompt and "## Tech" in prompt


def test_prompt_renders_orphan_section_under_its_own_name():
    prompt = build_rundown_prompt(
        sections=[("Alpha", ["a"]), ("Invented Name", ["b"])], date_str="2026-03-10"
    )
    assert "## Invented Name" in prompt and "b" in prompt


def test_prompt_matches_legacy_rendering_when_there_is_nothing_to_fix():
    """Permanent guard: on a normal day the prompt is byte-identical to the old one.

    Carries the OLD rendering logic inline as a reference implementation. For input
    where every plan theme has >=1 article and there are no orphans -- i.e. the
    overwhelmingly common case -- the new builder must produce exactly what the old
    one did. This pins 'identical except the two intended changes' in CI, which the
    one-time manual diff in Task 6 cannot do.
    """
    themes = ["Alpha", "Beta"]
    articles = {"Alpha": ["a1", "a2"], "Beta": ["b1"]}
    legacy_sections = []
    for theme in themes:                      # old logic, verbatim
        lines = [f"## {theme}"]
        for j, art in enumerate(articles[theme], 1):
            lines.append(f"### Source {j}")
            lines.append(art)
        legacy_sections.append("\n".join(lines))
    legacy_block = "\n\n".join(legacy_sections)

    prompt = build_rundown_prompt(
        sections=[(t, articles[t]) for t in themes], date_str="2026-03-10"
    )
    assert legacy_block in prompt
```

**Step 2: Run to verify they fail.** Run: `uv run pytest pipeline/test_rundown_writer.py -x -q`

**Step 3: Implement**

```python
def build_rundown_prompt(
    sections: list[tuple[str, list[str]]],
    date_str: str,
    context_scripts: list[str] | None = None,
) -> str:
    ...
    themes_list = "\n".join(f"- {theme}" for theme, _ in sections)
    story_sections: list[str] = []
    for theme, articles in sections:
        section_lines = [f"## {theme}"]
        for j, article_text in enumerate(articles, 1):
            section_lines.append(f"### Source {j}")
            section_lines.append(article_text)
        story_sections.append("\n".join(section_lines))
    stories_block = "\n\n".join(story_sections)
```

Update `generate_rundown_script` to take and forward `sections`, then update **both** call
sites (`consumer.py`, `__main__.py`) to pass `sections` directly, deleting both shims.

**Step 4: Run tests.** Run: `uv run pytest pipeline/ -q`

**Step 5: Mutation-check.** Re-derive `themes_list` from a separate argument rather than
section names → the themes-match-sections test must fail. Restore.

**Step 6: Verify importability**, as in Task 2 Step 5.

**Step 7: Commit**

```bash
git add pipeline/rundown_writer.py pipeline/consumer.py pipeline/__main__.py pipeline/test_rundown_writer.py
git commit -m "feat(rundown): build the writer prompt from ordered sections"
```

---

### Task 4: Record `reached_prompt`

**Files:**
- Modify: `pipeline/consumer.py` (`_assemble_writer_inputs`)
- Test: `pipeline/test_consumer_open_access.py`

Now — and only now — the field is truthful, because the prompt is genuinely built from
`sections`.

**Step 1: Write the failing tests**

```python
def test_writer_inputs_marks_resolved_directive_as_reached_prompt(tmp_path): ...
def test_writer_inputs_marks_unresolved_directive_as_not_reached_prompt(tmp_path):
    """source_path is None -> reached_prompt is False."""
def test_orphan_directive_is_reached_prompt(tmp_path):
    """Regression guard for a3x: the orphan genuinely reaches the model now."""
```

**Step 2: Run to verify they fail.**

**Step 3: Implement.** Set `"reached_prompt": bool(text)` in the same loop that appends to
`by_theme`. Add a comment stating the invariant this refactor buys: *every non-empty
`text` lands in some section (plan theme or orphan), so `bool(text)` and "is in a section"
are the same predicate by construction.*

**Step 4: Run tests.** Run: `uv run pytest pipeline/ -q`

**Step 5: Mutation-check.** Hardcode `"reached_prompt": True` → the unresolved-directive
test must fail. Restore.

**Step 6: Commit**

```bash
git add pipeline/consumer.py pipeline/test_consumer_open_access.py
git commit -m "feat(rundown): record whether each directive's text reached the prompt"
```

---

### Task 5: Record why a directive resolved to nothing (bead 2sf)

**Files:**
- Modify: `pipeline/__main__.py` (`find_rundown_article_source`)
- Modify: `pipeline/consumer.py` (`_assemble_writer_inputs`)
- Modify: `pipeline/test_things_happen_collector.py` (**~9 call sites unpack the 2-tuple**)
- Test: `pipeline/test_consumer_open_access.py`

Widen `find_rundown_article_source` from `tuple[str, str | None]` to
`tuple[str, str | None, str | None]` — third element is a miss reason, `None` on a hit.

**Callers that MUST be updated in this same commit** (a missed one is a mid-job crash):
- `pipeline/consumer.py:332` (production)
- `pipeline/__main__.py:33` in `_find_rundown_article_text` — *unless Task 2 deleted it*
- `pipeline/test_things_happen_collector.py` lines 739, 765, 784, 803, 822, 847, 867, 880,
  905 — all do `text, path = find_rundown_article_source(...)`

**The taxonomy must match the code's actual shape.** The function has exactly **one** miss
return (the final `return "", None`); the lookups *cascade*, so "the index missed" and
"slug and Exa missed" are both true on every miss. Do not invent per-branch reasons.
Define the reason by the state of the index at the single miss point:

- `"no_index"` — `headline_index.json` did not exist
- `"index_unreadable"` — it existed but failed to parse (today this silently becomes `{}`)
- `"index_no_overlap"` — it parsed, but neither exact match nor word-overlap hit

All three imply the slug and Exa fallbacks also missed; that is what "miss" means. Record
that reasoning in a comment so the next reader does not re-litigate it.

**Step 1: Write the failing tests**

```python
def test_miss_reason_no_index(tmp_path): ...
def test_miss_reason_index_unreadable(tmp_path):
    """A corrupt headline_index.json is an operational problem worth distinguishing."""
def test_miss_reason_index_no_overlap(tmp_path): ...
def test_hit_records_no_miss_reason(tmp_path): ...
```

**Step 2: Run to verify they fail.**

**Step 3: Implement.** Track the reason as the function cascades; return it from the single
miss return. Set `"miss_reason": reason` on the `writer_inputs` entry.

**Step 4: Run tests.** Run: `uv run pytest pipeline/ -q`

**Step 5: Mutation-check.** Return a constant `"no_index"` from the miss return → the
`index_no_overlap` test must fail. Restore.

**Step 6: Commit**

```bash
git add pipeline/__main__.py pipeline/consumer.py pipeline/test_things_happen_collector.py pipeline/test_consumer_open_access.py
git commit -m "feat(rundown): record why a directive resolved to no article text"
```

---

### Task 6: Surface both new fields in the funnel

**Files:**
- Modify: `pipeline/run_stats.py` (`collect_run_stats` ~303-350, `render_report`)
- Test: `pipeline/test_run_stats.py`

Add a `dropped_before_prompt` count (entries with `reached_prompt` false but `chars > 0` —
which should now always be **zero**, making it the regression canary) and a compact
`miss_reason` histogram on the `WRITE` line.

**Backward compatibility is mandatory.** Historical `writer_inputs.json` lack both fields.
Follow the existing precedent at `run_stats.py:318` (`item.get("exa_appended") is True`)
and the comment at `run_stats.py:102-106`: use `is True` / `is False`, **never truthiness**,
so a missing key reads as "unknown" rather than False. Treating missing as False makes
every historical work dir report a false alarm.

The existing `dropped` counter (`run_stats.py:329-331`, `source_path is None`) is disjoint
from `dropped_before_prompt` — a miss always has `chars == 0` because the Exa append is
gated on `src is not None` (`consumer.py:343`). No double-count.

**Step 1: Write the failing tests**

```python
def test_dropped_before_prompt_counts_text_that_never_reached_the_model(): ...
def test_missing_reached_prompt_key_is_not_counted_as_dropped():
    """Historical writer_inputs.json predates the field; must not false-alarm."""
def test_render_report_shows_miss_reason_histogram(): ...
```

**Step 2-4:** fail, implement, pass. Run: `uv run pytest pipeline/test_run_stats.py -q`

**Step 5: Mutation-check.** Treat a missing `reached_prompt` as False → the
backward-compatibility test must fail. Restore.

**Step 6: Commit**

```bash
git add pipeline/run_stats.py pipeline/test_run_stats.py
git commit -m "feat(run-stats): surface prompt-delivery and miss reasons in the funnel"
```

---

### Task 7: Autospec the writer mocks

**Files:**
- Modify: `pipeline/test_consumer.py` (~330 and siblings)

The consumer tests patch `generate_rundown_script` without `autospec=True`, which is
exactly the blindness that let the Task 3 signature hazard through unnoticed. Add
`autospec=True` to those patches so a future signature change fails loudly in CI.

Run: `uv run pytest pipeline/ -q`. If autospec surfaces existing call-shape mismatches,
fix the calls — that is the point.

```bash
git add pipeline/test_consumer.py
git commit -m "test: autospec the writer mocks so signature drift fails loudly"
```

---

### Task 8: Pre/post prompt diff on real historical work dirs

**Files:** none committed — a verification gate, not code.

Task 3's equivalence test pins the common case in CI. This step checks the *real* data
for surprises the synthetic test cannot model.

**Step 1:** List candidate work dirs with realistic theme names:

```bash
uv run python -c "
import json,glob
for d in sorted(glob.glob('/tmp/the-rundown-*/')):
    try: p=json.load(open(d+'plan.json'))
    except Exception: continue
    th=p.get('themes') or []
    if th and any(len(t)>12 for t in th): print(d, th)
"
```

**Step 2:** For 3-4 of them, render the prompt on each branch and diff. Use a throwaway
worktree for `main` — **never** `git checkout` in a shared tree:

```bash
wt="$(mktemp -d)"; git worktree add --detach "$wt" main
# render on both, diff the STORIES BY THEME block
git worktree remove --force "$wt"
```

**Step 3: What to expect.** Most plans render **identically**. The only intended
differences are (a) a vanished empty `## Theme` header and (b) an orphan section now
present. **Any other difference is a bug — stop and investigate.**

**Step 4:** Record the observed diff summary in the PR description.

---

### Task 9: Docs

**Files:**
- Modify: `AGENTS.md` (The Rundown flow / key modules)
- Modify: `.opencode/skills/operating-things-happen-digest/SKILL.md`

Document: the prompt is built from ordered sections; themes with no material are not
announced; an orphan theme becomes its own trailing section and is never reassigned;
`writer_inputs.json` carries `reached_prompt` and `miss_reason`; `dropped_before_prompt`
should always be **0** and non-zero means the writer lost a story. Note that dry-run and
production now share one assembler, so a `--dry-run` + `publish-script` episode matches
what the consumer would have produced.

```bash
git add AGENTS.md .opencode/skills/operating-things-happen-digest/SKILL.md
git commit -m "docs: describe section-based writer assembly and the new funnel fields"
```

---

## Definition of done

- `uv run pytest -q` — all pass, count >= 543 + ~15 new.
- `uv run ruff check .` and `uv run ruff format --check .` clean.
- `uv run python -c "import pipeline.consumer"` and `import pipeline.__main__` both exit 0
  **at every commit** (the lazy-import crash-loop guard).
- Task 8's prompt diff shows only the two intended changes.
- PR opened, CI green, merged.
- **Deploy: check for in-flight jobs, then `sudo systemctl restart my-podcasts-consumer`.**
- `docs/ROADMAP.md` updated; `my-podcasts-a3x` and `my-podcasts-2sf` closed; `bd dolt push`.
