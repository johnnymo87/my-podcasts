# Theme Rotation & Freshness Budget Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce repetitive episode content by giving the collector and editor structured awareness of recently covered themes, with a freshness budget that forces story rotation.

**Architecture:** Add a `recent_coverage_summary()` method to `StateStore` that queries `articles_json` from recent episodes. Create a new `pipeline/freshness.py` module that (1) builds a coverage ledger, (2) calls Gemini Flash-Lite to classify today's headlines against prior themes, and (3) annotates headlines with freshness tags. Both FP Digest and The Rundown editors get the structured ledger and annotated headlines instead of raw prior scripts.

**Tech Stack:** Python, SQLite (existing), google-genai (Gemini Flash-Lite), Pydantic

---

### Task 1: Coverage Ledger — DB Query

Add a method to `StateStore` that returns structured coverage history from `articles_json`.

**Files:**
- Modify: `pipeline/db.py` (add method after `list_episodes` ~line 247)
- Test: `pipeline/test_show_notes_db.py` (add tests)

**Step 1: Write failing tests**

Add to `pipeline/test_show_notes_db.py`:

```python
def test_recent_coverage_summary_returns_themes(tmp_path):
    """Coverage summary extracts themes from articles_json."""
    store = StateStore(tmp_path / "test.db")
    # Episode with articles_json
    store.insert_episode(Episode(
        id="ep-1", title="2026-03-10 - FP Digest", slug="2026-03-10-fp-digest",
        pub_date="Tue, 10 Mar 2026 22:00:00 +0000", r2_key="episodes/fp-digest/2026-03-10.mp3",
        feed_slug="fp-digest", category="News", source_tag=None,
        preset_name="FP Digest", source_url=None, size_bytes=1000,
        duration_seconds=300, summary="Test summary",
        articles_json='[{"title":"School Strike","url":"https://example.com/1","theme":"US-Iran War"},{"title":"Troops Wounded","url":"https://example.com/2","theme":"US-Iran War"},{"title":"THAAD Move","url":"https://example.com/3","theme":"Military Overstretch"}]',
    ))
    result = store.recent_coverage_summary("fp-digest", days=3)
    assert len(result) == 2
    iran = next(t for t in result if t["theme"] == "US-Iran War")
    assert iran["days_covered"] == 1
    assert iran["article_count"] == 2
    assert iran["was_lead"] is True  # First theme in list
    overstretch = next(t for t in result if t["theme"] == "Military Overstretch")
    assert overstretch["was_lead"] is False
    store.close()


def test_recent_coverage_summary_multi_day(tmp_path):
    """Coverage summary aggregates across multiple episodes."""
    store = StateStore(tmp_path / "test.db")
    store.insert_episode(Episode(
        id="ep-1", title="2026-03-10 - FP Digest", slug="2026-03-10-fp-digest",
        pub_date="Tue, 10 Mar 2026 22:00:00 +0000", r2_key="episodes/fp-digest/2026-03-10.mp3",
        feed_slug="fp-digest", category="News", source_tag=None,
        preset_name="FP Digest", source_url=None, size_bytes=1000,
        duration_seconds=300, summary=None,
        articles_json='[{"title":"A","url":null,"theme":"Iran War"},{"title":"B","url":null,"theme":"Congress"}]',
    ))
    store.insert_episode(Episode(
        id="ep-2", title="2026-03-11 - FP Digest", slug="2026-03-11-fp-digest",
        pub_date="Wed, 11 Mar 2026 22:00:00 +0000", r2_key="episodes/fp-digest/2026-03-11.mp3",
        feed_slug="fp-digest", category="News", source_tag=None,
        preset_name="FP Digest", source_url=None, size_bytes=1000,
        duration_seconds=300, summary=None,
        articles_json='[{"title":"C","url":null,"theme":"Iran War"},{"title":"D","url":null,"theme":"THAAD"}]',
    ))
    result = store.recent_coverage_summary("fp-digest", days=3)
    iran = next(t for t in result if t["theme"] == "Iran War")
    assert iran["days_covered"] == 2
    assert iran["article_count"] == 2  # 1 per episode
    assert iran["was_lead"] is True  # Lead in both episodes
    store.close()


def test_recent_coverage_summary_no_articles_json(tmp_path):
    """Episodes without articles_json are silently skipped."""
    store = StateStore(tmp_path / "test.db")
    store.insert_episode(Episode(
        id="ep-1", title="2026-03-10 - FP Digest", slug="2026-03-10-fp-digest",
        pub_date="Tue, 10 Mar 2026 22:00:00 +0000", r2_key="episodes/fp-digest/2026-03-10.mp3",
        feed_slug="fp-digest", category="News", source_tag=None,
        preset_name="FP Digest", source_url=None, size_bytes=1000,
        duration_seconds=300,
    ))
    result = store.recent_coverage_summary("fp-digest", days=3)
    assert result == []
    store.close()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_show_notes_db.py -v -k "coverage_summary"`
