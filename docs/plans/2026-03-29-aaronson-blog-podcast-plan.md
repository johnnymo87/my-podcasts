# Aaronson Blog Podcast Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add RSS-polled blog-to-podcast pipeline for Scott Aaronson's Shtetl-Optimized, producing one episode per blog post.

**Architecture:** A blog poller runs inside the consumer loop every 6 hours, fetches the RSS feed, detects new posts by checking a `processed_blog_posts` DB table, adapts HTML content for audio via Gemini Flash, then runs TTS and publishes. A CLI command provides manual triggering.

**Tech Stack:** Python, feedparser (already a dependency), google-genai (Gemini Flash, already used), ttsjoin (TTS), SQLite, Click CLI

---

### Task 1: Blog Source Definition

**Files:**
- Create: `pipeline/blog_sources.py`
- Test: `pipeline/test_blog_poller.py`

**Step 1: Write the test for blog source definition**

```python
# pipeline/test_blog_poller.py
from pipeline.blog_sources import BLOG_SOURCES, BlogSource


def test_blog_sources_has_aaronson() -> None:
    assert len(BLOG_SOURCES) >= 1
    aaronson = [s for s in BLOG_SOURCES if s.feed_slug == "aaronson"]
    assert len(aaronson) == 1
    src = aaronson[0]
    assert src.feed_url == "https://scottaaronson.blog/?feed=rss2"
    assert src.tts_voice == "fable"
    assert src.category == "Technology"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_blog_poller.py::test_blog_sources_has_aaronson -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Write the implementation**

```python
# pipeline/blog_sources.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BlogSource:
    name: str
    feed_url: str
    feed_slug: str
    tts_voice: str
    category: str


BLOG_SOURCES: tuple[BlogSource, ...] = (
    BlogSource(
        name="Scott Aaronson - Shtetl-Optimized",
        feed_url="https://scottaaronson.blog/?feed=rss2",
        feed_slug="aaronson",
        tts_voice="fable",
        category="Technology",
    ),
)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest pipeline/test_blog_poller.py::test_blog_sources_has_aaronson -v`
Expected: PASS

**Step 5: Commit**

```bash
git add pipeline/blog_sources.py pipeline/test_blog_poller.py
git commit -m "feat: add BlogSource definition for Aaronson blog"
```

---

### Task 2: Add Preset and DB Table

**Files:**
- Modify: `pipeline/presets.py` (add aaronson preset after line 56)
- Modify: `pipeline/db.py` (add `processed_blog_posts` table to SCHEMA, add methods)
- Test: `pipeline/test_blog_poller.py` (add DB tests)

**Step 1: Write DB tests**

Add to `pipeline/test_blog_poller.py`:

```python
from pipeline.db import StateStore


def test_is_blog_post_processed_returns_false_for_unknown(tmp_path) -> None:
    store = StateStore(tmp_path / "test.db")
    assert store.is_blog_post_processed("https://example.com/post1") is False
    store.close()


def test_mark_blog_post_processed(tmp_path) -> None:
    store = StateStore(tmp_path / "test.db")
    store.mark_blog_post_processed("https://example.com/post1", "aaronson")
    assert store.is_blog_post_processed("https://example.com/post1") is True
    store.close()


def test_mark_blog_post_processed_idempotent(tmp_path) -> None:
    store = StateStore(tmp_path / "test.db")
    store.mark_blog_post_processed("https://example.com/post1", "aaronson")
    store.mark_blog_post_processed("https://example.com/post1", "aaronson")
    assert store.is_blog_post_processed("https://example.com/post1") is True
    store.close()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_blog_poller.py -k "blog_post_processed" -v`
Expected: FAIL with AttributeError (methods don't exist yet)

**Step 3: Add DB table and methods**

In `pipeline/db.py`, add to the SCHEMA string (after the `pending_the_rundown` table, before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS processed_blog_posts (
    source_url TEXT PRIMARY KEY,
    feed_slug TEXT NOT NULL,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Add methods to `StateStore` class (after the `mark_processed` method, around line 219):

```python
def is_blog_post_processed(self, source_url: str) -> bool:
    row = self._conn.execute(
        "SELECT 1 FROM processed_blog_posts WHERE source_url = ?",
        (source_url,),
    ).fetchone()
    return row is not None

