# Publish Script Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `publish-script` CLI command that produces a podcast episode from a pre-written script file with optional markdown show notes.

**Architecture:** New `script_processor.py` module handles text prep, TTS, upload, and episode insertion. A new `show_notes_html` column on the `episodes` table stores pre-rendered HTML for rich show notes. `feed.py` prefers `show_notes_html` when set, otherwise falls back to building from `articles_json`.

**Tech Stack:** Python, Click (CLI), `markdown` library (new dep), `ttsjoin` (TTS), `ffprobe` (duration), SQLite, Cloudflare R2.

---

### Task 1: Add `markdown` dependency

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add the dependency**

In `pyproject.toml`, add `"markdown>=3.7"` to the `dependencies` list (after `"google-genai>=1.65.0"`).

**Step 2: Install**

Run: `uv sync`

**Step 3: Verify**

Run: `uv run python -c "import markdown; print(markdown.version)"`
Expected: prints a version number like `3.7`

**Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "feat: add markdown library for show notes rendering"
```

---

### Task 2: Add `show_notes_html` to Episode dataclass + DB migration

**Files:**
- Modify: `pipeline/db.py`
- Test: `pipeline/test_script_processor.py` (create)

**Step 1: Write the failing test**

Create `pipeline/test_script_processor.py`:

```python
from __future__ import annotations

from pipeline.db import Episode, StateStore


def test_episode_show_notes_html_stored_and_retrieved(tmp_path) -> None:
    """Episodes with show_notes_html can be stored and retrieved."""
    store = StateStore(tmp_path / "test.sqlite3")

    episode = Episode(
        id="ep-html",
        title="Test Episode",
        slug="test-episode",
        pub_date="Thu, 13 Mar 2026 12:00:00 +0000",
        r2_key="episodes/deep-dives/test-episode.mp3",
        feed_slug="deep-dives",
        category="Technology",
        source_tag=None,
        preset_name="Script",
        source_url=None,
        size_bytes=5000,
        duration_seconds=120,
        summary="A test summary.",
        show_notes_html="<h2>Show Notes</h2><p>Details here.</p>",
    )
    store.insert_episode(episode)

    episodes = store.list_episodes(feed_slug="deep-dives")
    assert len(episodes) == 1
    assert episodes[0].show_notes_html == "<h2>Show Notes</h2><p>Details here.</p>"
    assert episodes[0].summary == "A test summary."

    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_script_processor.py::test_episode_show_notes_html_stored_and_retrieved -v`
Expected: FAIL - `Episode.__init__()` got unexpected keyword argument `show_notes_html`

**Step 3: Implement Episode + migration changes**

In `pipeline/db.py`:

1. Add `show_notes_html: str | None = None` to the `Episode` dataclass (after `articles_json`).

2. Add to `_migrate_schema` migrations dict:
   ```python
   "show_notes_html": "ALTER TABLE episodes ADD COLUMN show_notes_html TEXT",
   ```

3. Add `show_notes_html` to the `INSERT INTO episodes` statement (add column name and `?` placeholder, add `episode.show_notes_html` to the values tuple).

4. Add `show_notes_html` to both `SELECT` queries in `list_episodes` and to the `Episode(...)` construction in the list comprehension.

**Step 4: Run test to verify it passes**

Run: `uv run pytest pipeline/test_script_processor.py::test_episode_show_notes_html_stored_and_retrieved -v`
Expected: PASS

**Step 5: Run existing tests to verify no regressions**

Run: `uv run pytest pipeline/test_feed_show_notes.py pipeline/test_things_happen_processor.py -v`
Expected: All PASS (existing Episode constructors don't pass `show_notes_html`, which defaults to None)

**Step 6: Commit**

```bash
git add pipeline/db.py pipeline/test_script_processor.py
git commit -m "feat: add show_notes_html column to episodes table"
```

---

### Task 3: Update feed.py to use `show_notes_html`

**Files:**
- Modify: `pipeline/feed.py`
- Test: `pipeline/test_script_processor.py` (append)

**Step 1: Write the failing test**

Append to `pipeline/test_script_processor.py`:

```python
import json
from xml.etree import ElementTree as ET

