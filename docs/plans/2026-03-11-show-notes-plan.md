# Show Notes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add article links and summaries to Rundown/FP Digest show notes in the RSS feed.

**Architecture:** Store structured data (summary text + article list JSON) in the episodes DB at publish time. Render HTML show notes at feed generation time. Writer LLM produces summary alongside script via `<summary>` tags. New `show_notes.py` module extracts article URLs from work directory files.

**Tech Stack:** Python, SQLite, xml.etree.ElementTree, Pydantic (existing models)

---

### Task 1: Add summary/articles_json to Episode dataclass and DB schema

**Files:**
- Modify: `pipeline/db.py:15-28` (Episode dataclass)
- Modify: `pipeline/db.py:91-116` (_migrate_schema)
- Modify: `pipeline/db.py:135-170` (insert_episode)
- Modify: `pipeline/db.py:172-231` (list_episodes)
- Test: `pipeline/test_show_notes_db.py` (new)

**Step 1: Write the failing test**

Create `pipeline/test_show_notes_db.py`:

```python
from __future__ import annotations

import json

from pipeline.db import Episode, StateStore


def test_episode_with_show_notes_round_trip(tmp_path) -> None:
    """Episodes with summary and articles_json survive insert + list."""
    store = StateStore(tmp_path / "test.sqlite3")

    articles = [
        {"title": "Oil's Wild Monday", "url": "https://example.com/oil", "theme": "Markets"},
        {"title": "AI Training Workers", "url": "https://nymag.com/ai", "theme": "AI & Labor"},
    ]
    episode = Episode(
        id="ep-1",
        title="2026-03-10 - The Rundown",
        slug="2026-03-10-the-rundown",
        pub_date="Mon, 10 Mar 2026 22:00:00 +0000",
        r2_key="episodes/the-rundown/2026-03-10-the-rundown.mp3",
        feed_slug="the-rundown",
        category="News",
        source_tag="the-rundown",
        preset_name="The Rundown",
        source_url=None,
        size_bytes=1000,
        duration_seconds=300,
        summary="Today we cover oil market chaos and the AI training labor story.",
        articles_json=json.dumps(articles),
    )
    store.insert_episode(episode)

    episodes = store.list_episodes(feed_slug="the-rundown")
    assert len(episodes) == 1
    got = episodes[0]
    assert got.summary == episode.summary
    assert json.loads(got.articles_json) == articles

    store.close()


def test_episode_without_show_notes_defaults_to_none(tmp_path) -> None:
    """Episodes without summary/articles_json get None (backward compat)."""
    store = StateStore(tmp_path / "test.sqlite3")

    episode = Episode(
        id="ep-2",
        title="2026-03-10 - Levine",
        slug="2026-03-10-levine",
        pub_date="Mon, 10 Mar 2026 22:00:00 +0000",
        r2_key="episodes/levine/2026-03-10-levine.mp3",
        feed_slug="levine",
        category="News",
        source_tag=None,
        preset_name="General Newsletter",
        source_url="https://bloomberg.com/opinion/...",
        size_bytes=2000,
        duration_seconds=600,
    )
    store.insert_episode(episode)

    episodes = store.list_episodes(feed_slug="levine")
    assert len(episodes) == 1
    assert episodes[0].summary is None
    assert episodes[0].articles_json is None

    store.close()


def test_migration_adds_columns_to_existing_db(tmp_path) -> None:
    """Opening StateStore on an old DB adds the new columns."""
    import sqlite3

    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE episodes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            slug TEXT NOT NULL,
            pub_date TEXT NOT NULL,
            r2_key TEXT NOT NULL,
            feed_slug TEXT NOT NULL DEFAULT 'general',
            category TEXT NOT NULL DEFAULT 'News',
            source_tag TEXT,
            preset_name TEXT NOT NULL DEFAULT 'General Newsletter',
            source_url TEXT,
            size_bytes INTEGER NOT NULL,
            duration_seconds INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE processed_emails (
            r2_key TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE pending_things_happen (
            id TEXT PRIMARY KEY,
            email_r2_key TEXT NOT NULL,
            date_str TEXT NOT NULL,
            links_json TEXT NOT NULL,
            process_after TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE pending_fp_digest (
            id TEXT PRIMARY KEY,
            date_str TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            process_after TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE pending_the_rundown (
            id TEXT PRIMARY KEY,
            date_str TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            process_after TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.close()

    # Opening StateStore should add the new columns via migration
    store = StateStore(db_path)
    cols = {
        row[1]
        for row in store._conn.execute("PRAGMA table_info(episodes)").fetchall()
    }
    assert "summary" in cols
    assert "articles_json" in cols
    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_show_notes_db.py -v`
