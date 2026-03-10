# Rundown Non-Interactive Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace The Rundown's broken interactive agent architecture with a fully synchronous, non-interactive pipeline matching FP Digest's proven architecture.

**Architecture:** The Rundown currently launches an opencode agent that blocks on a Question tool (which has no Telegram integration in opencode-serve headless mode). We replace this with: collector writes `plan.json` -> synchronous script generation via `rundown_writer.py` -> TTS -> publish. All steps are inline, no agent sessions, no session tracking.

**Tech Stack:** Python 3.12+, Pydantic, google-genai (Gemini Flash-Lite), opencode-serve HTTP API, pytest

---

### Task 1: Enhance `things_happen_editor.py` — add themes, story selection, context awareness

**Files:**
- Modify: `pipeline/things_happen_editor.py` (full rewrite, 69 lines)
- Test: `pipeline/test_things_happen_editor.py` (full rewrite, 69 lines)

**Context:** Currently the editor only classifies stories with `is_foreign_policy`, `needs_exa`, `is_ai` flags. The new editor must also identify themes, assign priorities, and select which stories to include in the episode — matching `fp_editor.py`'s `FPResearchPlan` / `FPStoryDirective` pattern.

**Step 1: Write the failing tests**

Replace the contents of `pipeline/test_things_happen_editor.py` with:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.things_happen_editor import (
    RundownResearchPlan,
    RundownStoryDirective,
    generate_rundown_research_plan,
)


def test_generate_plan_returns_empty_if_no_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = generate_rundown_research_plan(["Some headline"])
    assert result.themes == []
    assert result.directives == []