from pipeline.feed import generate_feed_xml


def test_feed_uses_show_notes_html_when_set(tmp_path, monkeypatch) -> None:
    """Episodes with show_notes_html use it for content:encoded instead of articles_json."""
    monkeypatch.setenv("PODCAST_BASE_URL", "https://podcast.test")
    store = StateStore(tmp_path / "test.sqlite3")

    episode = Episode(
        id="ep-rich",
        title="Deep Dive Episode",
        slug="deep-dive-episode",
        pub_date="Thu, 13 Mar 2026 12:00:00 +0000",
        r2_key="episodes/deep-dives/deep-dive-episode.mp3",
        feed_slug="deep-dives",
        category="Technology",
        source_tag=None,
        preset_name="Script",
        source_url=None,
        size_bytes=5000,
        duration_seconds=120,
        summary="A summary for description tag.",
        show_notes_html="<h2>Full Notes</h2><p>Rich content.</p>",
    )
    store.insert_episode(episode)

    xml_bytes = generate_feed_xml(store, feed_slug="deep-dives")
    root = ET.fromstring(xml_bytes)

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    item = root.find(".//item")

    desc = item.find("description")
    assert desc is not None
    assert desc.text == "A summary for description tag."

    encoded = item.find("content:encoded", ns)
    assert encoded is not None
    assert "<h2>Full Notes</h2>" in encoded.text
    assert "<p>Rich content.</p>" in encoded.text

    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_script_processor.py::test_feed_uses_show_notes_html_when_set -v`
Expected: FAIL - `encoded` is None (current code only builds from `articles_json`)

**Step 3: Update feed.py**

In `pipeline/feed.py`, in the `generate_feed_xml` function, change the show notes section (around line 120-124) from:

```python
        show_notes_html = _build_show_notes_html(episode.summary, episode.articles_json)
        if show_notes_html:
```

to:

```python
        show_notes_html = episode.show_notes_html or _build_show_notes_html(
            episode.summary, episode.articles_json
        )
        if show_notes_html:
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest pipeline/test_script_processor.py::test_feed_uses_show_notes_html_when_set -v`
Expected: PASS

**Step 5: Run existing feed tests to verify no regressions**

Run: `uv run pytest pipeline/test_feed_show_notes.py -v`
Expected: All PASS (existing episodes have `show_notes_html=None`, so fallback to `_build_show_notes_html` still works)

**Step 6: Commit**

```bash
git add pipeline/feed.py pipeline/test_script_processor.py
git commit -m "feat: prefer show_notes_html for content:encoded in feed"
```

---

### Task 4: Script text stripping for TTS

**Files:**
- Create: `pipeline/script_processor.py`
- Test: `pipeline/test_script_processor.py` (append)

**Step 1: Write the failing tests**

Append to `pipeline/test_script_processor.py`:

```python
from pipeline.script_processor import strip_markdown_for_tts


def test_strip_markdown_headings() -> None:
    text = "# Title\n\n## ACT 1: THE DREAM\n\nSome text.\n\n### Story One\n\nMore text."
    result = strip_markdown_for_tts(text)
    assert "# " not in result
    assert "## " not in result
    assert "### " not in result
    assert "Some text." in result
    assert "More text." in result


def test_strip_markdown_bold_italic() -> None:
    text = "This is **bold** and *italic* and ***both***."
    result = strip_markdown_for_tts(text)
    assert "**" not in result
    assert result.strip() == "This is bold and italic and both."


def test_strip_horizontal_rules() -> None:
    text = "Before.\n\n---\n\nAfter."
    result = strip_markdown_for_tts(text)
    assert "---" not in result
    assert "Before." in result
    assert "After." in result


def test_strip_end_marker() -> None:
    text = "Final paragraph.\n\n*[END OF SCRIPT]*"
    result = strip_markdown_for_tts(text)
    assert "[END OF SCRIPT]" not in result
    assert "Final paragraph." in result


def test_strip_preserves_content() -> None:
    text = "Two point two billion dollars. That's the average cost."
    result = strip_markdown_for_tts(text)
    assert result.strip() == text
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_script_processor.py::test_strip_markdown_headings -v`
Expected: FAIL - `cannot import name 'strip_markdown_for_tts'`

**Step 3: Implement `strip_markdown_for_tts`**

Create `pipeline/script_processor.py`:

```python
from __future__ import annotations