Expected: FAIL — `AttributeError: 'StateStore' object has no attribute 'recent_coverage_summary'`

**Step 3: Implement `recent_coverage_summary`**

Add to `pipeline/db.py` after `list_episodes` (after line 247):

```python
def recent_coverage_summary(
    self, feed_slug: str, days: int = 3
) -> list[dict]:
    """Return coverage frequency of themes from recent episodes.

    Queries articles_json from the most recent episodes within the
    given day window.  Returns a list of dicts:
      {"theme": str, "days_covered": int, "article_count": int,
       "episode_dates": list[str], "was_lead": bool}
    sorted by days_covered descending, then article_count descending.
    """
    episodes = self.list_episodes(feed_slug=feed_slug)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # theme -> {dates: set, articles: int, lead_count: int}
    theme_stats: dict[str, dict] = {}

    for ep in episodes:
        try:
            ep_dt = parsedate_to_datetime(ep.pub_date)
        except Exception:
            continue
        if ep_dt < cutoff:
            continue
        if not ep.articles_json:
            continue
        try:
            articles = json.loads(ep.articles_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not articles:
            continue

        # Extract date string from pub_date for readability
        date_str = ep_dt.strftime("%Y-%m-%d")
        lead_theme = articles[0].get("theme", "") if articles else ""

        seen_themes_this_ep: dict[str, int] = {}
        for art in articles:
            theme = art.get("theme", "")
            if not theme:
                continue
            seen_themes_this_ep[theme] = seen_themes_this_ep.get(theme, 0) + 1

        for theme, count in seen_themes_this_ep.items():
            if theme not in theme_stats:
                theme_stats[theme] = {
                    "dates": set(),
                    "articles": 0,
                    "lead_count": 0,
                }
            theme_stats[theme]["dates"].add(date_str)
            theme_stats[theme]["articles"] += count
            if theme == lead_theme:
                theme_stats[theme]["lead_count"] += 1

    result = []
    for theme, stats in theme_stats.items():
        result.append({
            "theme": theme,
            "days_covered": len(stats["dates"]),
            "article_count": stats["articles"],
            "episode_dates": sorted(stats["dates"]),
            "was_lead": stats["lead_count"] > 0,
        })

    result.sort(key=lambda r: (-r["days_covered"], -r["article_count"]))
    return result
```

Requires adding these imports to `db.py` if not already present:
```python
import json
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_show_notes_db.py -v -k "coverage_summary"`
Expected: 3 PASS

**Step 5: Commit**

```bash
git add pipeline/db.py pipeline/test_show_notes_db.py
git commit --no-gpg-sign -m "feat: add recent_coverage_summary to StateStore

Queries articles_json from recent episodes and returns structured theme
coverage frequency data (days covered, article count, lead status)."
```

---

### Task 2: Freshness Module — Theme Classification & Annotation

Create `pipeline/freshness.py` with functions to classify headlines against prior themes and annotate them with freshness tags.

**Files:**
- Create: `pipeline/freshness.py`
- Create: `pipeline/test_freshness.py`

**Step 1: Write failing tests**

Create `pipeline/test_freshness.py`:

```python
"""Tests for the freshness annotation module."""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

from pipeline.freshness import (
    format_coverage_ledger,
    annotate_headlines,
    build_freshness_prompt,
    HeadlineClassification,
)


def test_format_coverage_ledger_empty():
    result = format_coverage_ledger([])
    assert result == ""


def test_format_coverage_ledger_with_data():
    summary = [
        {
            "theme": "US-Iran War",
            "days_covered": 3,
            "article_count": 6,
            "episode_dates": ["2026-03-09", "2026-03-10", "2026-03-11"],
            "was_lead": True,
        },
        {
            "theme": "THAAD Transfer",
            "days_covered": 1,
            "article_count": 2,
            "episode_dates": ["2026-03-11"],
            "was_lead": False,
        },
    ]
    result = format_coverage_ledger(summary)
    assert "US-Iran War" in result
    assert "3/3" in result or "3 of 3" in result
    assert "LEAD" in result
    assert "THAAD Transfer" in result
    assert "1/3" in result or "1 of 3" in result


def test_annotate_headlines_marks_fresh_and_recurring():
    headlines = [
        "[homepage/iran] School strike update\nContext: New details...",
        "[rss/antiwar] New Somalia airstrike\nContext: US launches...",
    ]
    # Simulate LLM classification: first matches "Iran War", second is fresh
    classifications = [
        HeadlineClassification(
            headline_index=0,
            matched_theme="US-Iran War",
        ),
        HeadlineClassification(
            headline_index=1,
            matched_theme=None,
        ),
    ]
    coverage = [
        {
            "theme": "US-Iran War",
            "days_covered": 3,
            "article_count": 6,
            "episode_dates": ["2026-03-09", "2026-03-10", "2026-03-11"],
            "was_lead": True,
        },
    ]
    result = annotate_headlines(headlines, classifications, coverage)
    assert "[RECURRING" in result[0]
    assert "3/3" in result[0] or "3 of 3" in result[0]
    assert "[FRESH]" in result[1]


def test_annotate_headlines_no_coverage_all_fresh():
    headlines = ["[rss/antiwar] Something new\nContext: ..."]
    classifications = [
        HeadlineClassification(headline_index=0, matched_theme=None),
    ]
    result = annotate_headlines(headlines, classifications, [])
    assert "[FRESH]" in result[0]


def test_build_freshness_prompt_includes_themes_and_headlines():
    headlines = ["[homepage/iran] Strike update\nContext: ..."]
    coverage = [
        {"theme": "Iran War", "days_covered": 2, "article_count": 4,
         "episode_dates": ["2026-03-10", "2026-03-11"], "was_lead": True},
    ]
    prompt = build_freshness_prompt(headlines, coverage)
    assert "Iran War" in prompt
    assert "Strike update" in prompt
    assert "headline_index" in prompt  # Schema mention
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_freshness.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.freshness'`

**Step 3: Implement `pipeline/freshness.py`**