def mark_blog_post_processed(self, source_url: str, feed_slug: str) -> None:
    self._conn.execute(
        "INSERT OR REPLACE INTO processed_blog_posts (source_url, feed_slug) VALUES (?, ?)",
        (source_url, feed_slug),
    )
    self._conn.commit()
```

**Step 4: Add preset**

In `pipeline/presets.py`, add after the FP Digest preset (after line 56, before the closing `)`):

```python
NewsletterPreset(
    name="Scott Aaronson - Shtetl-Optimized",
    route_tags=("aaronson", "shtetl-optimized"),
    tts_model="tts-1-hd",
    tts_voice="fable",
    category="Technology",
    feed_slug="aaronson",
),
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_blog_poller.py -v`
Expected: All PASS

**Step 6: Run full test suite to check for regressions**

Run: `uv run pytest pipeline/ -v`
Expected: All PASS

**Step 7: Commit**

```bash
git add pipeline/db.py pipeline/presets.py pipeline/test_blog_poller.py
git commit -m "feat: add processed_blog_posts DB table and aaronson preset"
```

---

### Task 3: RSS Feed Parser

**Files:**
- Create: `pipeline/blog_poller.py`
- Test: `pipeline/test_blog_poller.py` (add feed parsing tests)

**Step 1: Write tests for RSS parsing**

Add to `pipeline/test_blog_poller.py`:

```python
from pipeline.blog_poller import BlogPost, parse_blog_feed


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
<title>Test Blog</title>
<item>
  <title>Test Post</title>
  <link>https://example.com/post1</link>
  <pubDate>Sun, 29 Mar 2026 05:17:35 +0000</pubDate>
  <content:encoded><![CDATA[<p>Hello world</p>]]></content:encoded>
  <guid isPermaLink="false">https://example.com/?p=123</guid>
</item>
<item>
  <title>Second Post</title>
  <link>https://example.com/post2</link>
  <pubDate>Wed, 18 Mar 2026 16:10:13 +0000</pubDate>
  <content:encoded><![CDATA[<p>Another post</p>]]></content:encoded>
  <guid isPermaLink="false">https://example.com/?p=124</guid>
</item>
</channel>
</rss>"""


def test_parse_blog_feed_extracts_posts() -> None:
    posts = parse_blog_feed(SAMPLE_RSS)
    assert len(posts) == 2
    assert posts[0].title == "Test Post"
    assert posts[0].url == "https://example.com/post1"
    assert posts[0].html_content == "<p>Hello world</p>"
    assert posts[0].pub_date == "Sun, 29 Mar 2026 05:17:35 +0000"


def test_parse_blog_feed_handles_missing_content() -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Test</title>
    <item><title>No Content</title><link>https://example.com/x</link>
    <pubDate>Sun, 29 Mar 2026 05:17:35 +0000</pubDate></item>
    </channel></rss>"""
    posts = parse_blog_feed(rss)
    assert len(posts) == 0  # skip posts without content:encoded