import re


def strip_markdown_for_tts(text: str) -> str:
    """Strip markdown formatting from script text for TTS input.

    Removes headings, horizontal rules, bold/italic markers, and end markers.
    Preserves the actual content text.
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        # Skip horizontal rules
        if re.match(r"^-{3,}$", stripped):
            continue
        # Skip end-of-script marker
        if re.match(r"^\*?\[END OF SCRIPT\]\*?$", stripped):
            continue
        # Strip heading markers
        line = re.sub(r"^#{1,6}\s+", "", line)
        # Strip bold/italic markers (*** first, then **, then *)
        line = re.sub(r"\*{3}(.+?)\*{3}", r"\1", line)
        line = re.sub(r"\*{2}(.+?)\*{2}", r"\1", line)
        line = re.sub(r"\*(.+?)\*", r"\1", line)
        result_lines.append(line)
    return "\n".join(result_lines)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_script_processor.py -k "strip" -v`
Expected: All 5 strip tests PASS

**Step 5: Commit**

```bash
git add pipeline/script_processor.py pipeline/test_script_processor.py
git commit -m "feat: add strip_markdown_for_tts for script text preparation"
```

---

### Task 5: Show notes extraction (summary + HTML conversion)

**Files:**
- Modify: `pipeline/script_processor.py`
- Test: `pipeline/test_script_processor.py` (append)

**Step 1: Write the failing tests**

Append to `pipeline/test_script_processor.py`:

```python
from pipeline.script_processor import extract_summary, render_show_notes_html


def test_extract_summary_from_show_notes() -> None:
    md = (
        "# Show Notes\n\n"
        "## Editorial Notes\n\nStuff.\n\n"
        "## Episode Summary\n\n"
        "AI is discovering drug candidates faster than ever. "
        "This episode explores who's doing it.\n\n"
        "---\n\n"
        "## Key Numbers\n\nMore stuff."
    )
    summary = extract_summary(md)
    assert "AI is discovering drug candidates faster than ever" in summary
    assert "Key Numbers" not in summary
    assert "Editorial Notes" not in summary


def test_extract_summary_returns_none_when_missing() -> None:
    md = "# Show Notes\n\n## Key Numbers\n\nStuff."
    summary = extract_summary(md)
    assert summary is None


def test_render_show_notes_html() -> None:
    md = (
        "## Key Numbers\n\n"
        "- **$2.23 billion**: Average cost\n"
        "- **~12%**: Phase I approval rate\n\n"
        "## Companies\n\n"
        "### Nova In Silico\n"
        "- Website: [novainsilico.ai](https://www.novainsilico.ai)\n"
    )
    html = render_show_notes_html(md)
    assert "<h2>" in html
    assert "Key Numbers" in html
    assert "<strong>" in html
    assert 'href="https://www.novainsilico.ai"' in html


def test_render_show_notes_handles_tables() -> None:
    md = (
        "| Drug | What happened |\n"
        "|------|---------------|\n"
        "| Vioxx | Heart attacks |\n"
    )
    html = render_show_notes_html(md)
    assert "<table>" in html
    assert "Vioxx" in html
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_script_processor.py::test_extract_summary_from_show_notes -v`
Expected: FAIL - `cannot import name 'extract_summary'`

**Step 3: Implement `extract_summary` and `render_show_notes_html`**

Add to `pipeline/script_processor.py`:

```python
import markdown as md_lib


def extract_summary(show_notes_md: str) -> str | None:
    """Extract the Episode Summary section from show notes markdown.

    Looks for a '## Episode Summary' heading and returns the text
    between it and the next heading or horizontal rule.
    """
    lines = show_notes_md.split("\n")
    in_summary = False
    summary_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if re.match(r"^##\s+Episode Summary", stripped, re.IGNORECASE):
            in_summary = True
            continue
        if in_summary:
            # Stop at next heading or horizontal rule
            if re.match(r"^#{1,6}\s+", stripped) or re.match(r"^-{3,}$", stripped):
                break
            summary_lines.append(line)

    text = "\n".join(summary_lines).strip()
    return text if text else None


def render_show_notes_html(show_notes_md: str) -> str:
    """Convert markdown show notes to HTML for feed content:encoded."""
    return md_lib.markdown(
        show_notes_md,
        extensions=["tables", "fenced_code"],
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_script_processor.py -k "extract_summary or render_show_notes" -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add pipeline/script_processor.py pipeline/test_script_processor.py
git commit -m "feat: add show notes extraction and HTML rendering"
```

---

### Task 6: Core publish function

**Files:**
- Modify: `pipeline/script_processor.py`
- Test: `pipeline/test_script_processor.py` (append)

**Step 1: Write the failing test**

Append to `pipeline/test_script_processor.py`:

```python
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from pipeline.script_processor import publish_script


def test_publish_script_end_to_end(tmp_path, monkeypatch) -> None:
    """Full publish flow: read script, TTS, upload, insert episode, regen feed."""
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    script_file = tmp_path / "script.md"
    script_file.write_text(
        "## ACT 1\n\nHere is the script content.\n\n---\n\n*[END OF SCRIPT]*",
        encoding="utf-8",
    )

    show_notes_file = tmp_path / "notes.md"
    show_notes_file.write_text(
        "## Episode Summary\n\nA great episode about testing.\n\n---\n\n"
        "## Key Numbers\n\n- **42**: The answer.\n",
        encoding="utf-8",
    )

    # Mock ttsjoin and ffprobe
    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="180.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    # Mock feed regeneration
    feed_regen_called = []
    monkeypatch.setattr(
        "pipeline.script_processor.regenerate_and_upload_feed",
        lambda s, r: feed_regen_called.append(True),
    )

    result = publish_script(
        script_file=script_file,
        title="Test Episode",
        feed_slug="deep-dives",
        store=store,
        r2_client=r2_client,
        show_notes_file=show_notes_file,
        voice="nova",
        category="Technology",
        date_str="2026-03-13",
    )

    # Episode should be inserted
    episodes = store.list_episodes(feed_slug="deep-dives")
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.title == "Test Episode"
    assert ep.feed_slug == "deep-dives"
    assert ep.category == "Technology"
    assert ep.summary == "A great episode about testing."
    assert ep.show_notes_html is not None
    assert "<strong>" in ep.show_notes_html
    assert ep.duration_seconds == 180

    # MP3 should be uploaded
    r2_client.upload_file.assert_called_once()
    upload_key = r2_client.upload_file.call_args[0][1]
    assert upload_key.startswith("episodes/deep-dives/")
    assert "2026-03-13" in upload_key

    # Feed should be regenerated
    assert feed_regen_called

    # Result should contain key info
    assert result.r2_key == upload_key
    assert result.title == "Test Episode"

    store.close()


def test_publish_script_without_show_notes(tmp_path, monkeypatch) -> None:
    """Publish works without show notes file."""
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    script_file = tmp_path / "script.md"
    script_file.write_text("Just a plain script.", encoding="utf-8")

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
        "pipeline.script_processor.regenerate_and_upload_feed",
        lambda s, r: None,
    )

    publish_script(
        script_file=script_file,
        title="No Notes Episode",
        feed_slug="general",
        store=store,
        r2_client=r2_client,
        voice="ash",
        category="News",
        date_str="2026-03-13",
    )

    episodes = store.list_episodes(feed_slug="general")
    assert len(episodes) == 1
    assert episodes[0].summary is None
    assert episodes[0].show_notes_html is None

    store.close()


def test_publish_script_tts_receives_stripped_text(tmp_path, monkeypatch) -> None:
    """Verify TTS input file contains stripped text, not raw markdown."""
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    script_file = tmp_path / "script.md"
    script_file.write_text(
        "## Heading\n\nThis is **bold** text.\n\n---\n\n*[END OF SCRIPT]*",
        encoding="utf-8",
    )

    tts_input_text = []

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            input_file = cmd[cmd.index("--input-file") + 1]
            tts_input_text.append(Path(input_file).read_text(encoding="utf-8"))
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="30.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        "pipeline.script_processor.regenerate_and_upload_feed",
        lambda s, r: None,
    )

    publish_script(
        script_file=script_file,
        title="Strip Test",
        feed_slug="test",
        store=store,
        r2_client=r2_client,
        date_str="2026-03-13",
    )

    assert len(tts_input_text) == 1
    assert "##" not in tts_input_text[0]
    assert "**" not in tts_input_text[0]
    assert "---" not in tts_input_text[0]
    assert "[END OF SCRIPT]" not in tts_input_text[0]
    assert "This is bold text." in tts_input_text[0]

    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_script_processor.py::test_publish_script_end_to_end -v`
Expected: FAIL - `cannot import name 'publish_script'`

**Step 3: Implement `publish_script`**

Add to `pipeline/script_processor.py`:

```python
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.db import Episode
from pipeline.feed import regenerate_and_upload_feed

if TYPE_CHECKING:
    from pipeline.db import StateStore
    from pipeline.r2 import R2Client


TTS_MODEL = "tts-1-hd"
DEFAULT_VOICE = "nova"
DEFAULT_CATEGORY = "Technology"


@dataclass(frozen=True)
class PublishResult:
    """Result of publishing a script episode."""
    title: str
    r2_key: str
    feed_slug: str
    size_bytes: int
    duration_seconds: int | None


def _parse_duration_seconds(mp3_path: Path) -> int | None:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(mp3_path),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return int(round(float(result.stdout.strip())))
    except ValueError:
        return None


def _slugify(title: str) -> str:
    """Convert a title to a URL-safe slug."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def publish_script(
    *,
    script_file: Path,
    title: str,
    feed_slug: str,
    store: StateStore,
    r2_client: R2Client,
    show_notes_file: Path | None = None,
    voice: str = DEFAULT_VOICE,
    category: str = DEFAULT_CATEGORY,
    date_str: str | None = None,
) -> PublishResult:
    """Publish a podcast episode from a pre-written script file."""
    if date_str is None:
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    # Read and prepare script text for TTS
    raw_script = script_file.read_text(encoding="utf-8")
    tts_text = strip_markdown_for_tts(raw_script)

    # Process show notes if provided
    summary: str | None = None
    show_notes_html: str | None = None
    if show_notes_file is not None:
        notes_md = show_notes_file.read_text(encoding="utf-8")
        summary = extract_summary(notes_md)
        show_notes_html = render_show_notes_html(notes_md)

    # TTS
    title_slug = _slugify(title)
    episode_slug = f"{date_str}-{title_slug}"
    episode_r2_key = f"episodes/{feed_slug}/{episode_slug}.mp3"

    with tempfile.TemporaryDirectory(prefix="publish-script-") as tmp_dir:
        tmp = Path(tmp_dir)
        input_txt = tmp / f"{episode_slug}.txt"
        output_mp3 = tmp / f"{episode_slug}.mp3"
        input_txt.write_text(tts_text, encoding="utf-8")

        cmd = [
            "ttsjoin",
            "--input-file", str(input_txt),
            "--output-file", str(output_mp3),
            "--model", TTS_MODEL,
            "--voice", voice,
        ]
        subprocess.run(cmd, check=True)

        r2_client.upload_file(output_mp3, episode_r2_key, content_type="audio/mpeg")
        size_bytes = output_mp3.stat().st_size
        duration_seconds = _parse_duration_seconds(output_mp3)

    # Insert episode
    episode = Episode(
        id=str(uuid.uuid4()),
        title=title,
        slug=episode_slug,
        pub_date=format_datetime(datetime.now(tz=UTC)),
        r2_key=episode_r2_key,
        feed_slug=feed_slug,
        category=category,
        source_tag=None,
        preset_name="Script",
        source_url=None,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        summary=summary,
        show_notes_html=show_notes_html,
    )
    store.insert_episode(episode)
    regenerate_and_upload_feed(store, r2_client)

    return PublishResult(
        title=title,
        r2_key=episode_r2_key,
        feed_slug=feed_slug,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_script_processor.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add pipeline/script_processor.py pipeline/test_script_processor.py
git commit -m "feat: add publish_script function for one-off episodes"
```

---

### Task 7: Add `publish-script` CLI command

**Files:**
- Modify: `pipeline/__main__.py`

**Step 1: Add the CLI command**

Add to `pipeline/__main__.py`, after the existing imports at the top:

```python
from pipeline.script_processor import publish_script
```

Then add the command (before the `sync-sources` command):

```python
@cli.command("publish-script")
@click.option(
    "--script-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the script file (markdown or plain text).",
)
@click.option("--title", required=True, type=str, help="Episode title.")
@click.option("--feed-slug", required=True, type=str, help="Feed slug to publish on.")
@click.option(
    "--show-notes",
    "show_notes_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to markdown show notes file.",
)
@click.option("--voice", default="nova", show_default=True, type=str)
@click.option("--category", default="Technology", show_default=True, type=str)
@click.option(
    "--date",
    "date_str",
    default=None,
    type=str,
    help="Date (YYYY-MM-DD). Defaults to today.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Run TTS locally but don't publish to R2 or insert into DB.",
)
def publish_script_command(
    script_file: Path,
    title: str,
    feed_slug: str,
    show_notes_file: Path | None,
    voice: str,
    category: str,
    date_str: str | None,
    dry_run: bool,
) -> None:
    """Publish a podcast episode from a pre-written script file."""
    from datetime import UTC, datetime

    if date_str is None:
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    if dry_run:
        from pipeline.script_processor import strip_markdown_for_tts
        import subprocess
        import tempfile

        raw = script_file.read_text(encoding="utf-8")
        tts_text = strip_markdown_for_tts(raw)

        with tempfile.TemporaryDirectory(prefix="publish-script-dry-") as tmp_dir:
            tmp = Path(tmp_dir)
            input_txt = tmp / "dry-run.txt"
            output_mp3 = tmp / "dry-run.mp3"
            input_txt.write_text(tts_text, encoding="utf-8")

            cmd = [
                "ttsjoin",
                "--input-file", str(input_txt),
                "--output-file", str(output_mp3),
                "--model", "tts-1-hd",
                "--voice", voice,
            ]
            click.echo(f"Running TTS (dry run, voice={voice})...")
            subprocess.run(cmd, check=True)
            size = output_mp3.stat().st_size
            click.echo(f"MP3 generated: {output_mp3} ({size} bytes)")
            click.echo("Dry run complete. No episode published.")
        return

    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()
        click.echo(f"Publishing '{title}' to feed '{feed_slug}'...")
        result = publish_script(
            script_file=script_file,
            title=title,
            feed_slug=feed_slug,
            store=store,
            r2_client=r2_client,
            show_notes_file=show_notes_file,
            voice=voice,
            category=category,
            date_str=date_str,
        )
        click.echo(f"Published: {result.r2_key}")
        click.echo(f"Title: {result.title}")
        click.echo(f"Feed: {result.feed_slug}")
        click.echo(f"Size: {result.size_bytes} bytes")
        if result.duration_seconds is not None:
            click.echo(f"Duration: {result.duration_seconds} sec")
    finally:
        store.close()
```

**Step 2: Verify CLI registration**

Run: `uv run python -m pipeline publish-script --help`
Expected: Shows help text with all options

**Step 3: Commit**

```bash
git add pipeline/__main__.py
git commit -m "feat: add publish-script CLI command"
```

---

### Task 8: Final validation

**Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests PASS

**Step 2: Run linter**

Run: `uv run ruff check pipeline/script_processor.py pipeline/test_script_processor.py pipeline/__main__.py pipeline/db.py pipeline/feed.py`
Expected: No errors

**Step 3: Run formatter**

Run: `uv run ruff format --check pipeline/script_processor.py pipeline/test_script_processor.py pipeline/__main__.py pipeline/db.py pipeline/feed.py`
Expected: No reformatting needed (or apply fixes)

**Step 4: Run type checker**

Run: `uv run mypy pipeline/script_processor.py pipeline/db.py pipeline/feed.py`
Expected: No new errors

**Step 5: Fix any issues and commit**

```bash
git add -A
git commit -m "chore: fix lint/type issues from publish-script feature"
```