```python
"""Headline freshness annotation for theme rotation.

Classifies today's headlines against recently covered themes and
annotates them with [FRESH] or [RECURRING] tags so the editor can
enforce a freshness budget.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class HeadlineClassification(BaseModel):
    """LLM output: maps a headline to a prior theme or marks it fresh."""
    headline_index: int
    matched_theme: str | None  # None means FRESH


class ClassificationResult(BaseModel):
    """LLM output: list of headline classifications."""
    classifications: list[HeadlineClassification]


def format_coverage_ledger(
    coverage_summary: list[dict],
    window_days: int = 3,
) -> str:
    """Format coverage summary as a markdown table for the editor prompt.

    Returns empty string if no coverage data.
    """
    if not coverage_summary:
        return ""

    lines = [
        "## COVERAGE LEDGER (last {n} episodes)".format(n=window_days),
        "",
        "| Theme | Days Covered | Lead? | Dates |",
        "|-------|-------------|-------|-------|",
    ]
    for entry in coverage_summary:
        theme = entry["theme"]
        days = entry["days_covered"]
        lead = "LEAD" if entry["was_lead"] else ""
        dates = ", ".join(entry["episode_dates"])
        lines.append(f"| {theme} | {days}/{window_days} | {lead} | {dates} |")

    lines.append("")
    lines.append("## FRESHNESS BUDGET")
    lines.append("- At least 40% of selected stories must be tagged [FRESH]")
    lines.append(
        "- Themes covered 3+ consecutive days CANNOT be the lead story"
    )
    lines.append(
        "- Recurring themes: only include if you can cite a specific new "
        "fact, number, or event that justifies re-covering them"
    )
    lines.append(
        "- A shorter episode with genuinely fresh material is better than "
        "a longer one that retreads familiar ground"
    )
    lines.append(
        "- If you cannot meet the 40% freshness budget due to limited "
        "fresh material, set rotation_override explaining why"
    )

    return "\n".join(lines)


def build_freshness_prompt(
    headlines: list[str],
    coverage_summary: list[dict],
) -> str:
    """Build the Gemini prompt for classifying headlines against prior themes."""
    theme_names = [entry["theme"] for entry in coverage_summary]

    prompt = (
        "You are classifying news headlines against a list of previously "
        "covered themes from recent podcast episodes.\n\n"
        "PRIOR THEMES:\n"
    )
    for entry in coverage_summary:
        prompt += f"- \"{entry['theme']}\" (covered {entry['days_covered']} days)\n"

    prompt += (
        "\nFor each headline below, determine if it belongs to one of the "
        "prior themes above. If it does, set matched_theme to the exact "
        "theme name string. If the headline covers a genuinely different "
        "topic, set matched_theme to null.\n\n"
        "Be generous with matching — if a headline is about the same "
        "ongoing situation or storyline as a prior theme, it matches, "
        "even if the specific angle is different.\n\n"
        "HEADLINES:\n"
    )
    for i, headline in enumerate(headlines):
        # Only include the first line (headline) not the full context
        first_line = headline.split("\n")[0]
        prompt += f"{i}: {first_line}\n"

    prompt += (
        "\nReturn a JSON object with a 'classifications' array. Each element "
        "must have 'headline_index' (int) and 'matched_theme' (string or null)."
    )
    return prompt


def classify_headlines(
    headlines: list[str],
    coverage_summary: list[dict],
) -> list[HeadlineClassification]:
    """Call Gemini Flash-Lite to classify headlines against prior themes.

    Returns a HeadlineClassification for each headline. If the API call
    fails or coverage_summary is empty, returns all headlines as FRESH.
    """
    if not coverage_summary or not headlines:
        return [
            HeadlineClassification(headline_index=i, matched_theme=None)
            for i in range(len(headlines))
        ]

    prompt = build_freshness_prompt(headlines, coverage_summary)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set; marking all headlines FRESH")
        return [
            HeadlineClassification(headline_index=i, matched_theme=None)
            for i in range(len(headlines))
        ]

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ClassificationResult,
                temperature=0.1,
            ),
        )
        parsed = response.parsed
        if parsed and parsed.classifications:
            return parsed.classifications
    except Exception:
        logger.exception("Gemini classification failed; marking all FRESH")

    return [
        HeadlineClassification(headline_index=i, matched_theme=None)
        for i in range(len(headlines))
    ]


def annotate_headlines(
    headlines: list[str],
    classifications: list[HeadlineClassification],
    coverage_summary: list[dict],
) -> list[str]:
    """Prepend [FRESH] or [RECURRING ...] tags to each headline string."""
    # Build lookup from coverage summary
    theme_info = {entry["theme"]: entry for entry in coverage_summary}

    # Build index->classification lookup
    class_by_idx = {c.headline_index: c for c in classifications}

    annotated = []
    for i, headline in enumerate(headlines):
        classification = class_by_idx.get(i)
        if classification and classification.matched_theme:
            theme = classification.matched_theme
            info = theme_info.get(theme, {})
            days = info.get("days_covered", "?")
            window = 3
            lead = ", LEAD" if info.get("was_lead") else ""
            tag = f"[RECURRING: \"{theme}\" - {days}/{window} days{lead}]"
        else:
            tag = "[FRESH]"
        annotated.append(f"{tag} {headline}")

    return annotated
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_freshness.py -v`
Expected: 5 PASS