def test_parse_blog_feed_empty() -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Test</title></channel></rss>"""
    posts = parse_blog_feed(rss)
    assert len(posts) == 0
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_blog_poller.py -k "parse_blog_feed" -v`
Expected: FAIL with ImportError

**Step 3: Implement RSS parser**

```python
# pipeline/blog_poller.py
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"


@dataclass(frozen=True)
class BlogPost:
    title: str
    url: str
    pub_date: str
    html_content: str
    guid: str


def parse_blog_feed(rss_xml: str) -> list[BlogPost]:
    """Parse RSS XML and return blog posts with content."""
    root = ET.fromstring(rss_xml)
    posts: list[BlogPost] = []

    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pub_date_el = item.find("pubDate")
        content_el = item.find(f"{{{_CONTENT_NS}}}encoded")
        guid_el = item.find("guid")

        if title_el is None or link_el is None or pub_date_el is None:
            continue
        if content_el is None or not content_el.text:
            continue

        posts.append(
            BlogPost(
                title=title_el.text or "",
                url=link_el.text or "",
                pub_date=pub_date_el.text or "",
                html_content=content_el.text.strip(),
                guid=guid_el.text if guid_el is not None and guid_el.text else link_el.text or "",
            )
        )

    return posts
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_blog_poller.py -k "parse_blog_feed" -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add pipeline/blog_poller.py pipeline/test_blog_poller.py
git commit -m "feat: add RSS feed parser for blog posts"
```

---

### Task 4: AI Audio Adaptation

**Files:**
- Modify: `pipeline/blog_poller.py` (add `adapt_for_audio` function)
- Test: `pipeline/test_blog_poller.py` (add adaptation tests)

**Step 1: Write tests for AI adaptation**

Add to `pipeline/test_blog_poller.py`:

```python
from unittest.mock import patch, MagicMock

from pipeline.blog_poller import adapt_for_audio


def test_adapt_for_audio_calls_gemini(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Adapted text for TTS."
    mock_client.models.generate_content.return_value = mock_response

    with patch("pipeline.blog_poller.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        result = adapt_for_audio("<p>Hello <b>world</b></p>", "Test Title")

    assert result == "Adapted text for TTS."
    call_args = mock_client.models.generate_content.call_args
    assert "gemini" in str(call_args)


def test_adapt_for_audio_returns_none_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = adapt_for_audio("<p>Hello</p>", "Test Title")
    assert result is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_blog_poller.py -k "adapt_for_audio" -v`
Expected: FAIL with ImportError

**Step 3: Implement AI adaptation**

Add to `pipeline/blog_poller.py`:

```python
import os

from google import genai
from google.genai import types


_ADAPT_SYSTEM_PROMPT = """\
You are preparing a blog post for text-to-speech conversion.
Convert the HTML to clean spoken prose. Preserve ALL content
and the author's voice faithfully. Do not summarize or editorialize.

Rules:
- Remove images entirely, no references to them
- Verbalize math notation naturally (x squared, 2 to the n, etc.)
- Keep hyperlink text, drop URLs
- Introduce blockquotes with attribution where available
- Convert horizontal rules to paragraph breaks
- Output plain text, no markdown formatting
"""


def adapt_for_audio(html_content: str, title: str) -> str | None:
    """Adapt HTML blog post content for TTS using Gemini Flash.

    Returns adapted text, or None if GEMINI_API_KEY is not set.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set, skipping audio adaptation")
        return None

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite-preview",
        contents=f"Blog post title: {title}\n\n{html_content}",
        config=types.GenerateContentConfig(
            system_instruction=_ADAPT_SYSTEM_PROMPT,
            temperature=0.3,
        ),
    )
    return response.text
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_blog_poller.py -k "adapt_for_audio" -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add pipeline/blog_poller.py pipeline/test_blog_poller.py
git commit -m "feat: add Gemini-based audio adaptation for blog posts"
```

---

### Task 5: Blog Post Processing (TTS + Publish)

**Files:**
- Modify: `pipeline/blog_poller.py` (add `process_blog_post` and `poll_and_process`)
- Test: `pipeline/test_blog_poller.py` (add end-to-end tests)

**Step 1: Write tests for single post processing**

Add to `pipeline/test_blog_poller.py`:

```python
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, ANY

from pipeline.blog_poller import process_blog_post
from pipeline.blog_sources import BLOG_SOURCES


def test_process_blog_post_publishes_episode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("PODCAST_BASE_URL", "https://test.example.com")

    store = StateStore(tmp_path / "test.db")
    r2_client = MagicMock()

    post = BlogPost(
        title="Test Post Title",
        url="https://example.com/post1",
        pub_date="Sun, 29 Mar 2026 05:17:35 +0000",
        html_content="<p>Hello world. This is a test post.</p>",
        guid="https://example.com/?p=123",
    )
    source = BLOG_SOURCES[0]

    # Mock Gemini
    mock_gemini_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Hello world. This is a test post."
    mock_gemini_client.models.generate_content.return_value = mock_response

    # Mock ttsjoin
    def fake_ttsjoin(cmd, **kwargs):
        # Find --output-file arg and create a dummy MP3
        output_idx = cmd.index("--output-file") + 1
        Path(cmd[output_idx]).write_bytes(b"\x00" * 100)
        return subprocess.CompletedProcess(cmd, 0)

    with (
        patch("pipeline.blog_poller.genai") as mock_genai,
        patch("subprocess.run", side_effect=fake_ttsjoin),
        patch("pipeline.feed.regenerate_and_upload_feed"),
    ):
        mock_genai.Client.return_value = mock_gemini_client
        process_blog_post(post, source, store, r2_client)

    # Episode should be inserted
    episodes = store.list_episodes(feed_slug="aaronson")
    assert len(episodes) == 1
    assert episodes[0].title == "Test Post Title"
    assert episodes[0].source_url == "https://example.com/post1"
    assert episodes[0].feed_slug == "aaronson"
    assert episodes[0].category == "Technology"

    # Post should be marked as processed
    assert store.is_blog_post_processed("https://example.com/post1") is True

    store.close()