Expected: FAIL — Episode dataclass doesn't accept summary/articles_json.

**Step 3: Implement the changes**

In `pipeline/db.py`:

Add two fields to the `Episode` dataclass (after `duration_seconds`):
```python
    summary: str | None = None
    articles_json: str | None = None
```

Add migration entries in `_migrate_schema`:
```python
        migrations = {
            "feed_slug": feed_slug_ddl,
            "category": category_ddl,
            "source_tag": "ALTER TABLE episodes ADD COLUMN source_tag TEXT",
            "preset_name": preset_name_ddl,
            "source_url": "ALTER TABLE episodes ADD COLUMN source_url TEXT",
            "summary": "ALTER TABLE episodes ADD COLUMN summary TEXT",
            "articles_json": "ALTER TABLE episodes ADD COLUMN articles_json TEXT",
        }
```

Update `insert_episode` to include the new columns in the INSERT statement — add `summary` and `articles_json` to both the column list and the VALUES placeholders, and add `episode.summary` and `episode.articles_json` to the tuple.

Update `list_episodes` — add `summary` and `articles_json` to both SELECT queries and to the `Episode()` constructor in the list comprehension.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_show_notes_db.py -v`
Expected: All 3 PASS.

Run: `uv run pytest pipeline/ -v`
Expected: All existing tests still pass (Episode defaults make them backward-compatible).

**Step 5: Commit**

```bash
git add pipeline/db.py pipeline/test_show_notes_db.py
git commit --no-gpg-sign -m "feat: add summary and articles_json columns to Episode schema"
```

---

### Task 2: Create show_notes.py — extract articles from work directory

**Files:**
- Create: `pipeline/show_notes.py`
- Test: `pipeline/test_show_notes.py` (new)

**Step 1: Write the failing tests**

Create `pipeline/test_show_notes.py`:

```python
from __future__ import annotations

import json

from pipeline.show_notes import extract_show_notes_articles