**Step 5: Commit**

```bash
git add pipeline/freshness.py pipeline/test_freshness.py
git commit --no-gpg-sign -m "feat: add freshness module for headline classification

New pipeline/freshness.py with coverage ledger formatting, Gemini-based
headline classification against prior themes, and [FRESH]/[RECURRING]
annotation. Gracefully degrades to all-FRESH when no coverage data or
API key is unavailable."
```

---

### Task 3: Editor Prompt Changes — FP Digest

Update `fp_editor.py` to accept the coverage ledger and use annotated headlines instead of raw scripts. Add `rotation_override` to the model.

**Files:**
- Modify: `pipeline/fp_editor.py`
- Modify: `pipeline/test_fp_editor.py` (if it exists, otherwise the tests that call the editor)

**Step 1: Update `FPResearchPlan` model**

In `pipeline/fp_editor.py`, add `rotation_override` to `FPResearchPlan`:

```python
class FPResearchPlan(BaseModel):
    themes: list[str]                    # 3-5 dominant themes
    directives: list[FPStoryDirective]
    rotation_override: str | None = None  # Explanation if freshness budget unmet
```

**Step 2: Update `generate_fp_research_plan` signature and prompt**

Change the function signature to accept a coverage ledger string instead of raw scripts:

```python
def generate_fp_research_plan(
    headlines_with_snippets: list[str],
    context_scripts: list[str] | None = None,  # Keep for backward compat
    coverage_ledger: str | None = None,         # New: structured coverage data
) -> FPResearchPlan:
```

Replace the `context_scripts` prompt section with the coverage ledger:

```python
# Old: raw script context
# if context_scripts:
#     prompt += "\n\nPrevious episodes ..."
#     for script in context_scripts:
#         prompt += f"\n---\n{script}\n"

# New: structured coverage ledger (preferred) or fallback to scripts
if coverage_ledger:
    prompt += f"\n\n{coverage_ledger}\n"
elif context_scripts:
    # Fallback for transition period
    prompt += (
        "\n\nPrevious episodes (listeners already heard these):\n"
        "Deprioritize stories that were covered in depth unless there is a\n"
        "significant new development (new facts, new numbers, a policy change,\n"
        "a concrete event — not just continued coverage of the same situation).\n"
        "When in doubt, prefer fresh stories over continuing threads.\n"
    )
    for script in context_scripts:
        prompt += f"\n---\n{script}\n"
```

**Step 3: Write a test for the new prompt path**

Add to existing editor tests (or create `pipeline/test_fp_editor.py` if needed):

```python
def test_fp_editor_uses_coverage_ledger_over_scripts():
    """When coverage_ledger is provided, scripts are not included in prompt."""
    from unittest.mock import patch, MagicMock
    from pipeline.fp_editor import generate_fp_research_plan, FPResearchPlan

    mock_plan = FPResearchPlan(themes=["Theme A"], directives=[])
    mock_response = MagicMock()
    mock_response.parsed = mock_plan

    with patch("pipeline.fp_editor.genai") as mock_genai:
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = mock_response

        generate_fp_research_plan(
            ["[homepage/iran] Test headline\nContext: ..."],
            context_scripts=["Old script text here"],
            coverage_ledger="## COVERAGE LEDGER\n| Theme | Days |",
        )

        prompt_used = mock_client.models.generate_content.call_args[1]["contents"]
        assert "COVERAGE LEDGER" in prompt_used
        assert "Old script text here" not in prompt_used
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_fp_editor.py -v` (or wherever FP editor tests live)
Expected: PASS

**Step 5: Commit**

```bash
git add pipeline/fp_editor.py pipeline/test_fp_editor.py
git commit --no-gpg-sign -m "feat: FP editor accepts coverage ledger for theme rotation

Replace raw script context with structured coverage ledger in the editor
prompt. Add rotation_override field to FPResearchPlan. Falls back to
script context when no ledger provided."
```

---

### Task 4: Editor Prompt Changes — The Rundown

