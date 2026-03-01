# Things Happen Delayed Digest Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Automatically extract "Things Happen" links from Matt Levine's Money Stuff emails, fetch the articles after a 24-hour delay (via archive.today with fallbacks), generate an LLM-written audio briefing using Claude Opus 4.6, and publish it as a separate podcast feed.

**Architecture:** When a Levine email is processed, the Things Happen section is parsed and a pending job is stored in SQLite with a 24-hour delay. The existing consumer loop checks for due jobs on each iteration. Due jobs trigger a fetch-summarize-TTS pipeline: articles are fetched (archive.today -> live URL -> headline-only fallback), Claude Opus 4.6 via `opencode run` generates a TTS script, `ttsjoin` produces audio, and the episode is published to `feeds/things-happen.xml`.

**Tech Stack:** Python, BeautifulSoup, SQLite, `opencode run` (Claude Opus 4.6), `ttsjoin` (OpenAI TTS-1-HD), Cloudflare R2

---

### Task 1: Things Happen link extractor

**Files:**
- Create: `pipeline/things_happen_extractor.py`
- Create: `pipeline/test_things_happen_extractor.py`

This module parses the "Things Happen" section from a Levine email's raw HTML and extracts each headline with its associated URL.

**Step 1: Write the failing test**

Create `pipeline/test_things_happen_extractor.py`:

```python
from __future__ import annotations

import pytest

from pipeline.things_happen_extractor import ThingsHappenLink, extract_things_happen


# Minimal reproduction of Bloomberg's email HTML structure for Things Happen.
THINGS_HAPPEN_HTML = """\
<html><body>
<table><tr><td>
  <h2 style="font-weight: bold;">Things happen</h2>
</td></tr></table>
<p style="margin: 16px 0;">How <a href="https://links.message.bloomberg.com/s/c/FAKE1">Blue Owl's battles</a> sparked a chill for private credit.\xa0Jefferies Fund Sued by Investors Over <a href="https://links.message.bloomberg.com/s/c/FAKE2"> First Brands Collapse</a>.\xa0<a href="https://links.message.bloomberg.com/s/c/FAKE3">Janus Bidding War</a> Begins as Victory Capital Tops Trian Offer. AI <a href="https://links.message.bloomberg.com/s/c/FAKE4">dispersion trades</a>.</p>
<p style="margin: 16px 0;">If you'd like to get Money Stuff in handy email form, right in your inbox, please subscribe at this link.</p>
</body></html>
"""


def test_extracts_links_from_things_happen_section() -> None:
    links = extract_things_happen(THINGS_HAPPEN_HTML)
    assert len(links) == 4
    assert all(isinstance(link, ThingsHappenLink) for link in links)


def test_link_text_is_cleaned() -> None:
    links = extract_things_happen(THINGS_HAPPEN_HTML)
    assert links[0].link_text == "Blue Owl's battles"
    assert links[1].link_text == "First Brands Collapse"


def test_link_urls_are_captured() -> None:
    links = extract_things_happen(THINGS_HAPPEN_HTML)
    assert links[0].raw_url == "https://links.message.bloomberg.com/s/c/FAKE1"
    assert links[3].raw_url == "https://links.message.bloomberg.com/s/c/FAKE4"


def test_headline_context_captures_full_sentence() -> None:
    links = extract_things_happen(THINGS_HAPPEN_HTML)
    assert "Blue Owl's battles" in links[0].headline_context
    assert "sparked a chill for private credit" in links[0].headline_context


def test_returns_empty_list_when_no_things_happen_section() -> None:
    html = "<html><body><p>No Things Happen here.</p></body></html>"
    links = extract_things_happen(html)
    assert links == []
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_extractor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.things_happen_extractor'`

**Step 3: Write the implementation**

Create `pipeline/things_happen_extractor.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class ThingsHappenLink:
    link_text: str
    raw_url: str
    headline_context: str


def extract_things_happen(html_content: str) -> list[ThingsHappenLink]:
    """Extract links from the 'Things happen' section of a Levine email."""
    soup = BeautifulSoup(html_content, "html.parser")

    th_h2 = None
    for h2 in soup.find_all("h2"):
        if "things happen" in h2.get_text().lower():
            th_h2 = h2
            break

    if th_h2 is None:
        return []

    p_tag = th_h2.find_next("p")
    if p_tag is None:
        return []

    full_text = p_tag.get_text()
    # Split into sentences on ". " or ".\xa0" boundaries.
    sentences = re.split(r"\.[\s\xa0]+", full_text)

    links: list[ThingsHappenLink] = []
    for a_tag in p_tag.find_all("a"):
        href = a_tag.get("href", "")
        if not href:
            continue
        link_text = a_tag.get_text().strip()
        if not link_text:
            continue

        # Find the sentence containing this link text.
        headline_context = link_text
        for sentence in sentences:
            if link_text in sentence:
                headline_context = sentence.strip()
                break

        links.append(
            ThingsHappenLink(
                link_text=link_text,
                raw_url=href,
                headline_context=headline_context,
            )
        )

    return links
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest pipeline/test_things_happen_extractor.py -v`
Expected: All 5 tests pass.