def test_process_blog_post_skips_already_processed(tmp_path, monkeypatch) -> None:
    store = StateStore(tmp_path / "test.db")
    r2_client = MagicMock()

    post = BlogPost(
        title="Already Done",
        url="https://example.com/done",
        pub_date="Sun, 29 Mar 2026 05:17:35 +0000",
        html_content="<p>Already processed</p>",
        guid="done",
    )
    source = BLOG_SOURCES[0]
    store.mark_blog_post_processed("https://example.com/done", "aaronson")

    # Should not call anything - just skip
    process_blog_post(post, source, store, r2_client)

    episodes = store.list_episodes(feed_slug="aaronson")
    assert len(episodes) == 0
    store.close()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_blog_poller.py -k "process_blog_post" -v`
Expected: FAIL with ImportError

**Step 3: Implement process_blog_post**

Add to `pipeline/blog_poller.py`:

```python
import re
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.feed import regenerate_and_upload_feed
from pipeline.script_processor import TTS_MODEL

if TYPE_CHECKING:
    from pipeline.blog_sources import BlogSource
    from pipeline.db import StateStore
    from pipeline.r2 import R2Client


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        slug = "untitled"
    return slug[:80]


def process_blog_post(
    post: BlogPost,
    source: BlogSource,
    store: StateStore,
    r2_client: R2Client,
) -> None:
    """Process a single blog post: adapt, TTS, publish."""
    from pipeline.db import Episode

    if store.is_blog_post_processed(post.url):
        logger.info("Already processed: %s", post.url)
        return

    logger.info("Processing blog post: %s", post.title)

    # AI adaptation
    adapted_text = adapt_for_audio(post.html_content, post.title)
    if adapted_text is None:
        logger.error("AI adaptation failed for: %s", post.title)
        return

    # TTS
    date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    title_slug = _slugify(post.title)
    episode_slug = f"{date_str}-{title_slug}"
    episode_r2_key = f"episodes/{source.feed_slug}/{episode_slug}.mp3"

    with tempfile.TemporaryDirectory(prefix="blog-post-") as tmp_dir:
        tmp = Path(tmp_dir)
        input_txt = tmp / f"{episode_slug}.txt"
        output_mp3 = tmp / f"{episode_slug}.mp3"
        input_txt.write_text(adapted_text, encoding="utf-8")

        cmd = [
            "ttsjoin",
            "--input-file", str(input_txt),
            "--output-file", str(output_mp3),
            "--model", TTS_MODEL,
            "--voice", source.tts_voice,
        ]
        subprocess.run(cmd, check=True)

        r2_client.upload_file(output_mp3, episode_r2_key, content_type="audio/mpeg")
        size_bytes = output_mp3.stat().st_size

        # Parse duration
        duration_seconds = None
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(output_mp3)],
            check=False, capture_output=True, text=True,
        )
        if probe.returncode == 0:
            try:
                duration_seconds = int(round(float(probe.stdout.strip())))
            except ValueError:
                pass

    episode = Episode(
        id=str(uuid.uuid4()),
        title=post.title,
        slug=episode_slug,
        pub_date=format_datetime(datetime.now(tz=UTC)),
        r2_key=episode_r2_key,
        feed_slug=source.feed_slug,
        category=source.category,
        source_tag=None,
        preset_name=source.name,
        source_url=post.url,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        summary=None,
        show_notes_html=None,
    )
    store.insert_episode(episode)
    store.mark_blog_post_processed(post.url, source.feed_slug)
    regenerate_and_upload_feed(store, r2_client)
    logger.info("Published blog episode: %s", post.title)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_blog_poller.py -k "process_blog_post" -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add pipeline/blog_poller.py pipeline/test_blog_poller.py