Same changes as Task 3 but for `things_happen_editor.py`.

**Files:**
- Modify: `pipeline/things_happen_editor.py`
- Modify: `pipeline/test_rundown_editor.py` (or wherever Rundown editor tests live)

**Step 1: Update `RundownResearchPlan` model**

Add `rotation_override`:
```python
class RundownResearchPlan(BaseModel):
    themes: list[str]
    directives: list[RundownStoryDirective]
    rotation_override: str | None = None
```

**Step 2: Update `generate_rundown_research_plan` signature and prompt**

Same pattern as FP editor: accept `coverage_ledger: str | None = None`, prefer it over `context_scripts`.

**Step 3: Write test**

Same pattern as Task 3.

**Step 4: Run tests, commit**

```bash
git add pipeline/things_happen_editor.py pipeline/test_rundown_writer.py
git commit --no-gpg-sign -m "feat: Rundown editor accepts coverage ledger for theme rotation

Same structured coverage ledger support as FP editor. Add
rotation_override field to RundownResearchPlan."
```

---

### Task 5: Collector Integration — FP Digest

Wire the freshness annotation into `fp_collector.py`. The collector needs to receive a `StateStore` (or the coverage summary directly) so it can query recent coverage data.

**Files:**
- Modify: `pipeline/fp_collector.py`
- Modify: `pipeline/consumer.py` (pass store to collector)
- Modify: `pipeline/__main__.py` (pass store to collector in full-run path)

**Step 1: Update `collect_fp_artifacts` signature**

Add a `coverage_summary` parameter — this keeps the collector decoupled from `StateStore` (the caller computes the summary and passes it in):

```python
def collect_fp_artifacts(
    job_id: str,
    work_dir: Path,
    scripts_source_dir: Path | None = None,
    fp_routed_dir: Path | None = None,
    homepage_cache_dir: Path | None = None,
    antiwar_rss_cache_dir: Path | None = None,
    semafor_cache_dir: Path | None = None,
    lookback_days: int = 2,
    coverage_summary: list[dict] | None = None,  # NEW
) -> None:
```

**Step 2: Add freshness annotation before editor call**

After building `headlines_with_snippets` (Phase 4) and before calling `generate_fp_research_plan` (Phase 5):

```python
from pipeline.freshness import (
    classify_headlines,
    annotate_headlines,
    format_coverage_ledger,
)

# Phase 4.5: Freshness annotation
coverage_ledger: str | None = None
if coverage_summary:
    classifications = classify_headlines(headlines_with_snippets, coverage_summary)
    headlines_with_snippets = annotate_headlines(
        headlines_with_snippets, classifications, coverage_summary
    )
    coverage_ledger = format_coverage_ledger(coverage_summary)

# Phase 5: Editor call (updated)
plan = generate_fp_research_plan(
    headlines_with_snippets,
    context_scripts=context_scripts if not coverage_ledger else None,
    coverage_ledger=coverage_ledger,
)
```

Note: When coverage_ledger is available, we pass `context_scripts=None` to the editor (the ledger replaces scripts). The scripts are still saved to the work_dir for the writer to use later.

**Step 3: Update consumer.py to pass coverage_summary**

In `consumer.py`, before calling `collect_fp_artifacts`:

```python
fp_coverage = store.recent_coverage_summary("fp-digest", days=3)
collect_fp_artifacts(
    job["id"],
    work_dir,
    fp_routed_dir=...,
    homepage_cache_dir=...,
    antiwar_rss_cache_dir=...,
    semafor_cache_dir=...,
    lookback_days=fp_lookback,
    coverage_summary=fp_coverage,
)
```

**Step 4: Update `__main__.py` full-run path**

In `_fp_digest_full_run`, pass coverage_summary from the store:

```python
fp_coverage = store.recent_coverage_summary("fp-digest", days=3)
collect_fp_artifacts(
    ...,
    coverage_summary=fp_coverage,
)
```

The dry-run path should NOT pass coverage_summary (no store available), so it will use the script fallback.

**Step 5: Write integration test**

Add to `pipeline/test_freshness.py`:

```python
def test_freshness_integration_with_collector_args():
    """Verify annotate_headlines output format matches what editors expect."""
    headlines = [
        "[homepage/iran] School strike latest\nContext: New details on...",
        "[rss/caitlinjohnstone] New essay on media\nContext: Media coverage...",
    ]
    coverage = [
        {"theme": "US-Iran War", "days_covered": 3, "article_count": 6,
         "episode_dates": ["2026-03-09", "2026-03-10", "2026-03-11"],
         "was_lead": True},
    ]
    classifications = [
        HeadlineClassification(headline_index=0, matched_theme="US-Iran War"),
        HeadlineClassification(headline_index=1, matched_theme=None),
    ]
    annotated = annotate_headlines(headlines, classifications, coverage)
    # Annotated headlines should still contain the original content
    assert "School strike latest" in annotated[0]
    assert "New essay on media" in annotated[1]
    # Tags are prepended
    assert annotated[0].startswith("[RECURRING")
    assert annotated[1].startswith("[FRESH]")
```

**Step 6: Run all tests**

Run: `uv run pytest pipeline/ -v --tb=short`
Expected: All pass (except pre-existing `test_fp_collector.py::test_collect_fp_artifacts` failure)

**Step 7: Commit**

```bash
git add pipeline/fp_collector.py pipeline/consumer.py pipeline/__main__.py pipeline/test_freshness.py
git commit --no-gpg-sign -m "feat: wire freshness annotation into FP Digest collector

Collector classifies headlines against prior themes via Gemini, annotates
with [FRESH]/[RECURRING] tags, and passes structured coverage ledger to
editor instead of raw scripts. Consumer and CLI pass coverage_summary
from StateStore."
```

---

### Task 6: Collector Integration — The Rundown

Same wiring as Task 5 but for `things_happen_collector.py`.

**Files:**
- Modify: `pipeline/things_happen_collector.py`
- Modify: `pipeline/consumer.py` (pass coverage_summary for Rundown)
- Modify: `pipeline/__main__.py` (pass coverage_summary in Rundown full-run)

**Step 1: Update `collect_all_artifacts` signature**

Add `coverage_summary: list[dict] | None = None`.

**Step 2: Add freshness annotation**

Same pattern: after building `headlines_with_snippets`, classify, annotate, format ledger. Pass ledger to `generate_rundown_research_plan`.

Note: The Rundown collector currently does NOT pass `context_scripts` to the editor (only passes `headlines_with_snippets`). We now pass `coverage_ledger` instead. The scripts are still copied for the writer.

```python
plan = generate_rundown_research_plan(
    headlines_with_snippets,
    coverage_ledger=coverage_ledger,
)
```

**Step 3: Update consumer.py**

```python
rundown_coverage = store.recent_coverage_summary("the-rundown", days=3)
collect_all_artifacts(
    job["id"],
    work_dir,
    ...,
    coverage_summary=rundown_coverage,
)
```

**Step 4: Update `__main__.py`**

Same pattern as Task 5.

**Step 5: Run all tests**

Run: `uv run pytest pipeline/ -v --tb=short`

**Step 6: Commit**

```bash
git add pipeline/things_happen_collector.py pipeline/consumer.py pipeline/__main__.py
git commit --no-gpg-sign -m "feat: wire freshness annotation into Rundown collector

Rundown collector now classifies headlines and passes structured coverage
ledger to editor. Adds theme rotation awareness that was previously
missing entirely from The Rundown pipeline."
```

---

### Task 7: Script Fallback for Episodes Without articles_json

For episodes that predate the show notes feature, extract themes from prior scripts using an LLM call so the coverage ledger isn't empty during the transition period.

**Files:**
- Modify: `pipeline/freshness.py` (add `extract_themes_from_scripts` function)
- Modify: `pipeline/fp_collector.py` (use fallback when coverage_summary is empty)
- Modify: `pipeline/things_happen_collector.py` (same)
- Test: `pipeline/test_freshness.py`

**Step 1: Add `extract_themes_from_scripts` to freshness.py**