def test_generate_plan_returns_empty_if_no_headlines(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    result = generate_rundown_research_plan([])
    assert result.themes == []
    assert result.directives == []


@patch("pipeline.things_happen_editor.genai.Client")
def test_generate_plan_success(mock_client_class, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    mock_response = MagicMock()
    mock_response.parsed = RundownResearchPlan(
        themes=["Tech", "Finance"],
        directives=[
            RundownStoryDirective(
                headline="Tech Company IPO",
                source="semafor",
                priority=1,
                theme="Tech",
                needs_exa=True,
                exa_query="tech company IPO 2026",
                is_foreign_policy=False,
                fp_query="",
                is_ai=False,
                ai_query="",
                include_in_episode=True,
            ),
            RundownStoryDirective(
                headline="War Breaks Out",
                source="levine",
                priority=3,
                theme="Geopolitics",
                needs_exa=False,
                exa_query="",
                is_foreign_policy=True,
                fp_query="war breaks out",
                is_ai=False,
                ai_query="",
                include_in_episode=False,
            ),
        ],
    )
    mock_client.models.generate_content.return_value = mock_response

    result = generate_rundown_research_plan(["Tech Company IPO", "War Breaks Out"])

    assert len(result.themes) == 2
    assert "Tech" in result.themes
    assert len(result.directives) == 2
    assert result.directives[0].include_in_episode is True
    assert result.directives[1].is_foreign_policy is True
    assert result.directives[1].include_in_episode is False

    mock_client.models.generate_content.assert_called_once()
    kwargs = mock_client.models.generate_content.call_args[1]
    assert kwargs["model"] == "gemini-3.1-flash-lite-preview"


@patch("pipeline.things_happen_editor.genai.Client")
def test_generate_plan_with_context_scripts(mock_client_class, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    mock_response = MagicMock()
    mock_response.parsed = RundownResearchPlan(themes=[], directives=[])
    mock_client.models.generate_content.return_value = mock_response

    generate_rundown_research_plan(
        ["Headline"],
        context_scripts=["Prior episode script content"],
    )

    kwargs = mock_client.models.generate_content.call_args[1]
    assert "Previous episodes" in kwargs["contents"]
    assert "Prior episode script content" in kwargs["contents"]


@patch("pipeline.things_happen_editor.genai.Client")
def test_generate_plan_handles_exception(mock_client_class, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.models.generate_content.side_effect = Exception("API error")

    result = generate_rundown_research_plan(["Headline"])
    assert result.themes == []
    assert result.directives == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_things_happen_editor.py -v`
Expected: FAIL — `RundownResearchPlan`, `RundownStoryDirective`, `generate_rundown_research_plan` do not exist yet.

**Step 3: Write the implementation**

Replace `pipeline/things_happen_editor.py` with:

```python
from __future__ import annotations

import os

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


class RundownStoryDirective(BaseModel):
    headline: str = Field(description="The original headline")
    source: str = Field(
        description="Which source this came from (e.g. 'levine', 'semafor', 'zvi')"
    )
    priority: int = Field(description="1-5, where 1 is the lead story")
    theme: str = Field(
        description="Grouping label (e.g. 'AI & Machine Learning', 'Markets & Finance', 'Media & Culture')"
    )
    needs_exa: bool = Field(
        description="True if the article is paywalled or inaccessible and needs an open-access alternative via web search"
    )
    exa_query: str = Field(
        description="Search query for finding open-access alternatives (3-6 keywords). Empty string if needs_exa is false."
    )
    is_foreign_policy: bool = Field(
        description="True if the story relates to war, geopolitics, international relations, or military conflicts"
    )
    fp_query: str = Field(
        description="A concise query to search antiwar/independent RSS feeds (2-4 keywords). Empty string if is_foreign_policy is false."
    )
    is_ai: bool = Field(
        description="True if the story focuses on artificial intelligence, LLMs, AI companies, or AI safety"
    )
    ai_query: str = Field(
        description="A concise query to search AI-focused independent RSS feeds (2-4 keywords). Empty string if is_ai is false."
    )
    include_in_episode: bool = Field(
        description="True if this story should be included in today's episode. Select 8-12 stories that best cover the major themes. Exclude foreign policy stories."
    )


class RundownResearchPlan(BaseModel):
    themes: list[str] = Field(
        description="The 3-5 dominant themes or story arcs identified across all sources today"
    )
    directives: list[RundownStoryDirective]


_EMPTY_PLAN = RundownResearchPlan(themes=[], directives=[])


def generate_rundown_research_plan(
    headlines_with_snippets: list[str],
    context_scripts: list[str] | None = None,
) -> RundownResearchPlan:
    """Ask Gemini Flash-Lite to triage stories into themes and select which to include."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _EMPTY_PLAN

    if not headlines_with_snippets:
        return _EMPTY_PLAN

    client = genai.Client(api_key=api_key)

    prompt = (
        "You are the editor of The Rundown, a daily podcast covering business, "
        "technology, AI, law, media, science, and culture. Foreign policy goes to "
        "a separate podcast — flag FP stories but do NOT include them in the episode.\n\n"
        "Analyze these headlines and:\n"
        "1. Identify 3-5 dominant themes or story arcs across all sources today\n"
        "2. Assign each story to a theme and set its priority (1=lead story)\n"
        "3. Select 8-12 stories that best cover the major themes — avoid redundancy\n"
        "4. Flag paywalled or inaccessible articles that need open-access alternatives via Exa\n"
        "5. Flag foreign policy stories (is_foreign_policy=true, include_in_episode=false)\n\n"
        "Headlines:\n"
    )
    for item in headlines_with_snippets:
        prompt += f"- {item}\n"

    if context_scripts:
        prompt += (
            "\n\nPrevious episodes (listeners already heard these):\n"
            "Deprioritize stories that were covered in depth unless there is a\n"
            "significant new development (new facts, new numbers, a policy change,\n"
            "a concrete event — not just continued coverage of the same situation).\n"
            "When in doubt, prefer fresh stories over continuing threads.\n"
        )
        for script in context_scripts:
            prompt += f"\n---\n{script}\n"

    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RundownResearchPlan,
                temperature=0.1,
            ),
        )
        parsed = response.parsed
        if isinstance(parsed, RundownResearchPlan):
            return parsed
        return _EMPTY_PLAN
    except Exception as e:
        print(f"Error generating Rundown research plan: {e}")
        return _EMPTY_PLAN
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_things_happen_editor.py -v`
Expected: All 6 tests PASS.

**Step 5: Commit**

```bash
git -c commit.gpgsign=false commit -am "refactor: replace Rundown editor with themed research plan (FP Digest pattern)"
```

---

### Task 2: Persist `plan.json` in collector and update imports

**Files:**
- Modify: `pipeline/things_happen_collector.py:162-167` (add plan.json write, update import)
- Modify: `pipeline/test_things_happen_collector.py` (update imports, verify plan.json)

**Context:** The collector currently calls `generate_research_plan()` and only uses the result for FP routing and enrichment. After Task 1, the function is `generate_rundown_research_plan()` returning a `RundownResearchPlan`. We need to persist it as `plan.json` so the writer can read it.

**Step 1: Update the test for plan.json persistence**

In `pipeline/test_things_happen_collector.py`, update the import at the top — change:
```python
from pipeline.things_happen_editor import ResearchDirective
```
to:
```python
from pipeline.things_happen_editor import RundownResearchPlan, RundownStoryDirective
```

Then update every mock that returns `ResearchDirective` to return `RundownStoryDirective` (with the new fields: `source`, `priority`, `theme`, `include_in_episode`).

Also update every mock that patches `generate_research_plan` to patch `generate_rundown_research_plan` and return a `RundownResearchPlan` instead of a list.

Add a new assertion to `test_collect_all_artifacts`:
```python
    # Verify plan.json written
    plan_path = work_dir / "plan.json"
    assert plan_path.exists()
    plan_data = json.loads(plan_path.read_text())
    assert "themes" in plan_data
    assert "directives" in plan_data
```

See the detailed changes in Step 3 for exactly what the mock return values become.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_things_happen_collector.py -v`
Expected: FAIL — `generate_research_plan` renamed, `ResearchDirective` renamed, no plan.json written.

**Step 3: Update the collector implementation**

In `pipeline/things_happen_collector.py`:

1. Change import:
```python
# Old:
from pipeline.things_happen_editor import generate_research_plan
# New:
from pipeline.things_happen_editor import generate_rundown_research_plan
```

2. Replace lines 162-167 (the Phase 2 block) with:
```python
    # Phase 2: Editor AI
    plan = generate_rundown_research_plan(headlines_with_snippets)

    # Write plan.json
    plan_path = work_dir / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    # Partition directives: FP links go to staging, non-FP get enriched
    fp_directives = [d for d in plan.directives if d.is_foreign_policy]
    non_fp_directives = [d for d in plan.directives if not d.is_foreign_policy]
```

3. The rest of the code (FP routing and enrichment) remains the same except the variable name changes from `directives` to `plan.directives` (already handled by the partition above).

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_things_happen_collector.py -v`
Expected: All tests PASS.

**Step 5: Commit**

```bash
git -c commit.gpgsign=false commit -am "feat: persist plan.json in Rundown collector, use new editor API"
```

---

### Task 3: Create `pipeline/rundown_writer.py`

**Files:**
- Create: `pipeline/rundown_writer.py`
- Create: `pipeline/test_rundown_writer.py`

**Context:** This is a synchronous script generator modeled on `fp_writer.py`. It builds a prompt with the Rundown voice/persona, assembles articles by theme, includes context scripts for dedup, and calls opencode-serve to generate the script.

**Step 1: Write the failing tests**

Create `pipeline/test_rundown_writer.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.rundown_writer import build_rundown_prompt, generate_rundown_script


def test_build_prompt_basic():
    prompt = build_rundown_prompt(
        themes=["Tech", "Finance"],
        articles_by_theme={
            "Tech": ["Article about tech"],
            "Finance": ["Article about finance"],
        },
        date_str="2026-03-10",
    )
    assert "2026-03-10" in prompt
    assert "Tech" in prompt
    assert "Finance" in prompt
    assert "Article about tech" in prompt
    assert "Article about finance" in prompt
    assert "The Rundown" in prompt


def test_build_prompt_with_context():
    prompt = build_rundown_prompt(
        themes=["Tech"],
        articles_by_theme={"Tech": ["New article"]},
        date_str="2026-03-10",
        context_scripts=["Yesterday's script content"],
    )
    assert "PRIOR EPISODES" in prompt
    assert "Yesterday's script content" in prompt
    assert "genuinely new material" in prompt.lower() or "NEW" in prompt


def test_build_prompt_without_context():
    prompt = build_rundown_prompt(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
        context_scripts=None,
    )
    assert "PRIOR EPISODES" not in prompt


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_script(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_123"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "Generated podcast script here"

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
    )

    assert result == "Generated podcast script here"
    mock_create.assert_called_once()
    mock_send.assert_called_once()
    mock_wait.assert_called_once_with("ses_123", timeout=120)
    mock_delete.assert_called_once_with("ses_123")


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_script_timeout_raises(
    mock_create, mock_send, mock_wait, mock_delete
):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False

    try:
        generate_rundown_script(
            themes=["Tech"],
            articles_by_theme={"Tech": ["Article"]},
            date_str="2026-03-10",
        )
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "120 seconds" in str(e)

    mock_delete.assert_called_once_with("ses_timeout")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_rundown_writer.py -v`
Expected: FAIL — `pipeline.rundown_writer` does not exist.

**Step 3: Write the implementation**

Create `pipeline/rundown_writer.py`:

```python
from __future__ import annotations

from pipeline.opencode_client import (
    create_session,
    delete_session,
    get_last_assistant_text,
    get_messages,
    send_prompt_async,
    wait_for_idle,
)


PROMPT_TEMPLATE = """\
You are generating today's episode of The Rundown, a daily podcast covering business,
technology, AI, law, media, science, and culture. Today's date is {date_str}.

Foreign policy goes to a separate podcast. Skip it entirely.

Your job is to produce a natural, conversational podcast script covering the most
important stories of the day, grouped by theme. The script will be read aloud by
a TTS engine.

You are a smart friend explaining the day's news over drinks. You are genuinely
curious about how things work and why they matter. You have opinions and you share
them, but you hold them lightly and you're honest about uncertainty. You explain
complex things clearly without talking down to the listener — they're well-read,
they just haven't had time to read everything today. You draw connections across
stories when they exist. You have fun with language. You let the material breathe
when it deserves it and move briskly when it doesn't.

Write for the ear, not the page. Use plain spoken English — no markdown, bullet
points, or special characters.

LENGTH: Aim for 800-2200 words depending on how much genuinely new material there
is. A tight 5-8 minute episode that covers three or four real developments is far
better than a 15-minute episode that rehashes yesterday. Do not pad.
{context_block}
Introduce each theme section clearly, start with a brief welcome and overview,
and end with a brief sign-off.

---

TODAY'S THEMES:
{themes_list}

---

STORIES BY THEME:

{stories_block}
"""


def build_rundown_prompt(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
) -> str:
    """Build the LLM prompt for The Rundown podcast script."""
    if context_scripts:
        context_lines = [
            "\nPRIOR EPISODES (your listeners already heard these):",
            "Treat the content below as what your audience already knows. Your job",
            "today is to tell them what is NEW.",
            "",
            "Rules for handling prior coverage:",
            "- If a running story has a material new development, cover the new",
            "  development. Do not re-explain the background — listeners already",
            "  have it. A single sentence like 'as we discussed yesterday' is enough",
            "  to orient them before delivering the update.",
            "- If a running story has NO material new development since the last",
            "  episode, skip it entirely or give it at most one sentence.",
            "- Never restate facts, figures, or analysis that appeared in a prior",
            "  episode. If you covered a story yesterday, do not repeat it today",
            "  unless the situation has materially changed.",
            "- A shorter episode built from genuinely new material is always better",
            "  than a longer episode that recycles prior coverage.\n",
        ]
        for i, script in enumerate(context_scripts, 1):
            context_lines.append(f"[Prior Episode {i}]:\n{script}\n")
        context_block = "\n".join(context_lines) + "\n"
    else:
        context_block = ""

    themes_list = "\n".join(f"- {theme}" for theme in themes)

    story_sections: list[str] = []
    for theme in themes:
        articles = articles_by_theme.get(theme, [])
        section_lines = [f"## {theme}"]
        for j, article_text in enumerate(articles, 1):
            section_lines.append(f"### Source {j}")
            section_lines.append(article_text)
        story_sections.append("\n".join(section_lines))
    stories_block = "\n\n".join(story_sections)

    return PROMPT_TEMPLATE.format(
        date_str=date_str,
        context_block=context_block,
        themes_list=themes_list,
        stories_block=stories_block,
    )


def generate_rundown_script(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
) -> str:
    """Generate a Rundown podcast script via the shared opencode server."""
    prompt = build_rundown_prompt(themes, articles_by_theme, date_str, context_scripts)

    instruction = (
        "Read the following prompt and generate the podcast briefing script. "
        "Output ONLY the script text, nothing else.\n\n" + prompt
    )

    session_id = create_session()
    try:
        send_prompt_async(session_id, instruction)

        if not wait_for_idle(session_id, timeout=120):
            raise RuntimeError("opencode session did not complete within 120 seconds")

        messages = get_messages(session_id)
        return get_last_assistant_text(messages).strip()
    finally:
        delete_session(session_id)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_rundown_writer.py -v`
Expected: All 5 tests PASS.

**Step 5: Commit**

```bash
git -c commit.gpgsign=false commit -am "feat: add rundown_writer.py — synchronous script generator for The Rundown"
```

---

### Task 4: Rewrite `_the_rundown_full_run()` and `_the_rundown_dry_run()` in `__main__.py`

**Files:**
- Modify: `pipeline/__main__.py:317-390` (rewrite both functions)

**Context:** Currently `_the_rundown_full_run()` calls `launch_things_happen_agent()` which creates an interactive agent session. Replace with synchronous pipeline: collect -> read plan -> build articles_by_theme -> generate script -> TTS -> publish. Mirror `_fp_digest_full_run()`.

**Step 1: No new test file needed** — the `__main__.py` CLI functions are integration entry points tested via the consumer tests and manual CLI runs. The consumer tests (Task 5) will cover the pipeline flow.

**Step 2: Rewrite the imports and functions**

In `pipeline/__main__.py`:

Remove the import of `launch_things_happen_agent` (line 344). The new imports needed inside the function bodies are `RundownResearchPlan` from `things_happen_editor`, `generate_rundown_script` from `rundown_writer`, and `process_things_happen_job` from `things_happen_processor`.

Replace `_the_rundown_dry_run` (lines 317-338) with:

```python
def _the_rundown_dry_run(date_str: str, lookback_override: int | None = None) -> None:
    """Run collection + script generation without touching the DB."""
    import uuid

    from pipeline.rundown_writer import generate_rundown_script
    from pipeline.things_happen_collector import collect_all_artifacts
    from pipeline.things_happen_editor import RundownResearchPlan

    run_id = str(uuid.uuid4())
    work_dir = Path(f"/tmp/the-rundown-{run_id}")
    click.echo(f"Dry run for {date_str} (no DB entry created)")

    click.echo("Collecting sources...")
    collect_all_artifacts(
        run_id,
        work_dir,
        levine_cache_dir=Path("/persist/my-podcasts/levine-cache"),
        semafor_cache_dir=Path("/persist/my-podcasts/semafor-cache"),
        zvi_cache_dir=Path("/persist/my-podcasts/zvi-cache"),
        fp_routed_dir=Path("/persist/my-podcasts/fp-routed-links"),
        lookback_days=lookback_override or 2,
    )

    plan_path = work_dir / "plan.json"
    if not plan_path.exists():
        click.echo("Error: no plan generated")
        return

    plan = RundownResearchPlan.model_validate_json(plan_path.read_text())
    click.echo(f"Themes: {', '.join(plan.themes)}")
    selected = sum(1 for d in plan.directives if d.include_in_episode)
    click.echo(f"Selected {selected} stories")

    articles_by_theme: dict[str, list[str]] = {}
    for directive in plan.directives:
        if not directive.include_in_episode:
            continue
        text = _find_rundown_article_text(directive, work_dir)
        if text:
            articles_by_theme.setdefault(directive.theme, []).append(text)

    context_scripts = []
    context_dir = work_dir / "context"
    if context_dir.exists():
        for f in sorted(context_dir.glob("*.txt"), reverse=True):
            context_scripts.append(f.read_text(encoding="utf-8"))

    click.echo("Generating script...")
    script = generate_rundown_script(
        themes=plan.themes,
        articles_by_theme=articles_by_theme,
        date_str=date_str,
        context_scripts=context_scripts,
    )

    script_file = work_dir / "script.txt"
    script_file.write_text(script, encoding="utf-8")

    click.echo(f"Dry run complete. Script saved to: {script_file}")
    click.echo(f"Work directory: {work_dir}")
```

Replace `_the_rundown_full_run` (lines 341-390) with:

```python
def _the_rundown_full_run(date_str: str, lookback_override: int | None = None) -> None:
    """Run the full pipeline: collect, generate script, TTS, publish."""
    import shutil

    from pipeline.consumer import _compute_lookback
    from pipeline.rundown_writer import generate_rundown_script
    from pipeline.things_happen_collector import collect_all_artifacts
    from pipeline.things_happen_editor import RundownResearchPlan
    from pipeline.things_happen_processor import process_things_happen_job

    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()

        job_id = store.insert_pending_the_rundown(date_str)
        if job_id is None:
            click.echo(f"The Rundown job already exists for {date_str}")
            due = store.list_due_the_rundown()
            job = next((j for j in due if j["date_str"] == date_str), None)
            if job is None:
                click.echo("Job exists but is not pending.")
                return
        else:
            click.echo(f"Created The Rundown job {job_id} for {date_str}")
            due = store.list_due_the_rundown()
            job = next((j for j in due if j["id"] == job_id), None)
            if job is None:
                click.echo("Error: job not found")
                return

        lookback = lookback_override or _compute_lookback(store, "the-rundown")
        work_dir = Path(f"/tmp/the-rundown-{job['id']}")
        click.echo("Collecting sources...")
        collect_all_artifacts(
            job["id"],
            work_dir,
            levine_cache_dir=Path("/persist/my-podcasts/levine-cache"),
            semafor_cache_dir=Path("/persist/my-podcasts/semafor-cache"),
            zvi_cache_dir=Path("/persist/my-podcasts/zvi-cache"),
            fp_routed_dir=Path("/persist/my-podcasts/fp-routed-links"),
            lookback_days=lookback,
        )

        plan_path = work_dir / "plan.json"
        if not plan_path.exists():
            click.echo("Error: no plan generated")
            return

        plan = RundownResearchPlan.model_validate_json(plan_path.read_text())
        click.echo(f"Themes: {', '.join(plan.themes)}")
        selected = sum(1 for d in plan.directives if d.include_in_episode)
        click.echo(f"Selected {selected} stories")

        articles_by_theme: dict[str, list[str]] = {}
        for directive in plan.directives:
            if not directive.include_in_episode:
                continue
            text = _find_rundown_article_text(directive, work_dir)
            if text:
                articles_by_theme.setdefault(directive.theme, []).append(text)

        context_scripts = []
        context_dir = work_dir / "context"
        if context_dir.exists():
            for f in sorted(context_dir.glob("*.txt"), reverse=True):
                context_scripts.append(f.read_text(encoding="utf-8"))

        click.echo("Generating script...")
        script = generate_rundown_script(
            themes=plan.themes,
            articles_by_theme=articles_by_theme,
            date_str=date_str,
            context_scripts=context_scripts,
        )

        script_file = work_dir / "script.txt"
        script_file.write_text(script, encoding="utf-8")

        click.echo("Running TTS...")
        process_things_happen_job(job, store, r2_client, script_path=script_file)

        persist_dir = Path("/persist/my-podcasts/scripts/the-rundown")
        persist_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(script_file, persist_dir / f"{date_str}.txt")

        click.echo(f"Published The Rundown for {date_str}")
    finally:
        store.close()
```

Also add a helper function `_find_rundown_article_text()` above the CLI commands (after imports, around line 18):

```python
def _find_rundown_article_text(directive: Any, work_dir: Path) -> str:
    """Find article text for a Rundown directive.

    Searches:
    - work_dir/articles/{nn}-{slug}.md (flat Levine articles)
    - work_dir/articles/semafor/{slug}.md
    - work_dir/articles/zvi/{slug}.md
    - work_dir/enrichment/exa/{slug}.md
    - work_dir/enrichment/rss/{slug}*.md
    """
    from pipeline.things_happen_collector import _slugify

    slug = _slugify(directive.headline)

    # Flat Levine articles
    articles_dir = work_dir / "articles"
    if articles_dir.exists():
        for match in articles_dir.glob(f"*{slug}.md"):
            if match.parent == articles_dir:  # Only top-level, not subdirs
                return match.read_text(encoding="utf-8")

    # Semafor articles
    semafor_file = work_dir / "articles" / "semafor" / f"{slug}.md"
    if semafor_file.exists():
        return semafor_file.read_text(encoding="utf-8")

    # Zvi articles
    zvi_dir = work_dir / "articles" / "zvi"
    if zvi_dir.exists():
        for match in zvi_dir.glob(f"*{slug}*.md"):
            return match.read_text(encoding="utf-8")

    # Exa enrichment
    exa_file = work_dir / "enrichment" / "exa" / f"{slug}.md"
    if exa_file.exists():
        return exa_file.read_text(encoding="utf-8")

    # RSS enrichment (Zvi AI perspectives)
    rss_dir = work_dir / "enrichment" / "rss"
    if rss_dir.exists():
        for match in rss_dir.glob(f"*{slug}*.md"):
            return match.read_text(encoding="utf-8")

    return ""
```

Add `from typing import Any` to the imports.

**Step 3: Run existing tests**

Run: `uv run pytest pipeline/ -v -k "not test_fp_collector"`
Expected: All pass (no test directly covers `__main__.py` functions).

**Step 4: Commit**

```bash
git -c commit.gpgsign=false commit -am "feat: rewrite Rundown CLI to synchronous pipeline (no agent)"
```

---

### Task 5: Simplify consumer Rundown handling

**Files:**
- Modify: `pipeline/consumer.py:1-31,272-337` (remove agent imports, simplify Rundown block)
- Modify: `pipeline/test_consumer.py` (remove stuck-agent test, update remaining tests)

**Context:** The consumer no longer needs to track agent sessions for The Rundown. Replace the agent-based block with an FP-Digest-style inline pipeline: if script exists, TTS+publish; otherwise, run the full synchronous pipeline (collect -> plan -> write -> TTS -> publish).

**Step 1: Update test file**

Remove/update these tests in `pipeline/test_consumer.py`:

1. Remove the import of `_AGENT_SESSION_TIMEOUT_SECONDS` from the import block.
2. Remove `test_consume_forever_kills_stuck_agent` entirely (agent sessions no longer exist).
3. In the remaining Rundown tests (`test_consume_forever_processes_rundown_script`, `test_consume_forever_dry_run_skips_tts`, `test_consume_forever_stops_agent_on_tts_failure`):
   - Remove all references to `stop_agent` mocking (no more agent to stop).
   - Remove `store.get_the_rundown_session_id` mock setup.
   - Remove assertions about `stopped`.
   - Remove `store.clear_the_rundown_session_id` assertions.

The tests should now only verify: script exists -> TTS called (or dry run skips), script copied to persist, cleanup called.

**Step 2: Update consumer implementation**

In `pipeline/consumer.py`:

1. Remove imports (lines 19-22):
```python
# Delete these lines:
from pipeline.things_happen_agent import (
    is_agent_running,
    stop_agent,
)
```

2. Remove the `_AGENT_SESSION_TIMEOUT_SECONDS` constant (line 31).

3. Remove `session_start_times: dict[str, float] = {}` (line 244).

4. Replace the Rundown processing block (lines 272-337) with a simpler version:

```python
        # Process any due Rundown jobs.
        try:
            due_jobs = store.list_due_the_rundown()
            for job in due_jobs:
                try:
                    work_dir = Path(f"/tmp/the-rundown-{job['id']}")
                    script_file = work_dir / "script.txt"

                    if script_file.exists():
                        # Script already generated — run TTS + publish.
                        try:
                            dry_run = os.environ.get("THE_RUNDOWN_DRY_RUN", "").strip()
                            if dry_run:
                                print(
                                    f"DRY RUN: skipping TTS for {job['id']} ({job['date_str']}). Script at: {script_file}"
                                )
                                store.mark_the_rundown_completed(job["id"])
                            else:
                                print(
                                    f"Processing Rundown job with script: {job['id']} ({job['date_str']})"
                                )
                                process_things_happen_job(
                                    job, store, r2_client, script_path=script_file
                                )
                                print(f"Completed Rundown job: {job['id']}")

                            # Copy to persistent storage
                            persist_dir = Path(
                                "/persist/my-podcasts/scripts/the-rundown"
                            )
                            persist_dir.mkdir(parents=True, exist_ok=True)
                            persist_path = persist_dir / f"{job['date_str']}.txt"
                            shutil.copy(script_file, persist_path)
                        finally:
                            _cleanup_old_work_dirs()

                    else:
                        # No script yet — run the full synchronous pipeline.
                        from pipeline.rundown_writer import generate_rundown_script
                        from pipeline.things_happen_collector import collect_all_artifacts
                        from pipeline.things_happen_editor import RundownResearchPlan

                        print(
                            f"Running Rundown collection: "
                            f"{job['id']} ({job['date_str']})"
                        )
                        rundown_lookback = _compute_lookback(store, "the-rundown")
                        collect_all_artifacts(
                            job["id"],
                            work_dir,
                            levine_cache_dir=Path("/persist/my-podcasts/levine-cache"),
                            semafor_cache_dir=Path("/persist/my-podcasts/semafor-cache"),
                            zvi_cache_dir=Path("/persist/my-podcasts/zvi-cache"),
                            fp_routed_dir=Path("/persist/my-podcasts/fp-routed-links"),
                            lookback_days=rundown_lookback,
                        )

                        plan_path = work_dir / "plan.json"
                        if not plan_path.exists():
                            print(
                                f"No plan generated for Rundown {job['id']}, skipping"
                            )
                            continue

                        plan = RundownResearchPlan.model_validate_json(
                            plan_path.read_text()
                        )

                        from pipeline.__main__ import _find_rundown_article_text

                        articles_by_theme: dict[str, list[str]] = {}
                        for directive in plan.directives:
                            if not directive.include_in_episode:
                                continue
                            text = _find_rundown_article_text(directive, work_dir)
                            if text:
                                articles_by_theme.setdefault(
                                    directive.theme, []
                                ).append(text)

                        context_scripts = []
                        context_dir = work_dir / "context"
                        if context_dir.exists():
                            for f in sorted(context_dir.glob("*.txt"), reverse=True):
                                context_scripts.append(f.read_text(encoding="utf-8"))

                        script = generate_rundown_script(
                            themes=plan.themes,
                            articles_by_theme=articles_by_theme,
                            date_str=job["date_str"],
                            context_scripts=context_scripts,
                        )
                        script_file.parent.mkdir(parents=True, exist_ok=True)
                        script_file.write_text(script, encoding="utf-8")
                        # Next loop will pick up the script and run TTS

                except Exception as exc:
                    print(f"Failed Rundown job {job['id']}: {exc}")
        except Exception as exc:
            print(f"Error checking Rundown jobs: {exc}")
```

**Step 3: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_consumer.py -v`
Expected: All remaining tests PASS.

**Step 4: Commit**

```bash
git -c commit.gpgsign=false commit -am "refactor: simplify consumer Rundown handling — remove agent session tracking"
```

---

### Task 6: Delete `things_happen_agent.py` and its tests

**Files:**
- Delete: `pipeline/things_happen_agent.py`
- Delete: `pipeline/test_things_happen_agent.py`

**Context:** The agent launcher is no longer used anywhere. All imports were removed in Tasks 4 and 5.

**Step 1: Verify no remaining imports**

Run: `grep -r "things_happen_agent" pipeline/ --include="*.py" | grep -v test_things_happen_agent | grep -v things_happen_agent.py`
Expected: No output (no remaining references).

**Step 2: Delete the files**

```bash
rm pipeline/things_happen_agent.py pipeline/test_things_happen_agent.py
```

**Step 3: Run full test suite**

Run: `uv run pytest pipeline/ -v -k "not test_fp_collector"`
Expected: All tests PASS. No import errors.

**Step 4: Commit**

```bash
git -c commit.gpgsign=false add -A && git -c commit.gpgsign=false commit -m "chore: delete things_happen_agent.py — replaced by synchronous pipeline"
```

---

### Task 7: Clean up DB session tracking

**Files:**
- Modify: `pipeline/db.py:71-78,413-434` (remove session_id column and methods)
- Modify: `pipeline/test_things_happen_db.py:99-107` (remove session_id test)

**Context:** The `opencode_session_id` column in `pending_the_rundown` and the `set/get/clear_the_rundown_session_id` methods are no longer needed. The `pending_things_happen` table also has an `opencode_session_id` column and matching methods that should be removed.

**Step 1: Update the test file**

In `pipeline/test_things_happen_db.py`:

Remove the `test_the_rundown_session_id` test (lines 99-107) entirely.

**Step 2: Run tests to verify they still pass (they should — we're removing a test)**

Run: `uv run pytest pipeline/test_things_happen_db.py -v`
Expected: All remaining tests PASS.

**Step 3: Update db.py**

1. Remove `opencode_session_id TEXT,` from the `pending_the_rundown` CREATE TABLE (line 76).

2. Remove the three session_id methods (lines 413-434):
   - `set_the_rundown_session_id`
   - `get_the_rundown_session_id`
   - `clear_the_rundown_session_id`

3. Also remove the three `things_happen` session_id methods (lines 312-333):
   - `set_things_happen_session_id`
   - `get_things_happen_session_id`
   - `clear_things_happen_session_id`

4. Remove the migration that adds `opencode_session_id` to `pending_things_happen` (lines 119-129).

Note: The column will still exist in the production DB (SQLite doesn't support DROP COLUMN easily). The schema just won't create it for new DBs, and the methods will be gone. This is fine — SQLite ignores extra columns.

**Step 4: Run full test suite**

Run: `uv run pytest pipeline/ -v -k "not test_fp_collector"`
Expected: All tests PASS.

**Step 5: Commit**

```bash
git -c commit.gpgsign=false commit -am "chore: remove session_id tracking from DB — no longer needed after agent removal"
```

---

## Post-Refactor: Verify and Run

After all 7 tasks are complete:

1. **Run full test suite**: `uv run pytest pipeline/ -v -k "not test_fp_collector"`
2. **Clean up stale Rundown job** in production DB (the one that blocked on Question tool)
3. **Run The Rundown** manually: `uv run python -m pipeline the-rundown --date 2026-03-10`
4. **Verify episode published** on the feed
5. **Restart consumer**: `sudo systemctl restart my-podcasts-consumer`
6. **Update AGENTS.md** to remove references to the agent architecture