git commit -m "feat: add blog post processing (adapt + TTS + publish)"
```

---

### Task 6: Poll-and-Process Orchestrator + RSS Fetch

**Files:**
- Modify: `pipeline/blog_poller.py` (add `fetch_and_poll`, `poll_all_blogs`)
- Test: `pipeline/test_blog_poller.py` (add orchestrator test)

**Step 1: Write test for poll_all_blogs**

Add to `pipeline/test_blog_poller.py`:

```python
from pipeline.blog_poller import poll_all_blogs


def test_poll_all_blogs_fetches_and_processes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("PODCAST_BASE_URL", "https://test.example.com")

    store = StateStore(tmp_path / "test.db")
    r2_client = MagicMock()

    # Mock HTTP fetch to return sample RSS
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_RSS
    mock_resp.raise_for_status = MagicMock()

    # Mock Gemini
    mock_gemini_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Adapted text."
    mock_gemini_client.models.generate_content.return_value = mock_response

    def fake_ttsjoin(cmd, **kwargs):
        output_idx = cmd.index("--output-file") + 1
        Path(cmd[output_idx]).write_bytes(b"\x00" * 100)
        return subprocess.CompletedProcess(cmd, 0)

    with (
        patch("pipeline.blog_poller.requests.get", return_value=mock_resp),
        patch("pipeline.blog_poller.genai") as mock_genai,
        patch("subprocess.run", side_effect=fake_ttsjoin),
        patch("pipeline.feed.regenerate_and_upload_feed"),
    ):
        mock_genai.Client.return_value = mock_gemini_client
        poll_all_blogs(store, r2_client)

    episodes = store.list_episodes(feed_slug="aaronson")
    # SAMPLE_RSS has 2 posts
    assert len(episodes) == 2
    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_blog_poller.py::test_poll_all_blogs_fetches_and_processes -v`
Expected: FAIL with ImportError

**Step 3: Implement poll_all_blogs**

Add to `pipeline/blog_poller.py`:

```python
import requests

from pipeline.blog_sources import BLOG_SOURCES


def poll_all_blogs(store: StateStore, r2_client: R2Client) -> None:
    """Fetch all blog RSS feeds and process new posts."""
    for source in BLOG_SOURCES:
        try:
            logger.info("Polling blog: %s", source.name)
            resp = requests.get(source.feed_url, timeout=30)
            resp.raise_for_status()
            posts = parse_blog_feed(resp.text)
            logger.info("Found %d posts in feed for %s", len(posts), source.name)

            for post in posts:
                try:
                    process_blog_post(post, source, store, r2_client)
                except Exception:
                    logger.exception("Failed to process post: %s", post.title)
        except Exception:
            logger.exception("Failed to poll blog: %s", source.name)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest pipeline/test_blog_poller.py::test_poll_all_blogs_fetches_and_processes -v`
Expected: PASS

**Step 5: Commit**

```bash
git add pipeline/blog_poller.py pipeline/test_blog_poller.py
git commit -m "feat: add poll_all_blogs orchestrator with RSS fetching"
```

---

### Task 7: Consumer Integration

**Files:**
- Modify: `pipeline/consumer.py` (add blog polling to consume_forever loop, after FP digest block, before the sleep)

**Step 1: Add blog polling to consumer loop**

In `pipeline/consumer.py`, add after the FP digest error handling block (after line 562, before `if not messages:`):

```python
# Poll blog sources for new posts.
try:
    now = time.time()
    if not hasattr(consume_forever, "_last_blog_poll"):
        consume_forever._last_blog_poll = 0.0  # type: ignore[attr-defined]
    blog_poll_interval = 6 * 3600  # 6 hours
    if now - consume_forever._last_blog_poll >= blog_poll_interval:  # type: ignore[attr-defined]
        from pipeline.blog_poller import poll_all_blogs

        print("Polling blog sources for new posts...")
        poll_all_blogs(store, r2_client)
        consume_forever._last_blog_poll = now  # type: ignore[attr-defined]