```python
class ScriptThemes(BaseModel):
    """LLM output: themes extracted from a prior episode script."""
    themes: list[str]  # 3-5 theme names


def extract_themes_from_scripts(
    scripts: list[str],
) -> list[dict]:
    """Extract theme names from prior episode scripts via LLM.

    Fallback for when articles_json is not available. Returns a
    coverage_summary-compatible list of dicts.
    """
    if not scripts:
        return []

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return []

    all_themes: dict[str, set[str]] = {}  # theme -> set of script indices

    for i, script in enumerate(scripts):
        # Truncate long scripts to save tokens
        truncated = script[:3000]
        prompt = (
            "Extract the 3-5 main themes or storylines from this podcast "
            "episode script. Return short theme names (2-5 words each).\n\n"
            f"Script:\n{truncated}"
        )
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-3.1-flash-lite-preview",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ScriptThemes,
                    temperature=0.1,
                ),
            )
            parsed = response.parsed
            if parsed and parsed.themes:
                for theme in parsed.themes:
                    if theme not in all_themes:
                        all_themes[theme] = set()
                    all_themes[theme].add(str(i))
        except Exception:
            logger.exception("Failed to extract themes from script %d", i)
            continue

    return [
        {
            "theme": theme,
            "days_covered": len(indices),
            "article_count": 0,
            "episode_dates": sorted(indices),
            "was_lead": False,  # Can't determine lead from scripts
        }
        for theme, indices in all_themes.items()
    ]
```

**Step 2: Use fallback in collectors**

In both `fp_collector.py` and `things_happen_collector.py`, after the freshness annotation block:

```python
if coverage_summary:
    # ... existing freshness annotation code ...
elif context_scripts:
    # Fallback: extract themes from scripts
    from pipeline.freshness import extract_themes_from_scripts
    fallback_coverage = extract_themes_from_scripts(context_scripts)
    if fallback_coverage:
        classifications = classify_headlines(headlines_with_snippets, fallback_coverage)
        headlines_with_snippets = annotate_headlines(
            headlines_with_snippets, classifications, fallback_coverage
        )
        coverage_ledger = format_coverage_ledger(fallback_coverage)
```

**Step 3: Write test**

```python
def test_extract_themes_returns_coverage_format():
    """Verify extract_themes_from_scripts returns coverage-compatible dicts."""
    from unittest.mock import patch, MagicMock
    from pipeline.freshness import extract_themes_from_scripts, ScriptThemes

    mock_response = MagicMock()
    mock_response.parsed = ScriptThemes(themes=["Iran War", "THAAD Transfer"])

    with patch("pipeline.freshness.genai") as mock_genai:
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = mock_response

        result = extract_themes_from_scripts(["Script text here"])
        assert len(result) == 2
        assert result[0]["theme"] in ("Iran War", "THAAD Transfer")
        assert "days_covered" in result[0]
        assert "article_count" in result[0]
```

**Step 4: Run tests, commit**

```bash
git add pipeline/freshness.py pipeline/test_freshness.py pipeline/fp_collector.py pipeline/things_happen_collector.py
git commit --no-gpg-sign -m "feat: script-based theme extraction fallback

When articles_json is not available for recent episodes, extract themes
from prior scripts via Gemini to populate the coverage ledger. Ensures
freshness annotation works during the transition period."
```

---

### Task 8: Restart Consumer & Verify

**Step 1: Run full test suite**

Run: `uv run pytest pipeline/ -v --tb=short`
Expected: All pass (except pre-existing `test_fp_collector.py::test_collect_fp_artifacts`)

**Step 2: Restart consumer**

```bash
sudo systemctl restart my-podcasts-consumer
sleep 2
sudo systemctl status my-podcasts-consumer --no-pager
```

**Step 3: Push to remote**

```bash
git push
```

**Step 4: Monitor next episode generation**

After the next scheduled run, check:
1. Consumer logs for freshness annotation output
2. The generated plan.json for theme diversity
3. The resulting script for reduced repetition

```bash
sudo journalctl -u my-podcasts-consumer --since "today" --no-pager | tail -50
```
