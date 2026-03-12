# Theme Rotation & Freshness Budget Design

**Date:** 2026-03-11
**Problem:** FP Digest (and The Rundown) episodes repeat the same storylines day after day, even when there are no material new developments. Listeners hear the same themes covered at length across consecutive episodes.
**Scope:** Both FP Digest and The Rundown feeds.

## Root Cause

The current anti-repetition mechanism is weak: the editor (Gemini Flash-Lite) receives the full text of the last 3 episode scripts and is told to "deprioritize" repeats. This is fuzzy — a lightweight model reading 6000+ words of prior prose and trying to match it against headline snippets is unreliable. The editor keeps selecting the same storylines, and the writer can only soften coverage, not substitute different stories.

Meanwhile, `articles_json` on the `episodes` table stores structured `[{title, url, theme}]` for every episode, but this data is never queried and fed back to the collector or editor.

## Solution: Coverage Ledger + Soft Rotation

### 1. Coverage Ledger (Data Layer)

Add a helper method to `StateStore`:

```python
def recent_coverage_summary(self, feed_slug: str, days: int = 3) -> list[dict]
```

Returns:
```python
[
  {
    "theme": "US-Iran War Escalation",
    "days_covered": 3,
    "last_lead": "2026-03-11",
    "episode_dates": ["2026-03-09", "2026-03-10", "2026-03-11"],
    "article_count": 8
  },
  ...
]
```

**Lead theme inference:** The first theme in `articles_json` is the lead (articles are ordered by theme order from `plan.json`, then priority within theme — the first theme in the plan's `themes` list is the lead).

**Fallback for episodes without `articles_json`:** Extract themes from prior scripts using an LLM call. This handles the transition period until structured data accumulates.

### 2. Headline Freshness Annotation (Collector Stage)

Before passing headlines to the editor, the collector annotates each one:

1. Query `store.recent_coverage_summary(feed_slug, days=3)`
2. Send today's headlines + the list of recent themes to Gemini Flash-Lite:
   - "Tag each headline with any matching prior theme from this list, or mark it FRESH"
3. Annotate each headline in `headlines_with_snippets`:
   - `[RECURRING: "US-Iran War" - covered 3/3 days, lead 2x]`
   - `[FRESH]`

This is a single lightweight LLM call added to the collector pipeline.

### 3. Editor Prompt Restructuring

Replace the raw prior-script context with structured data:

```
## COVERAGE LEDGER (last 3 episodes)
| Theme              | Days Covered | Last Lead | Episodes      |
|--------------------|-------------|-----------|---------------|
| US-Iran War        | 3/3         | Mar 11    | Mar 9, 10, 11 |
| Congressional War  | 2/3         | never     | Mar 9, 10     |
| THAAD Transfer     | 1/3         | never     | Mar 11        |

## FRESHNESS BUDGET
- At least 40% of selected stories must be tagged [FRESH]
- Themes covered 3+ consecutive days CANNOT be the lead story
- Recurring themes: only include if you can cite a specific new fact,
  number, or event that justifies re-covering them
- A shorter episode with genuinely fresh material is better than a
  longer one that retreads familiar ground
```

The raw prior scripts are removed from the editor prompt (saves tokens, reduces noise). The writer still receives prior scripts for prose-level dedup.

### 4. Editor Response Schema Update

Add an optional field to the research plan models:

```python
rotation_override: str | None  # Explanation if freshness budget was not met
```

### 5. Graceful Degradation

- **No prior `articles_json`:** Fall back to LLM-based theme extraction from prior scripts to build the coverage ledger.
- **Slow news day (< 40% fresh):** Editor may relax the freshness budget but must set `rotation_override` explaining why.
- **Breaking event on a recurring theme:** Editor may override the "3+ days cannot lead" rule by setting `rotation_override` with justification.

## Files to Modify

### Shared infrastructure:
- `pipeline/db.py` — Add `recent_coverage_summary()` method
- `pipeline/freshness.py` (new) — Headline annotation logic, theme matching LLM call, coverage ledger formatting

### FP Digest:
- `pipeline/fp_collector.py` — Call freshness annotation before editor, pass annotated headlines + coverage ledger
- `pipeline/fp_editor.py` — Replace raw script context with structured ledger + freshness budget prompt

### The Rundown:
- `pipeline/things_happen_collector.py` — Same freshness annotation integration
- `pipeline/things_happen_editor.py` — Same editor prompt restructuring

### Models:
- `pipeline/fp_editor.py` — Add `rotation_override` to `FPResearchPlan`
- `pipeline/things_happen_editor.py` — Add `rotation_override` to `RundownResearchPlan`

## What Does NOT Change

- `fp_writer.py` / `rundown_writer.py` — Still receive prior scripts for prose-level dedup (unchanged)
- `show_notes.py` — No changes
- `feed.py` — No changes
- `consumer.py` — May need minor wiring to pass `StateStore` to collectors

## Risks

- **Theme matching quality:** Gemini Flash-Lite may occasionally misclassify a headline as matching a prior theme when it's actually a distinct story. Mitigation: the editor can override.
- **40% budget is arbitrary:** May need tuning after a few days of observation.
- **Token budget for classification call:** Should be small (headlines + ~10 theme names). Well within Flash-Lite limits.