except Exception as exc:
    print(f"Error polling blog sources: {exc}")
```

**Step 2: Run full test suite**

Run: `uv run pytest pipeline/ -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add pipeline/consumer.py
git commit -m "feat: integrate blog polling into consumer loop (every 6 hours)"
```

---

### Task 8: CLI Command

**Files:**
- Modify: `pipeline/__main__.py` (add `poll-blogs` command before `sync-sources`)

**Step 1: Add CLI command**

In `pipeline/__main__.py`, add before the `sync-sources` command (before line 835):

```python
@cli.command("poll-blogs")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Fetch and adapt but don't TTS or publish.",
)
def poll_blogs_command(dry_run: bool) -> None:
    """Poll all blog RSS feeds and process new posts."""
    from pipeline.blog_poller import poll_all_blogs, parse_blog_feed, adapt_for_audio
    from pipeline.blog_sources import BLOG_SOURCES

    import requests

    if dry_run:
        for source in BLOG_SOURCES:
            click.echo(f"Polling: {source.name} ({source.feed_url})")
            try:
                resp = requests.get(source.feed_url, timeout=30)
                resp.raise_for_status()
                posts = parse_blog_feed(resp.text)
                click.echo(f"  Found {len(posts)} posts in feed")

                store = StateStore(_default_state_db_path())
                try:
                    for post in posts:
                        processed = store.is_blog_post_processed(post.url)
                        status = "SKIP (already processed)" if processed else "NEW"
                        click.echo(f"  [{status}] {post.title}")
                        if not processed:
                            adapted = adapt_for_audio(post.html_content, post.title)
                            if adapted:
                                click.echo(f"    Adapted: {len(adapted)} chars")
                            else:
                                click.echo("    Adaptation failed (no API key?)")
                finally:
                    store.close()
            except Exception as e:
                click.echo(f"  FAILED: {e}", err=True)
        return

    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()
        poll_all_blogs(store, r2_client)
        click.echo("Blog polling complete.")
    finally:
        store.close()
```

**Step 2: Test the CLI manually**

Run: `uv run python -m pipeline poll-blogs --dry-run`
Expected: Shows list of posts with NEW/SKIP status

**Step 3: Commit**

```bash
git add pipeline/__main__.py
git commit -m "feat: add poll-blogs CLI command"
```

---

### Task 9: Update AGENTS.md and Documentation

**Files:**
- Modify: `AGENTS.md` (add Aaronson feed URL, blog poller docs)

**Step 1: Update AGENTS.md**

Add to the subscription URLs section:
```
- Aaronson: `https://podcast.mohrbacher.dev/feeds/aaronson.xml`
```

Add a new section after "Source Caching":
```markdown
## Blog Polling

WordPress and other blog sources are polled via RSS for new posts. Each new post is adapted for audio (via Gemini Flash), converted to speech, and published as a standalone episode.

**Polling interval:** Every 6 hours (inside the consumer loop)

**CLI:** `uv run python -m pipeline poll-blogs [--dry-run]`

**Sources:**
- Scott Aaronson / Shtetl-Optimized: `https://scottaaronson.blog/?feed=rss2` -> feed slug `aaronson`

**Key module:** `pipeline/blog_poller.py` — RSS fetch, AI adaptation, TTS + publish
**Source definitions:** `pipeline/blog_sources.py` — `BlogSource` dataclass, `BLOG_SOURCES` tuple
```

**Step 2: Commit**

```bash
git add AGENTS.md
git commit -m "docs: add Aaronson blog podcast to AGENTS.md"
```

---

### Task 10: Final Verification

**Step 1: Run full test suite**

Run: `uv run pytest pipeline/ -v`
Expected: All PASS

**Step 2: Dry-run the blog poller**

Run: `uv run python -m pipeline poll-blogs --dry-run`
Expected: Shows Aaronson posts with adaptation preview

**Step 3: Verify consumer still starts**

Run: `sudo systemctl restart my-podcasts-consumer && sleep 3 && sudo systemctl status my-podcasts-consumer --no-pager`
Expected: Consumer running, no errors