def test_extract_rundown_articles(tmp_path) -> None:
    """Extract articles from a Rundown work directory."""
    # Set up a minimal work dir with plan.json and article files
    plan = {
        "themes": ["Markets", "AI & Labor"],
        "directives": [
            {
                "headline": "Oil's Wild Monday",
                "source": "levine",
                "priority": 1,
                "theme": "Markets",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "is_ai": False,
                "ai_query": "",
                "include_in_episode": True,
            },
            {
                "headline": "AI Training Workers",
                "source": "levine",
                "priority": 2,
                "theme": "AI & Labor",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "is_ai": True,
                "ai_query": "AI training data workers",
                "include_in_episode": True,
            },
            {
                "headline": "Skipped Story",
                "source": "levine",
                "priority": 3,
                "theme": "Markets",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": True,
                "fp_query": "",
                "is_ai": False,
                "ai_query": "",
                "include_in_episode": False,
            },
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    (articles_dir / "00-oil-s-wild-monday.md").write_text(
        "# Oil's Wild Monday\n\nURL: https://example.com/oil\n\nArticle text."
    )
    (articles_dir / "01-ai-training-workers.md").write_text(
        "# AI Training Workers\n\nURL: https://nymag.com/ai\n\nArticle text."
    )

    result = extract_show_notes_articles(tmp_path)

    assert len(result) == 2
    assert result[0]["title"] == "Oil's Wild Monday"
    assert result[0]["url"] == "https://example.com/oil"
    assert result[0]["theme"] == "Markets"
    assert result[1]["title"] == "AI Training Workers"
    assert result[1]["url"] == "https://nymag.com/ai"
    assert result[1]["theme"] == "AI & Labor"


def test_extract_fp_articles(tmp_path) -> None:
    """Extract articles from an FP Digest work directory."""
    plan = {
        "themes": ["Iran War"],
        "directives": [
            {
                "headline": "US Strikes Iran Base",
                "source": "homepage/iran",
                "priority": 1,
                "theme": "Iran War",
                "needs_exa": False,
                "exa_query": "",
                "include_in_episode": True,
            },
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    homepage_dir = tmp_path / "articles" / "homepage" / "iran"
    homepage_dir.mkdir(parents=True)
    (homepage_dir / "us-strikes-iran-base.md").write_text(
        "# US Strikes Iran Base\n\nURL: https://antiwar.com/iran\nRegion: Iran\n\nText."
    )

    result = extract_show_notes_articles(tmp_path)

    assert len(result) == 1
    assert result[0]["title"] == "US Strikes Iran Base"
    assert result[0]["url"] == "https://antiwar.com/iran"
    assert result[0]["theme"] == "Iran War"


def test_extract_article_missing_url(tmp_path) -> None:
    """Articles with no URL line get url=None."""
    plan = {
        "themes": ["Tech"],
        "directives": [
            {
                "headline": "Some Story",
                "source": "levine",
                "priority": 1,
                "theme": "Tech",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "is_ai": False,
                "ai_query": "",
                "include_in_episode": True,
            },
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    (articles_dir / "00-some-story.md").write_text(
        "# Some Story\n\nNo URL line here.\n\nArticle text."
    )

    result = extract_show_notes_articles(tmp_path)

    assert len(result) == 1
    assert result[0]["title"] == "Some Story"
    assert result[0]["url"] is None


def test_extract_no_plan_returns_empty(tmp_path) -> None:
    """If plan.json is missing, return empty list."""
    result = extract_show_notes_articles(tmp_path)
    assert result == []


def test_extract_orders_by_theme_then_priority(tmp_path) -> None:
    """Articles are ordered by theme order from plan, then by priority."""
    plan = {
        "themes": ["B Theme", "A Theme"],
        "directives": [
            {
                "headline": "B2",
                "source": "levine",
                "priority": 2,
                "theme": "B Theme",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "is_ai": False,
                "ai_query": "",
                "include_in_episode": True,
            },
            {
                "headline": "A1",
                "source": "levine",
                "priority": 1,
                "theme": "A Theme",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "is_ai": False,
                "ai_query": "",
                "include_in_episode": True,
            },
            {
                "headline": "B1",
                "source": "levine",
                "priority": 1,
                "theme": "B Theme",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "is_ai": False,
                "ai_query": "",
                "include_in_episode": True,
            },
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    (articles_dir / "00-b2.md").write_text("# B2\n\nURL: https://b2.com\n\nText.")
    (articles_dir / "01-a1.md").write_text("# A1\n\nURL: https://a1.com\n\nText.")
    (articles_dir / "02-b1.md").write_text("# B1\n\nURL: https://b1.com\n\nText.")

    result = extract_show_notes_articles(tmp_path)

    titles = [r["title"] for r in result]
    # B Theme comes first (index 0 in themes), then A Theme
    # Within B Theme, priority 1 (B1) before priority 2 (B2)
    assert titles == ["B1", "B2", "A1"]
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_show_notes.py -v`
Expected: FAIL — module `pipeline.show_notes` does not exist.

**Step 3: Implement**

Create `pipeline/show_notes.py`:

```python
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Create a safe filename slug from a headline."""
    safe = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:50]


def _extract_url_from_article(path: Path) -> str | None:
    """Parse the URL: line from an article markdown file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("URL:"):
            url = stripped[4:].strip()
            return url if url else None
    return None


def _find_article_file(headline: str, source: str, work_dir: Path) -> Path | None:
    """Find the article file matching a directive headline.

    Uses the same search logic as the consumer's _find_article_text and
    __main__._find_rundown_article_text functions.
    """
    slug = _slugify(headline)

    # Flat Levine-style articles (e.g. "00-headline.md")
    articles_dir = work_dir / "articles"
    if articles_dir.exists():
        for match in articles_dir.glob(f"*{slug}.md"):
            if match.parent == articles_dir:
                return match

    # Semafor articles
    semafor_file = articles_dir / "semafor" / f"{slug}.md"
    if semafor_file.exists():
        return semafor_file

    # Zvi articles (date-prefixed)
    zvi_dir = articles_dir / "zvi"
    if zvi_dir.exists():
        for match in zvi_dir.glob(f"*{slug}*.md"):
            return match

    # FP homepage articles (nested under region)
    homepage_dir = articles_dir / "homepage"
    if homepage_dir.exists():
        for match in homepage_dir.rglob(f"{slug}.md"):
            return match

    # FP RSS articles (nested under source)
    rss_dir = articles_dir / "rss"
    if rss_dir.exists():
        for match in rss_dir.rglob(f"{slug}.md"):
            return match

    # Routed articles
    routed_file = articles_dir / "routed" / f"{slug}.md"
    if routed_file.exists():
        return routed_file

    # Exa enrichment
    exa_file = work_dir / "enrichment" / "exa" / f"{slug}.md"
    if exa_file.exists():
        return exa_file

    return None


def extract_show_notes_articles(work_dir: Path) -> list[dict]:
    """Extract article titles, URLs, and themes from a work directory.

    Reads plan.json for themes and directives, then finds URLs from
    the corresponding article files. Returns a list of dicts:
    [{"title": str, "url": str | None, "theme": str}, ...]

    Ordered by theme order from the plan, then by priority within theme.
    Returns empty list if plan.json is missing or unparseable.
    """
    plan_path = work_dir / "plan.json"
    if not plan_path.exists():
        logger.warning("No plan.json found in %s", work_dir)
        return []

    try:
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read plan.json: %s", exc)
        return []

    themes: list[str] = plan_data.get("themes", [])
    directives: list[dict] = plan_data.get("directives", [])

    # Build theme ordering map
    theme_order = {theme: i for i, theme in enumerate(themes)}

    # Collect included directives with their URLs
    results: list[dict] = []
    for directive in directives:
        if not directive.get("include_in_episode", False):
            continue

        headline = directive.get("headline", "")
        source = directive.get("source", "")
        theme = directive.get("theme", "")
        priority = directive.get("priority", 99)

        article_file = _find_article_file(headline, source, work_dir)
        url = _extract_url_from_article(article_file) if article_file else None

        results.append({
            "title": headline,
            "url": url,
            "theme": theme,
            "_theme_order": theme_order.get(theme, 999),
            "_priority": priority,
        })

    # Sort by theme order, then priority
    results.sort(key=lambda r: (r["_theme_order"], r["_priority"]))

    # Strip internal sort keys
    return [{"title": r["title"], "url": r["url"], "theme": r["theme"]} for r in results]
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_show_notes.py -v`
Expected: All 5 PASS.

**Step 5: Commit**

```bash
git add pipeline/show_notes.py pipeline/test_show_notes.py
git commit --no-gpg-sign -m "feat: add show_notes module for extracting article URLs from work dir"
```

---

### Task 3: Modify writers to produce summary alongside script

**Files:**
- Modify: `pipeline/rundown_writer.py:1-147`
- Modify: `pipeline/fp_writer.py:1-124`
- Modify: `pipeline/test_rundown_writer.py`
- Modify: `pipeline/test_fp_writer.py`

**Step 1: Write the failing tests**

Add to `pipeline/test_rundown_writer.py`:

```python
from pipeline.rundown_writer import WriterOutput, parse_summary


def test_parse_summary_extracts_tags():
    text = "<summary>A brief summary.</summary>\n\nHey, welcome to The Rundown."
    result = parse_summary(text)
    assert result.summary == "A brief summary."
    assert result.script == "Hey, welcome to The Rundown."


def test_parse_summary_no_tags():
    text = "Hey, welcome to The Rundown."
    result = parse_summary(text)
    assert result.summary == ""
    assert result.script == "Hey, welcome to The Rundown."


def test_parse_summary_multiline():
    text = "<summary>\nLine one.\nLine two.\n</summary>\n\nThe script."
    result = parse_summary(text)
    assert result.summary == "Line one.\nLine two."
    assert result.script == "The script."


def test_generate_rundown_returns_writer_output(
    monkeypatch,
):
    """generate_rundown_script now returns WriterOutput with summary."""
    from unittest.mock import patch

    with (
        patch("pipeline.rundown_writer.create_session", return_value="ses_wout"),
        patch("pipeline.rundown_writer.send_prompt_async"),
        patch("pipeline.rundown_writer.wait_for_idle", return_value=True),
        patch(
            "pipeline.rundown_writer.get_messages",
            return_value=[{"role": "assistant", "parts": []}],
        ),
        patch(
            "pipeline.rundown_writer.get_last_assistant_text",
            return_value="<summary>Today's summary.</summary>\n\nThe script text.",
        ),
        patch("pipeline.rundown_writer.delete_session"),
    ):
        result = generate_rundown_script(
            themes=["Tech"],
            articles_by_theme={"Tech": ["Article"]},
            date_str="2026-03-10",
        )
        assert isinstance(result, WriterOutput)
        assert result.summary == "Today's summary."
        assert result.script == "The script text."
```

Add to `pipeline/test_fp_writer.py`:

```python
from pipeline.fp_writer import WriterOutput as FPWriterOutput


def test_generate_fp_returns_writer_output(monkeypatch):
    """generate_fp_script now returns WriterOutput with summary."""
    monkeypatch.setattr("pipeline.fp_writer.create_session", lambda directory=None: "sess-fp-wo")
    monkeypatch.setattr("pipeline.fp_writer.send_prompt_async", lambda sid, text: None)
    monkeypatch.setattr("pipeline.fp_writer.wait_for_idle", lambda sid, timeout=120: True)
    monkeypatch.setattr(
        "pipeline.fp_writer.get_messages",
        lambda sid: [{"role": "assistant", "parts": [{"type": "text", "text": "x"}]}],
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.get_last_assistant_text",
        return_value="<summary>FP summary.</summary>\n\nThe FP script.",
    )
    monkeypatch.setattr("pipeline.fp_writer.delete_session", lambda sid: None)

    result = generate_fp_script(
        themes=["Iran"],
        articles_by_theme={"Iran": ["Article"]},
        date_str="2026-03-06",
    )
    assert isinstance(result, FPWriterOutput)
    assert result.summary == "FP summary."
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_rundown_writer.py pipeline/test_fp_writer.py -v`
Expected: FAIL — `WriterOutput` and `parse_summary` don't exist; return type is str.

**Step 3: Implement**

In `pipeline/rundown_writer.py`:

Add at top of file (after imports):
```python
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WriterOutput:
    script: str
    summary: str


def parse_summary(text: str) -> WriterOutput:
    """Extract <summary>...</summary> block from writer output.

    Returns WriterOutput with summary and the remaining script text.
    If no summary tags found, summary is empty string.
    """
    match = re.search(r"<summary>\s*(.*?)\s*</summary>", text, re.DOTALL)
    if match:
        summary = match.group(1).strip()
        script = text[: match.start()] + text[match.end() :]
        script = script.strip()
        return WriterOutput(script=script, summary=summary)
    return WriterOutput(script=text, summary="")
```

Update the prompt instruction in `generate_rundown_script` (line 132-134) to:
```python
    instruction = (
        "Read the following prompt and generate the podcast briefing script. "
        "First, write a 2-3 sentence summary of today's episode wrapped in "
        "<summary>...</summary> tags. Then write the full script text.\n\n" + prompt
    )
```

Update the return statement (line 145) to:
```python
        raw = _strip_preamble(get_last_assistant_text(messages).strip())
        return parse_summary(raw)
```

In `pipeline/fp_writer.py` — same changes:

Add the same `WriterOutput` and `parse_summary` imports from rundown_writer (or duplicate — keep it simple, import from rundown_writer):
```python
from pipeline.rundown_writer import WriterOutput, parse_summary
```

Update the instruction (line 109-111):
```python
    instruction = (
        "Read the following prompt and generate the podcast briefing script. "
        "First, write a 2-3 sentence summary of today's episode wrapped in "
        "<summary>...</summary> tags. Then write the full script text.\n\n" + prompt
    )
```

Update the return (line 122):
```python
        raw = get_last_assistant_text(messages).strip()
        return parse_summary(raw)
```

**Step 4: Fix existing tests that expect str return type**

The existing `test_generate_script` in `test_rundown_writer.py` asserts `result == "Generated podcast script here"`. Update it to check `result.script` instead. Same for `test_generate_script_strips_preamble`. Same for `test_generate_fp_script` in `test_fp_writer.py`.

**Step 5: Run tests**

Run: `uv run pytest pipeline/test_rundown_writer.py pipeline/test_fp_writer.py -v`
Expected: All PASS.

**Step 6: Commit**

```bash
git add pipeline/rundown_writer.py pipeline/fp_writer.py pipeline/test_rundown_writer.py pipeline/test_fp_writer.py
git commit --no-gpg-sign -m "feat: writers return WriterOutput with summary extracted from <summary> tags"
```

---

### Task 4: Update processors to pass show notes data to insert_episode

**Files:**
- Modify: `pipeline/things_happen_processor.py:50-121`
- Modify: `pipeline/fp_processor.py:46-111`
- Modify: `pipeline/consumer.py:353-361` (Rundown writer call)
- Modify: `pipeline/consumer.py:448-455` (FP writer call)
- Modify: `pipeline/test_things_happen_processor.py`
- Modify: `pipeline/test_fp_processor.py`

**Step 1: Write the failing tests**

Add to `pipeline/test_things_happen_processor.py` a new test:

```python
def test_process_things_happen_with_show_notes(tmp_path, monkeypatch) -> None:
    """When work_dir has plan.json and articles, show notes are stored."""
    import json
    import subprocess

    from pipeline.db import StateStore
    from pipeline.things_happen_processor import process_things_happen_job

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_things_happen
           (id, email_r2_key, date_str, links_json, process_after, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("job-sn", "inbox/raw/abc.eml", "2026-03-10", "[]", past, "pending"),
    )
    store._conn.commit()

    # Set up work_dir with plan.json and article files
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    plan = {
        "themes": ["Markets"],
        "directives": [
            {
                "headline": "Oil Prices Spike",
                "source": "levine",
                "priority": 1,
                "theme": "Markets",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "is_ai": False,
                "ai_query": "",
                "include_in_episode": True,
            }
        ],
    }
    (work_dir / "plan.json").write_text(json.dumps(plan))
    articles_dir = work_dir / "articles"
    articles_dir.mkdir()
    (articles_dir / "00-oil-prices-spike.md").write_text(
        "# Oil Prices Spike\n\nURL: https://example.com/oil\n\nText."
    )

    script_file = work_dir / "script.txt"
    script_file.write_text("The briefing script.", encoding="utf-8")

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="60.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        "pipeline.things_happen_processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )

    job = store.list_due_things_happen()[0]
    process_things_happen_job(
        job, store, r2_client, script_path=script_file,
        work_dir=work_dir, summary="Today's summary.",
    )

    episodes = store.list_episodes(feed_slug="the-rundown")
    assert len(episodes) == 1
    assert episodes[0].summary == "Today's summary."
    articles = json.loads(episodes[0].articles_json)
    assert len(articles) == 1
    assert articles[0]["url"] == "https://example.com/oil"

    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_processor.py::test_process_things_happen_with_show_notes -v`
Expected: FAIL — processor doesn't accept `work_dir` or `summary` params.

**Step 3: Implement processor changes**

In `pipeline/things_happen_processor.py`:

Add import:
```python
from pipeline.show_notes import extract_show_notes_articles
```

Add `work_dir` and `summary` parameters to `process_things_happen_job`:
```python
def process_things_happen_job(
    job: dict,
    store: StateStore,
    r2_client: R2Client,
    script_path: Path | None = None,
    work_dir: Path | None = None,
    summary: str | None = None,
) -> None:
```

Before the Episode constructor (after step 4, TTS), extract articles:
```python
    # Extract show notes articles from work directory
    articles_json_str: str | None = None
    if work_dir is not None:
        articles = extract_show_notes_articles(work_dir)
        if articles:
            articles_json_str = json.dumps(articles)
```

Add `summary` and `articles_json` to the Episode constructor:
```python
        summary=summary,
        articles_json=articles_json_str,
```

In `pipeline/fp_processor.py` — same pattern:

Add import, add `work_dir` and `summary` params, extract articles, pass to Episode.

**Step 4: Update consumer.py to pass work_dir and summary**

In `consumer.py`, the Rundown writer call (around line 353) returns `WriterOutput` now. Update:

```python
                        writer_output = generate_rundown_script(...)
                        script_file.parent.mkdir(parents=True, exist_ok=True)
                        script_file.write_text(writer_output.script, encoding="utf-8")
                        # Save summary for the processor
                        summary_file = work_dir / "summary.txt"
                        summary_file.write_text(writer_output.summary, encoding="utf-8")
```

At the processor call site (line 284), pass work_dir and load summary:
```python
                                summary_text = None
                                summary_path = work_dir / "summary.txt"
                                if summary_path.exists():
                                    summary_text = summary_path.read_text(encoding="utf-8")
                                process_things_happen_job(
                                    job, store, r2_client, script_path=script_file,
                                    work_dir=work_dir, summary=summary_text,
                                )
```

Same pattern for FP Digest (lines 448 and 390).

**Step 5: Run all tests**

Run: `uv run pytest pipeline/ -v`
Expected: All PASS.

**Step 6: Commit**

```bash
git add pipeline/things_happen_processor.py pipeline/fp_processor.py pipeline/consumer.py pipeline/test_things_happen_processor.py pipeline/test_fp_processor.py
git commit --no-gpg-sign -m "feat: processors extract show notes from work dir and store in episode"
```

---

### Task 5: Update feed.py to render HTML show notes

**Files:**
- Modify: `pipeline/feed.py`
- Create: `pipeline/test_feed_show_notes.py`

**Step 1: Write the failing tests**

Create `pipeline/test_feed_show_notes.py`:

```python
from __future__ import annotations

import json
from xml.etree import ElementTree as ET

from pipeline.db import Episode, StateStore
from pipeline.feed import generate_feed_xml


def test_feed_includes_show_notes(tmp_path, monkeypatch) -> None:
    """Episodes with summary and articles_json produce description and content:encoded."""
    monkeypatch.setenv("PODCAST_BASE_URL", "https://podcast.test")
    store = StateStore(tmp_path / "test.sqlite3")

    articles = [
        {"title": "Oil's Wild Monday", "url": "https://example.com/oil", "theme": "Markets"},
        {"title": "AI Training", "url": "https://nymag.com/ai", "theme": "AI & Labor"},
        {"title": "No Link Story", "url": None, "theme": "AI & Labor"},
    ]
    episode = Episode(
        id="ep-show",
        title="2026-03-10 - The Rundown",
        slug="2026-03-10-the-rundown",
        pub_date="Mon, 10 Mar 2026 22:00:00 +0000",
        r2_key="episodes/the-rundown/2026-03-10-the-rundown.mp3",
        feed_slug="the-rundown",
        category="News",
        source_tag="the-rundown",
        preset_name="The Rundown",
        source_url=None,
        size_bytes=1000,
        duration_seconds=300,
        summary="Today we cover oil chaos and AI labor.",
        articles_json=json.dumps(articles),
    )
    store.insert_episode(episode)

    xml_bytes = generate_feed_xml(store, feed_slug="the-rundown")
    root = ET.fromstring(xml_bytes)

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    item = root.find(".//item")

    # description should be plain-text summary
    desc = item.find("description")
    assert desc is not None
    assert desc.text == "Today we cover oil chaos and AI labor."

    # content:encoded should have HTML with links
    encoded = item.find("content:encoded", ns)
    assert encoded is not None
    html = encoded.text
    assert "<p>Today we cover oil chaos and AI labor.</p>" in html
    assert '<a href="https://example.com/oil">Oil\'s Wild Monday</a>' in html
    assert '<a href="https://nymag.com/ai">AI Training</a>' in html
    assert "No Link Story" in html  # present but without link
    assert "Markets" in html
    assert "AI &amp; Labor" in html or "AI & Labor" in html

    store.close()


def test_feed_without_show_notes_unchanged(tmp_path, monkeypatch) -> None:
    """Episodes without show notes behave as before."""
    monkeypatch.setenv("PODCAST_BASE_URL", "https://podcast.test")
    store = StateStore(tmp_path / "test.sqlite3")

    episode = Episode(
        id="ep-no-sn",
        title="2026-03-10 - Levine",
        slug="2026-03-10-levine",
        pub_date="Mon, 10 Mar 2026 22:00:00 +0000",
        r2_key="episodes/levine/2026-03-10-levine.mp3",
        feed_slug="levine",
        category="News",
        source_tag=None,
        preset_name="General Newsletter",
        source_url="https://bloomberg.com/opinion/xyz",
        size_bytes=2000,
        duration_seconds=600,
    )
    store.insert_episode(episode)

    xml_bytes = generate_feed_xml(store, feed_slug="levine")
    root = ET.fromstring(xml_bytes)

    item = root.find(".//item")
    desc = item.find("description")
    assert desc is not None
    assert desc.text == "https://bloomberg.com/opinion/xyz"

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    encoded = item.find("content:encoded", ns)
    assert encoded is None

    store.close()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_feed_show_notes.py -v`
Expected: FAIL — no `content:encoded` element, no summary in description.

**Step 3: Implement**

In `pipeline/feed.py`:

Add import:
```python
import json
```

Add the `content` namespace to the `rss` element attributes (line 48-53):
```python
    rss = ET.Element(
        "rss",
        attrib={
            "version": "2.0",
            "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
            "xmlns:content": "http://purl.org/rss/1.0/modules/content/",
        },
    )
```

Add a helper function to build the HTML:
```python
def _build_show_notes_html(summary: str | None, articles_json: str | None) -> str | None:
    """Build HTML show notes from summary and articles list."""
    if not summary and not articles_json:
        return None

    parts: list[str] = []
    if summary:
        parts.append(f"<p>{summary}</p>")

    if articles_json:
        try:
            articles = json.loads(articles_json)
        except (json.JSONDecodeError, TypeError):
            articles = []

        if articles:
            current_theme = None
            for article in articles:
                theme = article.get("theme", "")
                if theme != current_theme:
                    if current_theme is not None:
                        parts.append("</ul>")
                    parts.append(f"<h3>{theme}</h3>")
                    parts.append("<ul>")
                    current_theme = theme

                title = article.get("title", "")
                url = article.get("url")
                if url:
                    parts.append(f'<li><a href="{url}">{title}</a></li>')
                else:
                    parts.append(f"<li>{title}</li>")

            if current_theme is not None:
                parts.append("</ul>")

    return "\n".join(parts)
```

Update the episode item loop (inside `for episode in episodes:`, after the existing `source_url` block):
```python
        if episode.source_url:
            ET.SubElement(item, "link").text = episode.source_url
            ET.SubElement(item, "description").text = episode.source_url
        elif episode.summary:
            ET.SubElement(item, "description").text = episode.summary

        show_notes_html = _build_show_notes_html(episode.summary, episode.articles_json)
        if show_notes_html:
            ET.SubElement(item, "content:encoded").text = show_notes_html
```

Note: `ET.SubElement` with a namespaced tag like `content:encoded` requires the namespace to be registered. Add at module level:
```python
ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_feed_show_notes.py -v`
Expected: All PASS.

Run: `uv run pytest pipeline/ -v`
Expected: All tests PASS.

**Step 5: Commit**

```bash
git add pipeline/feed.py pipeline/test_feed_show_notes.py
git commit --no-gpg-sign -m "feat: render show notes HTML in RSS feed from structured episode data"
```

---

### Task 6: Full integration test and final verification

**Files:**
- Test: `pipeline/test_show_notes_integration.py` (new)

**Step 1: Write an integration test**

Create `pipeline/test_show_notes_integration.py`:

```python
from __future__ import annotations

import json
from xml.etree import ElementTree as ET

from pipeline.db import Episode, StateStore
from pipeline.feed import generate_feed_xml
from pipeline.show_notes import extract_show_notes_articles


def test_show_notes_end_to_end(tmp_path, monkeypatch) -> None:
    """Full flow: extract articles from work dir, store in DB, render in feed."""
    monkeypatch.setenv("PODCAST_BASE_URL", "https://podcast.test")

    # 1. Create work dir with plan and articles
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    plan = {
        "themes": ["Markets", "Tech"],
        "directives": [
            {
                "headline": "Oil Crash",
                "source": "levine",
                "priority": 1,
                "theme": "Markets",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "is_ai": False,
                "ai_query": "",
                "include_in_episode": True,
            },
            {
                "headline": "New Chip Design",
                "source": "semafor",
                "priority": 1,
                "theme": "Tech",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "is_ai": False,
                "ai_query": "",
                "include_in_episode": True,
            },
        ],
    }
    (work_dir / "plan.json").write_text(json.dumps(plan))

    articles_dir = work_dir / "articles"
    articles_dir.mkdir()
    (articles_dir / "00-oil-crash.md").write_text(
        "# Oil Crash\n\nURL: https://bloomberg.com/oil\n\nOil text."
    )
    semafor_dir = articles_dir / "semafor"
    semafor_dir.mkdir()
    (semafor_dir / "new-chip-design.md").write_text(
        "# New Chip Design\n\nURL: https://semafor.com/chip\n\nChip text."
    )

    # 2. Extract articles
    articles = extract_show_notes_articles(work_dir)
    assert len(articles) == 2

    # 3. Store in DB
    store = StateStore(tmp_path / "test.sqlite3")
    episode = Episode(
        id="ep-int",
        title="2026-03-10 - The Rundown",
        slug="2026-03-10-the-rundown",
        pub_date="Mon, 10 Mar 2026 22:00:00 +0000",
        r2_key="episodes/the-rundown/2026-03-10-the-rundown.mp3",
        feed_slug="the-rundown",
        category="News",
        source_tag="the-rundown",
        preset_name="The Rundown",
        source_url=None,
        size_bytes=1000,
        duration_seconds=300,
        summary="Markets and tech stories today.",
        articles_json=json.dumps(articles),
    )
    store.insert_episode(episode)

    # 4. Generate feed and verify
    xml_bytes = generate_feed_xml(store, feed_slug="the-rundown")
    root = ET.fromstring(xml_bytes)

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    item = root.find(".//item")
    html = item.find("content:encoded", ns).text

    assert "Markets" in html
    assert "Tech" in html
    assert "https://bloomberg.com/oil" in html
    assert "https://semafor.com/chip" in html
    assert item.find("description").text == "Markets and tech stories today."

    store.close()
```

**Step 2: Run integration test**

Run: `uv run pytest pipeline/test_show_notes_integration.py -v`
Expected: PASS.

**Step 3: Run full test suite**

Run: `uv run pytest pipeline/ -v`
Expected: All PASS.

**Step 4: Commit**

```bash
git add pipeline/test_show_notes_integration.py
git commit --no-gpg-sign -m "test: add end-to-end integration test for show notes"
```

**Step 5: Run final checks**

```bash
uv run pytest pipeline/ -v
```
Expected: All PASS, no regressions.