**Step 5: Commit**

```bash
git add pipeline/things_happen_extractor.py pipeline/test_things_happen_extractor.py
git commit -m "feat: add Things Happen link extractor for Levine emails"
```

---

### Task 2: Bloomberg redirect URL resolver

**Files:**
- Modify: `pipeline/things_happen_extractor.py`
- Modify: `pipeline/test_things_happen_extractor.py`

Bloomberg wraps all links through `links.message.bloomberg.com/s/c/...` redirects. We need to resolve these to get the real article URLs. The existing `_resolve_once()` in `source_adapters.py` follows one redirect hop; here we need full redirect following to get the final URL.

**Step 1: Write the failing test**

Add to `pipeline/test_things_happen_extractor.py`:

```python
def test_resolve_redirect_urls(monkeypatch) -> None:
    """Resolving Bloomberg redirect URLs produces real article URLs."""
    from pipeline.things_happen_extractor import resolve_redirect_url

    def fake_head(url, **kwargs):
        class FakeResponse:
            url = "https://www.bloomberg.com/news/articles/2026-02-26/blue-owl"
            status_code = 200
        return FakeResponse()

    monkeypatch.setattr("requests.head", fake_head)

    result = resolve_redirect_url("https://links.message.bloomberg.com/s/c/FAKE1")
    assert result == "https://www.bloomberg.com/news/articles/2026-02-26/blue-owl"


def test_resolve_redirect_returns_original_on_failure(monkeypatch) -> None:
    """If redirect resolution fails, return the original URL."""
    from pipeline.things_happen_extractor import resolve_redirect_url

    def fake_head(url, **kwargs):
        raise Exception("network error")

    monkeypatch.setattr("requests.head", fake_head)

    result = resolve_redirect_url("https://links.message.bloomberg.com/s/c/FAKE1")
    assert result == "https://links.message.bloomberg.com/s/c/FAKE1"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_extractor.py::test_resolve_redirect_urls -v`
Expected: FAIL — `cannot import name 'resolve_redirect_url'`

**Step 3: Write the implementation**

Add to `pipeline/things_happen_extractor.py`:

```python
import requests


def resolve_redirect_url(url: str) -> str:
    """Follow redirects to get the final URL. Returns original on failure."""
    try:
        response = requests.head(url, allow_redirects=True, timeout=10)
        return response.url
    except Exception:
        return url
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_extractor.py -v`
Expected: All 7 tests pass.

**Step 5: Commit**

```bash
git add pipeline/things_happen_extractor.py pipeline/test_things_happen_extractor.py
git commit -m "feat: add Bloomberg redirect URL resolver for Things Happen links"
```

---

### Task 3: Database schema for pending Things Happen jobs

**Files:**
- Modify: `pipeline/db.py`
- Create: `pipeline/test_things_happen_db.py`

Add a `pending_things_happen` table to track jobs that need processing after a 24-hour delay.

**Step 1: Write the failing test**

Create `pipeline/test_things_happen_db.py`:

```python
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from pipeline.db import StateStore


def test_insert_and_list_pending_things_happen(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    links_json = json.dumps([{"link_text": "Test", "raw_url": "http://example.com", "headline_context": "Test story"}])
    store.insert_pending_things_happen(
        email_r2_key="inbox/raw/abc.eml",
        date_str="2026-02-26",
        links_json=links_json,
        delay_hours=24,
    )
    # Should not be due yet (24h in the future).
    due = store.list_due_things_happen()
    assert len(due) == 0
    store.close()


def test_due_jobs_returned_after_delay(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    links_json = json.dumps([{"link_text": "Test", "raw_url": "http://example.com", "headline_context": "Test story"}])
    # Insert with process_after in the past.
    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_things_happen
           (id, email_r2_key, date_str, links_json, process_after, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("test-id", "inbox/raw/abc.eml", "2026-02-26", links_json, past, "pending"),
    )
    store._conn.commit()
    due = store.list_due_things_happen()
    assert len(due) == 1
    assert due[0]["id"] == "test-id"
    assert due[0]["date_str"] == "2026-02-26"
    store.close()


def test_mark_things_happen_completed(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_things_happen
           (id, email_r2_key, date_str, links_json, process_after, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("test-id", "inbox/raw/abc.eml", "2026-02-26", "[]", past, "pending"),
    )
    store._conn.commit()
    store.mark_things_happen_completed("test-id")
    due = store.list_due_things_happen()
    assert len(due) == 0
    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_db.py -v`
Expected: FAIL — `AttributeError: 'StateStore' object has no attribute 'insert_pending_things_happen'`

**Step 3: Write the implementation**

In `pipeline/db.py`, add to the `SCHEMA` string:

```sql
CREATE TABLE IF NOT EXISTS pending_things_happen (
    id TEXT PRIMARY KEY,
    email_r2_key TEXT NOT NULL,
    date_str TEXT NOT NULL,
    links_json TEXT NOT NULL,
    process_after TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Add methods to `StateStore`:

```python
import json
import uuid
from datetime import UTC, datetime, timedelta

def insert_pending_things_happen(
    self,
    email_r2_key: str,
    date_str: str,
    links_json: str,
    delay_hours: int = 24,
) -> str:
    job_id = str(uuid.uuid4())
    process_after = (datetime.now(tz=UTC) + timedelta(hours=delay_hours)).isoformat()
    self._conn.execute(
        """INSERT INTO pending_things_happen
           (id, email_r2_key, date_str, links_json, process_after, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (job_id, email_r2_key, date_str, links_json, process_after, "pending"),
    )
    self._conn.commit()
    return job_id

def list_due_things_happen(self) -> list[dict]:
    now = datetime.now(tz=UTC).isoformat()
    rows = self._conn.execute(
        """SELECT id, email_r2_key, date_str, links_json, process_after
           FROM pending_things_happen
           WHERE status = 'pending' AND process_after <= ?
           ORDER BY process_after ASC""",
        (now,),
    ).fetchall()
    return [dict(row) for row in rows]

def mark_things_happen_completed(self, job_id: str) -> None:
    self._conn.execute(
        "UPDATE pending_things_happen SET status = 'completed' WHERE id = ?",
        (job_id,),
    )
    self._conn.commit()
```

Note: `uuid` and `datetime` imports already exist in the file or need to be added at the top of `db.py`.

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_db.py -v`
Expected: All 3 tests pass.

Also run: `uv run pytest -v` to ensure nothing is broken.

**Step 5: Commit**

```bash
git add pipeline/db.py pipeline/test_things_happen_db.py
git commit -m "feat: add pending_things_happen table for delayed digest jobs"
```

---

### Task 4: Article fetcher with archive.today fallback chain

**Files:**
- Create: `pipeline/article_fetcher.py`
- Create: `pipeline/test_article_fetcher.py`

Fetches article content using a three-tier fallback: archive.today -> live URL -> headline-only. Each result is tagged with its source tier so the LLM can communicate transparency.

**Step 1: Write the failing tests**

Create `pipeline/test_article_fetcher.py`:

```python
from __future__ import annotations

from pipeline.article_fetcher import FetchedArticle, fetch_article


def test_archive_today_success(monkeypatch) -> None:
    def fake_get(url, **kwargs):
        class FakeResponse:
            status_code = 200
            text = "<html><body><article><p>Full article text here.</p></article></body></html>"
            url = "https://archive.today/2026/https://example.com/article"
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)

    result = fetch_article("https://example.com/article", "Test headline")
    assert isinstance(result, FetchedArticle)
    assert result.source_tier == "archive"
    assert "Full article text" in result.content


def test_archive_fails_falls_back_to_live(monkeypatch) -> None:
    call_count = 0

    def fake_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if "archive" in url:
            class FailResponse:
                status_code = 429
                text = ""
                url = url
            return FailResponse()
        else:
            class OkResponse:
                status_code = 200
                text = "<html><body><p>Live article content.</p></body></html>"
                url = url
            return OkResponse()

    monkeypatch.setattr("requests.get", fake_get)

    result = fetch_article("https://example.com/article", "Test headline")
    assert result.source_tier == "live"
    assert "Live article content" in result.content


def test_all_fetches_fail_returns_headline_only(monkeypatch) -> None:
    def fake_get(url, **kwargs):
        raise Exception("network error")

    monkeypatch.setattr("requests.get", fake_get)

    result = fetch_article("https://example.com/article", "Test headline")
    assert result.source_tier == "headline_only"
    assert result.content == "Test headline"


def test_source_tier_label_messages() -> None:
    a = FetchedArticle(url="x", content="y", source_tier="archive")
    assert "full archived article" in a.source_label.lower()

    b = FetchedArticle(url="x", content="y", source_tier="live")
    assert "publicly available" in b.source_label.lower()

    c = FetchedArticle(url="x", content="y", source_tier="headline_only")
    assert "headline" in c.source_label.lower()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_article_fetcher.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `pipeline/article_fetcher.py`:

```python
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
        "Gecko/20100101 Firefox/128.0"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

SOURCE_LABELS = {
    "archive": "Based on the full archived article",
    "live": "Based on the publicly available portion of the article",
    "headline_only": "Based on the headline alone",
}


@dataclass(frozen=True)
class FetchedArticle:
    url: str
    content: str
    source_tier: str  # "archive", "live", or "headline_only"

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source_tier, self.source_tier)


def _extract_article_text(html: str) -> str:
    """Extract readable text from an HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    article = soup.find("article")
    target = article if article else soup.find("body") or soup
    text = target.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def _try_archive_today(url: str) -> str | None:
    """Try to fetch the article from archive.today."""
    archive_url = f"https://archive.today/newest/{quote(url, safe='')}"
    try:
        response = requests.get(
            archive_url,
            headers=_BROWSER_HEADERS,
            timeout=15,
            allow_redirects=True,
        )
        if response.status_code != 200:
            return None
        # If we weren't redirected to an archived page, there's no archive.
        if "/newest/" in response.url:
            return None
        return _extract_article_text(response.text)
    except Exception:
        return None


def _try_live_url(url: str) -> str | None:
    """Try to fetch the article directly."""
    try:
        response = requests.get(
            url,
            headers=_BROWSER_HEADERS,
            timeout=15,
            allow_redirects=True,
        )
        if response.status_code != 200:
            return None
        text = _extract_article_text(response.text)
        # If we got very little text, the article is probably paywalled.
        if len(text) < 200:
            return None
        return text
    except Exception:
        return None


def fetch_article(url: str, headline: str) -> FetchedArticle:
    """Fetch article content with fallback chain: archive -> live -> headline."""
    # Tier 1: archive.today
    content = _try_archive_today(url)
    if content:
        return FetchedArticle(url=url, content=content, source_tier="archive")

    # Tier 2: live URL
    content = _try_live_url(url)
    if content:
        return FetchedArticle(url=url, content=content, source_tier="live")

    # Tier 3: headline only
    return FetchedArticle(url=url, content=headline, source_tier="headline_only")


def fetch_all_articles(
    links: list[dict],
    delay_between: float = 3.0,
) -> list[FetchedArticle]:
    """Fetch all articles with a delay between requests to be polite."""
    results: list[FetchedArticle] = []
    for i, link in enumerate(links):
        if i > 0:
            time.sleep(delay_between)
        article = fetch_article(link["resolved_url"], link["headline_context"])
        results.append(article)
    return results
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_article_fetcher.py -v`
Expected: All 4 tests pass.

**Step 5: Commit**

```bash
git add pipeline/article_fetcher.py pipeline/test_article_fetcher.py
git commit -m "feat: add article fetcher with archive.today fallback chain"
```

---

### Task 5: LLM summarizer via opencode

**Files:**
- Create: `pipeline/summarizer.py`
- Create: `pipeline/test_summarizer.py`

Invokes `opencode run` headlessly with Claude Opus 4.6 to generate a TTS-friendly briefing script from the fetched articles.

**Step 1: Write the failing test**

Create `pipeline/test_summarizer.py`:

```python
from __future__ import annotations

from pipeline.article_fetcher import FetchedArticle
from pipeline.summarizer import build_prompt, generate_briefing_script


def test_build_prompt_includes_all_articles() -> None:
    articles = [
        FetchedArticle(url="https://example.com/1", content="Article about banks.", source_tier="archive"),
        FetchedArticle(url="https://example.com/2", content="Tech layoffs headline", source_tier="headline_only"),
    ]
    prompt = build_prompt(articles, date_str="2026-02-26")
    assert "Article about banks" in prompt
    assert "Tech layoffs headline" in prompt
    assert "2026-02-26" in prompt
    assert "full archived article" in prompt.lower()
    assert "headline alone" in prompt.lower()


def test_generate_briefing_calls_opencode(monkeypatch) -> None:
    """Verify that generate_briefing_script invokes opencode run."""
    import subprocess

    captured_args = {}

    def fake_run(cmd, **kwargs):
        captured_args["cmd"] = cmd
        class FakeResult:
            returncode = 0
            stdout = "Here is your briefing script about today's news."
        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_run)

    articles = [
        FetchedArticle(url="https://example.com/1", content="Test content.", source_tier="archive"),
    ]
    result = generate_briefing_script(articles, date_str="2026-02-26")
    assert "briefing script" in result
    assert captured_args["cmd"][0] == "opencode"
    assert "run" in captured_args["cmd"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_summarizer.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `pipeline/summarizer.py`:

```python
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pipeline.article_fetcher import FetchedArticle


PROMPT_TEMPLATE = """\
You are generating a podcast briefing script for a listener who reads Matt Levine's Money Stuff newsletter. Today's date is {date_str}. Below are the stories from the "Things Happen" section at the end of the newsletter. Your job is to brief the listener on each story: what happened, why it matters, and which stories are the biggest deals today.

IMPORTANT RULES:
- Be conversational and concise. This will be read aloud by a TTS engine.
- For each story, clearly state your source quality BEFORE summarizing:
  - If marked "FULL ARCHIVED ARTICLE": you have the complete article text. Summarize the key points.
  - If marked "PUBLICLY AVAILABLE PORTION": you have partial content. Summarize what's available and note it's partial.
  - If marked "HEADLINE ONLY": you only have the headline. Give brief context based on your knowledge, and be upfront that you're working from the headline alone.
- Start with a brief intro: "Here are today's Things Happen stories from Money Stuff."
- End with a brief sign-off.
- Flag the 2-3 biggest stories early on.
- Do NOT use any markdown formatting, bullet points, or special characters. Plain spoken English only.
- Do NOT use the word "delve" or any of its variants.

---

STORIES:

{stories_block}
"""


def build_prompt(articles: list[FetchedArticle], date_str: str) -> str:
    """Build the LLM prompt from fetched articles."""
    stories: list[str] = []
    for i, article in enumerate(articles, 1):
        tier_label = article.source_label.upper()
        stories.append(
            f"Story {i} [{tier_label}]:\n"
            f"URL: {article.url}\n"
            f"Content:\n{article.content}\n"
        )
    stories_block = "\n---\n".join(stories)
    return PROMPT_TEMPLATE.format(date_str=date_str, stories_block=stories_block)


def generate_briefing_script(
    articles: list[FetchedArticle],
    date_str: str,
) -> str:
    """Generate a TTS briefing script using Claude Opus 4.6 via opencode."""
    prompt = build_prompt(articles, date_str)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="things-happen-prompt-",
        delete=False,
    ) as f:
        f.write(prompt)
        prompt_path = f.name

    try:
        cmd = [
            "opencode",
            "run",
            "--file", prompt_path,
            "Read the attached file. It contains a prompt with instructions "
            "and article content. Follow the instructions in the file exactly "
            "and generate the podcast briefing script. Output ONLY the script "
            "text, nothing else.",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"opencode run failed (exit {result.returncode}): {result.stderr}"
            )
        # opencode default format includes a header line "build · model-name"
        # Strip that and any leading/trailing whitespace.
        output = result.stdout.strip()
        lines = output.splitlines()
        # Skip header lines that start with ">" or are empty.
        content_lines = []
        past_header = False
        for line in lines:
            if not past_header and (line.startswith(">") or line.strip() == ""):
                continue
            past_header = True
            content_lines.append(line)
        return "\n".join(content_lines).strip()
    finally:
        Path(prompt_path).unlink(missing_ok=True)
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_summarizer.py -v`
Expected: All 2 tests pass.

**Step 5: Commit**

```bash
git add pipeline/summarizer.py pipeline/test_summarizer.py
git commit -m "feat: add LLM summarizer using opencode run for Things Happen briefings"
```

---

### Task 6: Things Happen preset and cover art

**Files:**
- Modify: `pipeline/presets.py`
- Create: `assets/podcast/cover-things-happen.jpg`

**Step 1: Add the preset**

Add to the `PRESETS` tuple in `pipeline/presets.py`:

```python
NewsletterPreset(
    name="Things Happen - Money Stuff Links",
    route_tags=("things-happen",),
    tts_model="tts-1-hd",
    tts_voice="nova",
    category="Business",
    feed_slug="things-happen",
),
```

**Step 2: Create cover art**

For the MVP, reuse the Levine cover art:
```bash
cp assets/podcast/cover-levine.jpg assets/podcast/cover-things-happen.jpg
```

Upload to R2:
```bash
export R2_ACCOUNT_ID="$(sudo cat /run/secrets/r2_account_id)"
export R2_ACCESS_KEY_ID="$(sudo cat /run/secrets/r2_access_key_id)"
export R2_SECRET_ACCESS_KEY="$(sudo cat /run/secrets/r2_secret_access_key)"
uv run python3 -c "
from pipeline.r2 import R2Client
from pathlib import Path
r2 = R2Client()
r2.upload_file(Path('assets/podcast/cover-things-happen.jpg'), 'cover-things-happen.jpg', content_type='image/jpeg')
print('Uploaded cover-things-happen.jpg to R2')
"
```

**Step 3: Run tests**

Run: `uv run pytest -v`
Expected: All tests pass.

**Step 4: Commit**

```bash
git add pipeline/presets.py assets/podcast/cover-things-happen.jpg
git commit -m "feat: add Things Happen preset and cover art"
```

---

### Task 7: Things Happen episode processor

**Files:**
- Create: `pipeline/things_happen_processor.py`
- Create: `pipeline/test_things_happen_processor.py`

This is the orchestrator that ties together: link resolution, article fetching, LLM summarization, TTS, upload, and feed publication. It processes a single pending Things Happen job.

**Step 1: Write the failing test**

Create `pipeline/test_things_happen_processor.py`. This is an integration-level test that mocks external dependencies (network, opencode, ttsjoin, R2):

```python
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.things_happen_processor import process_things_happen_job


def test_process_things_happen_job_end_to_end(tmp_path, monkeypatch) -> None:
    """Verify the full orchestration: resolve links, fetch, summarize, TTS, upload."""
    from pipeline.db import StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    links = [
        {
            "link_text": "Blue Owl's battles",
            "raw_url": "https://links.message.bloomberg.com/s/c/FAKE1",
            "headline_context": "How Blue Owl's battles sparked a chill",
        },
    ]

    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_things_happen
           (id, email_r2_key, date_str, links_json, process_after, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("job-1", "inbox/raw/abc.eml", "2026-02-26", json.dumps(links), past, "pending"),
    )
    store._conn.commit()

    # Mock redirect resolution
    monkeypatch.setattr(
        "requests.head",
        lambda url, **kw: type("R", (), {"url": "https://bloomberg.com/article", "status_code": 200})(),
    )

    # Mock article fetching (skip network)
    monkeypatch.setattr(
        "pipeline.things_happen_processor.fetch_all_articles",
        lambda links, **kw: [
            type("A", (), {
                "url": "https://bloomberg.com/article",
                "content": "Full article text.",
                "source_tier": "archive",
                "source_label": "Based on the full archived article",
            })(),
        ],
    )

    # Mock opencode run (LLM)
    monkeypatch.setattr(
        "pipeline.things_happen_processor.generate_briefing_script",
        lambda articles, date_str: "Here is the briefing script for today.",
    )

    # Mock ttsjoin subprocess
    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="60.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    job = store.list_due_things_happen()[0]
    process_things_happen_job(job, store, r2_client)

    # Job should be marked completed.
    assert store.list_due_things_happen() == []

    # Episode should be inserted.
    episodes = store.list_episodes(feed_slug="things-happen")
    assert len(episodes) == 1
    assert "Things Happen" in episodes[0].title
    assert "2026-02-26" in episodes[0].title

    # MP3 should be uploaded to R2.
    r2_client.upload_file.assert_called_once()
    upload_key = r2_client.upload_file.call_args[0][1]
    assert upload_key.startswith("episodes/things-happen/")

    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_processor.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `pipeline/things_happen_processor.py`:

```python
from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.article_fetcher import fetch_all_articles
from pipeline.db import Episode
from pipeline.feed import regenerate_and_upload_feed
from pipeline.summarizer import generate_briefing_script
from pipeline.things_happen_extractor import resolve_redirect_url


if TYPE_CHECKING:
    from pipeline.db import StateStore
    from pipeline.r2 import R2Client


TTS_MODEL = "tts-1-hd"
TTS_VOICE = "nova"
FEED_SLUG = "things-happen"
CATEGORY = "Business"


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


def process_things_happen_job(
    job: dict,
    store: StateStore,
    r2_client: R2Client,
) -> None:
    """Process a single pending Things Happen job."""
    job_id = job["id"]
    date_str = job["date_str"]
    links_raw = json.loads(job["links_json"])

    # Step 1: Resolve redirect URLs.
    for link in links_raw:
        link["resolved_url"] = resolve_redirect_url(link["raw_url"])

    # Step 2: Fetch articles with fallback chain.
    articles = fetch_all_articles(links_raw, delay_between=3.0)

    # Step 3: Generate briefing script via LLM.
    script = generate_briefing_script(articles, date_str=date_str)

    # Step 4: TTS.
    episode_slug = f"{date_str}-things-happen"
    episode_r2_key = f"episodes/{FEED_SLUG}/{episode_slug}.mp3"

    with tempfile.TemporaryDirectory(prefix="things-happen-") as tmp_dir:
        tmp = Path(tmp_dir)
        input_txt = tmp / f"{episode_slug}.txt"
        output_mp3 = tmp / f"{episode_slug}.mp3"
        input_txt.write_text(script, encoding="utf-8")

        cmd = [
            "ttsjoin",
            "--input-file", str(input_txt),
            "--output-file", str(output_mp3),
            "--model", TTS_MODEL,
            "--voice", TTS_VOICE,
        ]
        subprocess.run(cmd, check=True)

        r2_client.upload_file(output_mp3, episode_r2_key, content_type="audio/mpeg")
        size_bytes = output_mp3.stat().st_size
        duration_seconds = _parse_duration_seconds(output_mp3)

    # Step 5: Insert episode and mark job complete.
    episode_title = f"{date_str} - Things Happen"
    episode = Episode(
        id=str(uuid.uuid4()),
        title=episode_title,
        slug=episode_slug,
        pub_date=format_datetime(datetime.now(tz=UTC)),
        r2_key=episode_r2_key,
        feed_slug=FEED_SLUG,
        category=CATEGORY,
        source_tag="things-happen",
        preset_name="Things Happen - Money Stuff Links",
        source_url=None,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
    )
    store.insert_episode(episode)
    store.mark_things_happen_completed(job_id)
    regenerate_and_upload_feed(store, r2_client)
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_processor.py -v`
Expected: PASS.

Also: `uv run pytest -v` — all tests pass.

**Step 5: Commit**

```bash
git add pipeline/things_happen_processor.py pipeline/test_things_happen_processor.py
git commit -m "feat: add Things Happen episode processor orchestrating fetch-summarize-TTS"
```

---

### Task 8: Hook extraction into Levine email processing

**Files:**
- Modify: `pipeline/processor.py`
- Modify existing tests or add to `pipeline/test_things_happen_processor.py`

When a Levine email is processed (`feed_slug == "levine"`), extract Things Happen links and create a pending job.

**Step 1: Write the failing test**

Add to `pipeline/test_things_happen_processor.py`:

```python
def test_levine_email_creates_pending_things_happen_job(tmp_path, monkeypatch) -> None:
    """Processing a Levine email with Things Happen should create a pending job."""
    from pipeline.db import StateStore
    from pipeline.processor import _maybe_queue_things_happen

    store = StateStore(tmp_path / "test.sqlite3")

    raw_html = """\
<html><body>
<h2>Things happen</h2>
<p>How <a href="https://links.message.bloomberg.com/s/c/FAKE1">Blue Owl</a> sparked a chill. <a href="https://links.message.bloomberg.com/s/c/FAKE2">Janus Bidding War</a> begins.</p>
</body></html>
"""

    _maybe_queue_things_happen(
        raw_email_html=raw_html,
        source_r2_key="inbox/raw/test.eml",
        date_str="2026-02-26",
        feed_slug="levine",
        store=store,
    )

    # A pending job should exist (not yet due).
    rows = store._conn.execute(
        "SELECT * FROM pending_things_happen WHERE status = 'pending'"
    ).fetchall()
    assert len(rows) == 1
    links = json.loads(rows[0]["links_json"])
    assert len(links) == 2
    assert links[0]["link_text"] == "Blue Owl"
    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_processor.py::test_levine_email_creates_pending_things_happen_job -v`
Expected: FAIL — `cannot import name '_maybe_queue_things_happen'`

**Step 3: Write the implementation**

Add to `pipeline/processor.py`:

```python
from pipeline.things_happen_extractor import extract_things_happen

def _maybe_queue_things_happen(
    *,
    raw_email_html: str,
    source_r2_key: str,
    date_str: str,
    feed_slug: str,
    store: StateStore,
) -> None:
    """If this is a Levine email, extract Things Happen links and queue a job."""
    if feed_slug != "levine":
        return
    links = extract_things_happen(raw_email_html)
    if not links:
        return
    import json
    links_json = json.dumps(
        [
            {
                "link_text": link.link_text,
                "raw_url": link.raw_url,
                "headline_context": link.headline_context,
            }
            for link in links
        ]
    )
    store.insert_pending_things_happen(
        email_r2_key=source_r2_key,
        date_str=date_str,
        links_json=links_json,
    )
```

Then call this function from `process_email_bytes()`, after the email is parsed but before the main processing is complete. Add it just before `store.mark_processed(source_r2_key)`:

```python
    # Queue Things Happen job if this is a Levine email.
    html_content = EmailProcessor(raw_email)._extract_html_part()
    _maybe_queue_things_happen(
        raw_email_html=html_content,
        source_r2_key=source_r2_key,
        date_str=date_str,
        feed_slug=preset.feed_slug,
        store=store,
    )
```

Note: `_extract_html_part()` is a private method on `EmailProcessor`. It parses the raw email and returns the HTML part. This avoids re-parsing the email from bytes.

**Step 4: Run tests**

Run: `uv run pytest -v`
Expected: All tests pass.

**Step 5: Commit**

```bash
git add pipeline/processor.py pipeline/test_things_happen_processor.py
git commit -m "feat: queue Things Happen jobs when processing Levine emails"
```

---

### Task 9: Consumer loop integration

**Files:**
- Modify: `pipeline/consumer.py`
- Modify: `pipeline/__main__.py`

Add a check in the consumer loop for due Things Happen jobs.

**Step 1: Modify the consumer loop**

In `pipeline/consumer.py`, add to `consume_forever()` after the queue polling/ack block (after line 136), before the `time.sleep`:

```python
from pipeline.things_happen_processor import process_things_happen_job

# Inside consume_forever, after the queue processing block:
# Process any due Things Happen jobs.
try:
    due_jobs = store.list_due_things_happen()
    for job in due_jobs:
        try:
            print(f"Processing Things Happen job: {job['id']} ({job['date_str']})")
            process_things_happen_job(job, store, r2_client)
            print(f"Completed Things Happen job: {job['id']}")
        except Exception as exc:
            print(f"Failed Things Happen job {job['id']}: {exc}")
except Exception as exc:
    print(f"Error checking Things Happen jobs: {exc}")
```

The modified `consume_forever` loop should look like:

```python
def consume_forever(
    store: StateStore,
    r2_client: R2Client,
    poll_interval: int = 10,
) -> None:
    consumer = CloudflareQueueConsumer()

    while True:
        messages = consumer.pull(batch_size=5)

        if messages:
            ack_messages: list[QueueMessage] = []
            for message in messages:
                if store.is_processed(message.key):
                    ack_messages.append(message)
                    continue
                try:
                    process_r2_email_key(message.key, message.route_tag, store, r2_client)
                except Exception as exc:
                    print(f"Failed processing {message.key}: {exc}")
                    continue
                ack_messages.append(message)
            consumer.ack(ack_messages)

        # Process any due Things Happen jobs.
        try:
            due_jobs = store.list_due_things_happen()
            for job in due_jobs:
                try:
                    print(f"Processing Things Happen job: {job['id']} ({job['date_str']})")
                    process_things_happen_job(job, store, r2_client)
                    print(f"Completed Things Happen job: {job['id']}")
                except Exception as exc:
                    print(f"Failed Things Happen job {job['id']}: {exc}")
        except Exception as exc:
            print(f"Error checking Things Happen jobs: {exc}")

        if not messages:
            time.sleep(poll_interval)
```

**Step 2: Run tests**

Run: `uv run pytest -v`
Expected: All tests pass.

**Step 3: Commit**

```bash
git add pipeline/consumer.py
git commit -m "feat: check for due Things Happen jobs in consumer loop"
```

---

### Task 10: End-to-end verification and push

**Step 1: Run the full test suite**

```bash
uv run pytest -v
```

Expected: All tests pass.

**Step 2: Verify the consumer service can still start**

```bash
sudo systemctl restart my-podcasts-consumer
sudo systemctl status my-podcasts-consumer --no-pager
```

Expected: Service is active/running.

**Step 3: Verify existing feeds still work**

```bash
curl -sI https://podcast.mohrbacher.dev/feed.xml | head -3
curl -sI https://podcast.mohrbacher.dev/feeds/levine.xml | head -3
curl -sI https://podcast.mohrbacher.dev/feeds/yglesias.xml | head -3
curl -sI https://podcast.mohrbacher.dev/cover-things-happen.jpg | head -3
```

**Step 4: Push**

```bash
git push
```

**Note:** The `feeds/things-happen.xml` feed will be auto-created when the first Things Happen episode is processed (24 hours after the next Levine email arrives).

**Note:** archive.today may aggressively block automated requests (429 responses). If this happens consistently during initial testing, the fetcher will gracefully fall back to live URL or headline-only tiers. Improving archive.today reliability (e.g., via headless browser or delayed retries) is a future iteration.
