# Source Caching and Catch-Up Data Gathering Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add persistent daily caches for antiwar homepage, antiwar RSS, and Semafor sources, with an adaptive lookback window based on the last episode date, so both podcasts catch up on missed days.

**Architecture:** Three new cache sync functions in `source_cache.py` follow the `zvi_cache.py` pattern (fetch, deduplicate by filename, write markdown files). A new `sync-sources` CLI command and systemd timer run them daily. Both collectors switch from live fetches to reading cached files filtered by lookback days. `days_since_last_episode()` in `db.py` drives the adaptive window.

**Tech Stack:** Python, feedparser, trafilatura, requests, sqlite3, systemd

---

### Task 1: Add `days_since_last_episode` to StateStore

**Files:**
- Modify: `pipeline/db.py`
- Create: `pipeline/test_db_lookback.py`

**Step 1: Write the failing test**

Add `pipeline/test_db_lookback.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pipeline.db import StateStore


def test_days_since_last_episode_with_episodes(tmp_path):
    """Returns correct days since last episode."""
    store = StateStore(tmp_path / "test.db")
    # Insert a fake episode 3 days ago
    three_days_ago = (datetime.now(tz=UTC) - timedelta(days=3)).strftime("%Y-%m-%d")
    store._conn.execute(
        "INSERT INTO episodes (id, title, slug, pub_date, r2_key, feed_slug, category, preset_name, size_bytes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ep1", "Test Episode", "test", three_days_ago, "key", "things-happen", "News", "General", 1000),
    )
    store._conn.commit()

    result = store.days_since_last_episode("things-happen")
    assert result == 3


def test_days_since_last_episode_no_episodes(tmp_path):
    """Returns None when no episodes exist for feed."""
    store = StateStore(tmp_path / "test.db")
    result = store.days_since_last_episode("things-happen")
    assert result is None


def test_days_since_last_episode_different_feed(tmp_path):
    """Only considers episodes for the requested feed_slug."""
    store = StateStore(tmp_path / "test.db")
    yesterday = (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    store._conn.execute(
        "INSERT INTO episodes (id, title, slug, pub_date, r2_key, feed_slug, category, preset_name, size_bytes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ep1", "FP Episode", "fp", yesterday, "key", "fp-digest", "News", "General", 1000),
    )
    store._conn.commit()

    # things-happen has no episodes
    assert store.days_since_last_episode("things-happen") is None
    # fp-digest has one from yesterday
    assert store.days_since_last_episode("fp-digest") == 1
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_db_lookback.py -v`
Expected: FAIL (method doesn't exist)

**Step 3: Write implementation**

Add to `pipeline/db.py` in the `StateStore` class (after `list_feed_slugs`):

```python
def days_since_last_episode(self, feed_slug: str) -> int | None:
    """Return the number of days since the last episode for a feed, or None if no episodes exist."""
    row = self._conn.execute(
        "SELECT MAX(pub_date) as latest FROM episodes WHERE feed_slug = ?",
        (feed_slug,),
    ).fetchone()
    if not row or not row["latest"]:
        return None
    latest = datetime.strptime(row["latest"], "%Y-%m-%d").replace(tzinfo=UTC)
    now = datetime.now(tz=UTC)
    return (now - latest).days
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_db_lookback.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/db.py pipeline/test_db_lookback.py
git -c commit.gpgsign=false commit -m "feat: add days_since_last_episode to StateStore"
```

---

### Task 2: Create `source_cache.py` with `sync_semafor_cache`

**Files:**
- Create: `pipeline/source_cache.py`
- Create: `pipeline/test_source_cache.py`

**Step 1: Write the failing tests**

Add `pipeline/test_source_cache.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from pipeline.source_cache import sync_semafor_cache


def _make_semafor_entry(title: str, category: str, published_str: str = "2026-03-07") -> dict:
    """Create a fake feedparser entry for Semafor."""
    import feedparser
    dt = datetime.strptime(published_str, "%Y-%m-%d").replace(tzinfo=UTC)
    st = dt.timetuple()
    return feedparser.FeedParserDict({
        "title": title,
        "link": f"https://semafor.com/{title.lower().replace(' ', '-')[:30]}",
        "published_parsed": st,
        "summary": f"Summary of {title}",
        "tags": [feedparser.FeedParserDict({"term": category})],
        "content": [{"value": f"<p>Full content about {title}.</p>"}],
    })


def test_sync_semafor_creates_files(tmp_path):
    """Semafor articles are cached as markdown files with category metadata."""
    entry = _make_semafor_entry("Tech Company IPO", "Technology")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        new_files = sync_semafor_cache(tmp_path)

    assert len(new_files) == 1
    content = new_files[0].read_text()
    assert "# Tech Company IPO" in content
    assert "Category: Technology" in content
    assert "Source: semafor" in content


def test_sync_semafor_is_idempotent(tmp_path):
    """Running sync twice does not create duplicates."""
    entry = _make_semafor_entry("Tech Story", "Business")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        first = sync_semafor_cache(tmp_path)
        second = sync_semafor_cache(tmp_path)

    assert len(first) == 1
    assert len(second) == 0


def test_sync_semafor_handles_fetch_failure(tmp_path):
    """If RSS fetch fails, return empty list."""
    with patch("pipeline.source_cache.fetch_feed", side_effect=Exception("timeout")):
        result = sync_semafor_cache(tmp_path)
    assert result == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_source_cache.py -v`
Expected: FAIL (module doesn't exist)

**Step 3: Write implementation**

Create `pipeline/source_cache.py`:

```python
from __future__ import annotations

import html as html_mod
import re
from datetime import UTC, datetime
from pathlib import Path

import trafilatura

from pipeline.rss_sources import SEMAFOR, FP_DIGEST_RSS_SOURCES, fetch_feed

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _slugify(text: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:50]


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode entities."""
    return html_mod.unescape(_HTML_TAG_RE.sub("", html)).strip()


def _parse_publish_date(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime(*st[:6], tzinfo=UTC)
    return None


def sync_semafor_cache(cache_dir: Path) -> list[Path]:
    """Fetch Semafor RSS and cache all articles. Returns newly-written paths."""
    try:
        feed = fetch_feed(SEMAFOR.feed_url)
    except Exception as e:
        print(f"[source_cache] Failed to fetch Semafor RSS: {e}")
        return []

    cache_dir.mkdir(parents=True, exist_ok=True)
    new_files: list[Path] = []

    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue

        pub_date = _parse_publish_date(entry)
        date_str = pub_date.strftime("%Y-%m-%d") if pub_date else "unknown"
        url = (entry.get("link") or "").strip()

        category = ""
        if entry.get("tags"):
            category = entry["tags"][0].get("term", "")

        content_html = (
            entry.get("content", [{}])[0].get("value", "")
            if entry.get("content")
            else ""
        )
        text = _strip_html(content_html) if content_html else (entry.get("summary") or "").strip()

        slug = _slugify(title)
        filename = f"{date_str}-{slug}.md"
        path = cache_dir / filename
        if path.exists():
            continue

        md = (
            f"# {title}\n\n"
            f"URL: {url}\n"
            f"Published: {date_str}\n"
            f"Source: semafor\n"
            f"Category: {category}\n"
            f"Type: article\n\n"
            f"{text}"
        )
        path.write_text(md, encoding="utf-8")
        new_files.append(path)

    return new_files
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_source_cache.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/source_cache.py pipeline/test_source_cache.py
git -c commit.gpgsign=false commit -m "feat: add source_cache module with sync_semafor_cache"
```

---

### Task 3: Add `sync_antiwar_rss_cache` to `source_cache.py`

**Files:**
- Modify: `pipeline/source_cache.py`
- Modify: `pipeline/test_source_cache.py`

**Step 1: Write the failing tests**

Add to `pipeline/test_source_cache.py`:

```python
from pipeline.source_cache import sync_antiwar_rss_cache


def _make_rss_entry(title: str, source_url: str, published_str: str = "2026-03-07") -> dict:
    """Create a fake feedparser entry for antiwar/CJ RSS."""
    dt = datetime.strptime(published_str, "%Y-%m-%d").replace(tzinfo=UTC)
    st = dt.timetuple()
    return {
        "title": title,
        "link": source_url,
        "published_parsed": st,
        "summary": f"Summary of {title}",
        "content": [{"value": f"<p>Full content about {title}.</p>"}],
    }


def test_sync_antiwar_rss_creates_files(tmp_path):
    """Antiwar RSS articles cached with source metadata."""
    entry = _make_rss_entry("US Troops Deploy", "https://news.antiwar.com/story1")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        new_files = sync_antiwar_rss_cache(tmp_path)

    assert len(new_files) >= 1
    content = new_files[0].read_text()
    assert "# US Troops Deploy" in content
    assert "Source: antiwar_news" in content


def test_sync_antiwar_rss_is_idempotent(tmp_path):
    """Running sync twice does not create duplicates."""
    entry = _make_rss_entry("Troops Story", "https://news.antiwar.com/story1")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        first = sync_antiwar_rss_cache(tmp_path)
        second = sync_antiwar_rss_cache(tmp_path)

    assert len(first) >= 1
    assert len(second) == 0
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_source_cache.py -k antiwar_rss -v`
Expected: FAIL (function doesn't exist)

**Step 3: Write implementation**

Add to `pipeline/source_cache.py`:

```python
def sync_antiwar_rss_cache(cache_dir: Path) -> list[Path]:
    """Fetch all FP Digest RSS feeds and cache articles. Returns newly-written paths."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    new_files: list[Path] = []

    for source in FP_DIGEST_RSS_SOURCES:
        try:
            feed = fetch_feed(source.feed_url)
        except Exception as e:
            print(f"[source_cache] Failed to fetch {source.name} RSS: {e}")
            continue

        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            if not title:
                continue

            pub_date = _parse_publish_date(entry)
            date_str = pub_date.strftime("%Y-%m-%d") if pub_date else "unknown"
            url = (entry.get("link") or "").strip()

            content_html = (
                entry.get("content", [{}])[0].get("value", "")
                if entry.get("content")
                else ""
            )
            summary = (entry.get("summary") or "").strip()
            text = _strip_html(content_html) if content_html else summary

            slug = _slugify(title)
            filename = f"{date_str}-{source.name}-{slug}.md"
            path = cache_dir / filename
            if path.exists():
                continue

            md = (
                f"# {title}\n\n"
                f"URL: {url}\n"
                f"Published: {date_str}\n"
                f"Source: {source.name}\n"
                f"Type: article\n\n"
                f"{text}"
            )
            path.write_text(md, encoding="utf-8")
            new_files.append(path)

    return new_files
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_source_cache.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/source_cache.py pipeline/test_source_cache.py
git -c commit.gpgsign=false commit -m "feat: add sync_antiwar_rss_cache for persistent RSS caching"
```

---

### Task 4: Add `sync_antiwar_homepage_cache` to `source_cache.py`

**Files:**
- Modify: `pipeline/source_cache.py`
- Modify: `pipeline/test_source_cache.py`

**Step 1: Write the failing tests**

Add to `pipeline/test_source_cache.py`:

```python
from pipeline.source_cache import sync_antiwar_homepage_cache
from pipeline.fp_homepage_scraper import HomepageLink


def test_sync_homepage_creates_files(tmp_path):
    """Homepage links cached with region metadata."""
    links = [
        HomepageLink(region="Middle East", headline="Iran Tensions Rise", url="https://example.com/iran"),
        HomepageLink(region="Europe", headline="NATO Expands", url="https://example.com/nato"),
    ]

    with (
        patch("pipeline.source_cache.scrape_homepage", return_value=links),
        patch("pipeline.source_cache._extract_homepage_text", return_value="Article body text."),
    ):
        new_files = sync_antiwar_homepage_cache(tmp_path)

    assert len(new_files) == 2
    content = new_files[0].read_text()
    assert "Region:" in content
    assert "Source: antiwar-homepage" in content


def test_sync_homepage_is_idempotent(tmp_path):
    """Running sync twice does not create duplicates."""
    links = [HomepageLink(region="Africa", headline="Story One", url="https://example.com/1")]

    with (
        patch("pipeline.source_cache.scrape_homepage", return_value=links),
        patch("pipeline.source_cache._extract_homepage_text", return_value="Text."),
    ):
        first = sync_antiwar_homepage_cache(tmp_path)
        second = sync_antiwar_homepage_cache(tmp_path)

    assert len(first) == 1
    assert len(second) == 0


def test_sync_homepage_handles_scrape_failure(tmp_path):
    """If homepage scrape fails, return empty list."""
    with patch("pipeline.source_cache.scrape_homepage", side_effect=Exception("timeout")):
        result = sync_antiwar_homepage_cache(tmp_path)
    assert result == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_source_cache.py -k homepage -v`
Expected: FAIL (function doesn't exist)

**Step 3: Write implementation**

Add to `pipeline/source_cache.py`:

```python
from pipeline.fp_homepage_scraper import scrape_homepage


def _extract_homepage_text(url: str) -> str:
    """Fetch and extract article text from a URL."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            return (text or "").strip()
    except Exception:
        pass
    return ""


def sync_antiwar_homepage_cache(cache_dir: Path) -> list[Path]:
    """Scrape antiwar.com homepage and cache all linked articles. Returns newly-written paths."""
    try:
        links = scrape_homepage()
    except Exception as e:
        print(f"[source_cache] Failed to scrape antiwar homepage: {e}")
        return []

    cache_dir.mkdir(parents=True, exist_ok=True)
    new_files: list[Path] = []
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    for link in links:
        slug = _slugify(link.headline)
        filename = f"{today}-{slug}.md"
        path = cache_dir / filename
        if path.exists():
            continue

        text = _extract_homepage_text(link.url)

        md = (
            f"# {link.headline}\n\n"
            f"URL: {link.url}\n"
            f"Published: {today}\n"
            f"Region: {link.region}\n"
            f"Source: antiwar-homepage\n"
            f"Type: article\n\n"
            f"{text}"
        )
        path.write_text(md, encoding="utf-8")
        new_files.append(path)

    return new_files
```

Note: Homepage links don't have a publication date in the HTML, so we use today's date. The filename deduplication prevents re-caching the same headline on the same day.

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_source_cache.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/source_cache.py pipeline/test_source_cache.py
git -c commit.gpgsign=false commit -m "feat: add sync_antiwar_homepage_cache for persistent homepage caching"
```

---

### Task 5: Add `sync-sources` CLI command

**Files:**
- Modify: `pipeline/__main__.py`
- Modify: `pipeline/test_source_cache.py` (add CLI test)

**Step 1: Write the failing test**

Add to `pipeline/test_source_cache.py`:

```python
from unittest.mock import call
from click.testing import CliRunner
from pipeline.__main__ import cli


def test_sync_sources_cli(tmp_path):
    """The sync-sources command calls all four sync functions."""
    with (
        patch("pipeline.__main__.sync_zvi_cache") as mock_zvi,
        patch("pipeline.__main__.sync_semafor_cache") as mock_semafor,
        patch("pipeline.__main__.sync_antiwar_rss_cache") as mock_rss,
        patch("pipeline.__main__.sync_antiwar_homepage_cache") as mock_homepage,
    ):
        mock_zvi.return_value = []
        mock_semafor.return_value = []
        mock_rss.return_value = []
        mock_homepage.return_value = []

        runner = CliRunner()
        result = runner.invoke(cli, ["sync-sources"])

    assert result.exit_code == 0
    mock_zvi.assert_called_once()
    mock_semafor.assert_called_once()
    mock_rss.assert_called_once()
    mock_homepage.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_source_cache.py::test_sync_sources_cli -v`
Expected: FAIL (no such command)

**Step 3: Write implementation**

Add to `pipeline/__main__.py`:

```python
from pipeline.source_cache import (
    sync_antiwar_homepage_cache,
    sync_antiwar_rss_cache,
    sync_semafor_cache,
)
from pipeline.zvi_cache import sync_zvi_cache
```

And the command:

```python
@cli.command("sync-sources")
def sync_sources_command() -> None:
    """Sync all source caches (Zvi, Semafor, Antiwar RSS, Antiwar homepage)."""
    from pathlib import Path

    caches = [
        ("Zvi", sync_zvi_cache, Path("/persist/my-podcasts/zvi-cache")),
        ("Semafor", sync_semafor_cache, Path("/persist/my-podcasts/semafor-cache")),
        ("Antiwar RSS", sync_antiwar_rss_cache, Path("/persist/my-podcasts/antiwar-rss-cache")),
        ("Antiwar Homepage", sync_antiwar_homepage_cache, Path("/persist/my-podcasts/antiwar-homepage-cache")),
    ]

    for name, sync_fn, cache_dir in caches:
        print(f"Syncing {name}...")
        try:
            new_files = sync_fn(cache_dir)
            print(f"  {name}: {len(new_files)} new files cached")
        except Exception as e:
            print(f"  {name}: FAILED - {e}")
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_source_cache.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/__main__.py pipeline/test_source_cache.py
git -c commit.gpgsign=false commit -m "feat: add sync-sources CLI command"
```

---

### Task 6: Switch FP Digest collector to read from caches

**Files:**
- Modify: `pipeline/fp_collector.py`
- Modify: `pipeline/test_fp_collector.py`

**Step 1: Write the failing test**

Add to `pipeline/test_fp_collector.py`:

```python
def test_fp_collector_reads_from_caches(tmp_path, monkeypatch):
    """FP collector reads from cache directories instead of fetching live."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # Mock only the non-cache external calls
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: f"Text for {url}")
    monkeypatch.setattr("pipeline.fp_collector.generate_fp_research_plan", lambda *a, **kw: _make_empty_plan())
    monkeypatch.setattr("pipeline.fp_collector.search_related", lambda *a, **kw: [])

    et = ZoneInfo("America/New_York")
    today = datetime.now(tz=et).strftime("%Y-%m-%d")

    # Pre-populate homepage cache
    homepage_cache = tmp_path / "homepage-cache"
    homepage_cache.mkdir()
    (homepage_cache / f"{today}-iran-tensions.md").write_text(
        f"# Iran Tensions\n\nURL: https://example.com/iran\nPublished: {today}\nRegion: Middle East\nSource: antiwar-homepage\nType: article\n\nIran article text."
    )

    # Pre-populate RSS cache
    rss_cache = tmp_path / "rss-cache"
    rss_cache.mkdir()
    (rss_cache / f"{today}-antiwar_news-troops-deploy.md").write_text(
        f"# Troops Deploy\n\nURL: https://antiwar.com/troops\nPublished: {today}\nSource: antiwar_news\nType: article\n\nTroops article text."
    )

    # Pre-populate Semafor cache
    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    (semafor_cache / f"{today}-gulf-crisis.md").write_text(
        f"# Gulf Crisis\n\nURL: https://semafor.com/gulf\nPublished: {today}\nSource: semafor\nCategory: Gulf\nType: article\n\nGulf article text."
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_fp_artifacts(
        "test-job", work_dir,
        scripts_source_dir=scripts_dir,
        homepage_cache_dir=homepage_cache,
        antiwar_rss_cache_dir=rss_cache,
        semafor_cache_dir=semafor_cache,
        lookback_days=2,
    )

    # Homepage articles should appear
    homepage_dir = work_dir / "articles" / "homepage"
    assert homepage_dir.exists()

    # RSS articles should appear
    rss_dir = work_dir / "articles" / "rss"
    assert rss_dir.exists()

    # Semafor FP articles should appear (Gulf is FP category)
    semafor_dir = work_dir / "articles" / "semafor"
    assert semafor_dir.exists()
    semafor_files = list(semafor_dir.glob("*.md"))
    assert len(semafor_files) == 1
    assert "Gulf Crisis" in semafor_files[0].read_text()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_fp_collector.py::test_fp_collector_reads_from_caches -v`
Expected: FAIL

**Step 3: Write implementation**

Modify `pipeline/fp_collector.py`:

1. Add imports:
   ```python
   from zoneinfo import ZoneInfo
   from pipeline.rss_sources import categorize_semafor_article
   ```

2. Add new parameters to `collect_fp_artifacts`:
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
   ) -> None:
   ```

3. Compute the lookback date window (after directory creation):
   ```python
   _et = ZoneInfo("America/New_York")
   lookback_dates = set()
   for i in range(lookback_days):
       d = (datetime.now(tz=_et) - timedelta(days=i)).strftime("%Y-%m-%d")
       lookback_dates.add(d)
   ```

4. Helper to check if a cached file is within the lookback window:
   ```python
   def _in_window(filename: str) -> bool:
       return any(filename.startswith(d) for d in lookback_dates)
   ```

5. Replace Phase 1 (homepage scraping) with cache read:
   ```python
   # Phase 1: Homepage articles from cache
   hp_cache = homepage_cache_dir or Path("/persist/my-podcasts/antiwar-homepage-cache")
   homepage_urls: set[str] = set()
   if hp_cache.exists():
       for cached in hp_cache.glob("*.md"):
           if not _in_window(cached.name):
               continue
           text = cached.read_text(encoding="utf-8")
           # Parse metadata
           lines = text.split("\n")
           headline = lines[0].lstrip("# ").strip()
           url = ""
           region = ""
           for line in lines[1:6]:
               if line.startswith("URL: "):
                   url = line[5:].strip()
               elif line.startswith("Region: "):
                   region = line[8:].strip()
           homepage_urls.add(url)
           region_slug = _slugify(region) if region else "unknown"
           region_dir = articles_homepage_dir / region_slug
           region_dir.mkdir(parents=True, exist_ok=True)
           slug = _slugify(headline)
           art_path = region_dir / f"{slug}.md"
           if not art_path.exists():
               art_path.write_text(text, encoding="utf-8")
   ```

6. Replace Phase 2 (RSS fetch) with cache read:
   ```python
   # Phase 2: RSS articles from cache
   rss_cache = antiwar_rss_cache_dir or Path("/persist/my-podcasts/antiwar-rss-cache")
   rss_articles_data: list[dict] = []
   if rss_cache.exists():
       for cached in rss_cache.glob("*.md"):
           if not _in_window(cached.name):
               continue
           text = cached.read_text(encoding="utf-8")
           lines = text.split("\n")
           headline = lines[0].lstrip("# ").strip()
           url = ""
           source = ""
           for line in lines[1:6]:
               if line.startswith("URL: "):
                   url = line[5:].strip()
               elif line.startswith("Source: "):
                   source = line[8:].strip()
           if url in homepage_urls:
               continue
           parts = text.split("\n\n", 2)
           body = parts[2] if len(parts) > 2 else ""
           rss_articles_data.append({"headline": headline, "url": url, "source": source, "text": body, "summary": body[:300]})
           source_dir = articles_rss_dir / source
           source_dir.mkdir(parents=True, exist_ok=True)
           slug = _slugify(headline)
           art_path = source_dir / f"{slug}.md"
           if not art_path.exists():
               art_path.write_text(text, encoding="utf-8")
   ```

7. Replace Phase 2b (routed links) — fix timezone to Eastern and use lookback window:
   ```python
   # Phase 2b: Routed links (use lookback window with Eastern time)
   # ... same logic but replace today/yesterday with lookback_dates
   if routed_links_dir.exists():
       for routed_path in sorted(routed_links_dir.glob("*.json")):
           fname = routed_path.stem
           if not _in_window(fname):
               continue
           # ... rest of logic unchanged
   ```

8. Replace Phase 2c (Semafor FP articles) with cache read:
   ```python
   # Phase 2c: Semafor FP articles from cache
   sem_cache = semafor_cache_dir or Path("/persist/my-podcasts/semafor-cache")
   if sem_cache.exists():
       for cached in sem_cache.glob("*.md"):
           if not _in_window(cached.name):
               continue
           text = cached.read_text(encoding="utf-8")
           lines = text.split("\n")
           headline = lines[0].lstrip("# ").strip()
           category = ""
           url = ""
           for line in lines[1:8]:
               if line.startswith("Category: "):
                   category = line[10:].strip()
               elif line.startswith("URL: "):
                   url = line[5:].strip()
           routing = categorize_semafor_article(category)
           if routing not in ("fp", "both"):
               continue
           if url in homepage_urls:
               continue
           slug = _slugify(headline)
           art_path = articles_semafor_dir / f"{slug}.md"
           if not art_path.exists():
               art_path.write_text(text, encoding="utf-8")
   ```

9. Remove or keep `_fetch_rss_articles` and `_fetch_semafor_fp_articles` as dead code — they are no longer called by `collect_fp_artifacts`. Remove the imports of `scrape_homepage` (it is now only used in `source_cache.py`). Removing the old functions can be done here or in a separate cleanup task.

10. Update Phase 4 (headlines building) to use `rss_articles_data` instead of `rss_articles` (the variable returned by the old `_fetch_rss_articles`).

11. Remove Phase 7 (fetch full text for short RSS articles) — the cache already has the full text.

12. Update existing tests to pass cache dir parameters and mock appropriately. The existing `test_collect_fp_artifacts` test patches `scrape_homepage`, `_fetch_rss_articles`, and `_fetch_semafor_fp_articles` — these mocks should be replaced with pre-populated cache directories.

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_fp_collector.py -v`
Expected: ALL PASS

**Step 5: Run lint**

Run: `uv run ruff check pipeline/fp_collector.py pipeline/test_fp_collector.py`

**Step 6: Commit**

```bash
git add pipeline/fp_collector.py pipeline/test_fp_collector.py
git -c commit.gpgsign=false commit -m "feat: switch FP collector to read from persistent caches with lookback window"
```

---

### Task 7: Switch Things Happen collector to read Semafor from cache

**Files:**
- Modify: `pipeline/things_happen_collector.py`
- Modify: `pipeline/test_things_happen_collector.py`

**Step 1: Write the failing test**

Add to `pipeline/test_things_happen_collector.py`:

```python
def test_semafor_reads_from_cache(tmp_path, monkeypatch):
    """Semafor TH articles are read from cache with lookback window."""
    monkeypatch.setattr("pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: [])
    monkeypatch.setattr("pipeline.things_happen_collector.resolve_redirect_url", lambda u: u)
    monkeypatch.setattr("pipeline.things_happen_collector.generate_research_plan", lambda *a, **kw: [])
    monkeypatch.setattr("pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: [])

    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    today = datetime.now(tz=et).strftime("%Y-%m-%d")

    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()
    # TH category article (should be included)
    (semafor_cache / f"{today}-tech-ipo.md").write_text(
        f"# Tech IPO\n\nURL: https://semafor.com/tech\nPublished: {today}\nSource: semafor\nCategory: Technology\nType: article\n\nTech article."
    )
    # FP category article (should be excluded)
    (semafor_cache / f"{today}-gulf-crisis.md").write_text(
        f"# Gulf Crisis\n\nURL: https://semafor.com/gulf\nPublished: {today}\nSource: semafor\nCategory: Gulf\nType: article\n\nGulf article."
    )
    # Old article outside lookback (should be excluded)
    (semafor_cache / "2025-01-01-old-story.md").write_text(
        "# Old Story\n\nURL: https://semafor.com/old\nPublished: 2025-01-01\nSource: semafor\nCategory: Technology\nType: article\n\nOld."
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_all_artifacts(
        "test-job", [], work_dir,
        scripts_source_dir=scripts_dir,
        zvi_cache_dir=tmp_path / "empty-zvi",
        semafor_cache_dir=semafor_cache,
        lookback_days=2,
    )

    semafor_dir = work_dir / "articles" / "semafor"
    assert semafor_dir.exists()
    semafor_files = list(semafor_dir.glob("*.md"))
    assert len(semafor_files) == 1
    assert "Tech IPO" in semafor_files[0].read_text()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_collector.py::test_semafor_reads_from_cache -v`
Expected: FAIL

**Step 3: Write implementation**

Modify `pipeline/things_happen_collector.py`:

1. Add imports:
   ```python
   from pipeline.rss_sources import categorize_semafor_article
   ```
   (Remove `SEMAFOR`, `fetch_feed` from imports if no longer used.)

2. Add new parameters to `collect_all_artifacts`:
   ```python
   def collect_all_artifacts(
       job_id: str,
       links_raw: list[dict],
       work_dir: Path,
       scripts_source_dir: Path | None = None,
       fp_routed_dir: Path | None = None,
       zvi_cache_dir: Path | None = None,
       semafor_cache_dir: Path | None = None,
       lookback_days: int = 2,
   ) -> None:
   ```

3. Compute lookback date set (using Eastern time, consistent with Zvi):
   ```python
   _et = ZoneInfo("America/New_York")
   lookback_dates = set()
   for i in range(lookback_days):
       d = (datetime.now(tz=_et) - timedelta(days=i)).strftime("%Y-%m-%d")
       lookback_dates.add(d)
   ```

4. Replace Phase 1b (Semafor live fetch) with cache read:
   ```python
   # Phase 1b: Semafor articles from cache (TH categories)
   semafor_dir = articles_dir / "semafor"
   semafor_dir.mkdir(parents=True, exist_ok=True)
   sem_cache = semafor_cache_dir or Path("/persist/my-podcasts/semafor-cache")
   if sem_cache.exists():
       for cached in sem_cache.glob("*.md"):
           if not any(cached.name.startswith(d) for d in lookback_dates):
               continue
           text = cached.read_text(encoding="utf-8")
           lines = text.split("\n")
           headline = lines[0].lstrip("# ").strip()
           category = ""
           for line in lines[1:8]:
               if line.startswith("Category: "):
                   category = line[10:].strip()
           routing = categorize_semafor_article(category)
           if routing not in ("th", "both"):
               continue
           slug = _slugify(headline)
           art_path = semafor_dir / f"{slug}.md"
           if not art_path.exists():
               art_path.write_text(text, encoding="utf-8")
   ```

5. Update Phase 1c (Zvi) to use `lookback_dates` instead of just today/yesterday:
   ```python
   for cached_file in zvi_cache.glob("*.md"):
       if any(cached_file.name.startswith(d) for d in lookback_dates):
           target = zvi_dir / cached_file.name
           if not target.exists():
               target.write_text(cached_file.read_text(encoding="utf-8"), encoding="utf-8")
   ```

6. Remove `_fetch_semafor_articles` function and its import of `SEMAFOR`/`fetch_feed` (if no longer needed).

7. Update existing tests to pass `semafor_cache_dir` and `lookback_days` where needed, removing mocks for `_fetch_semafor_articles`.

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_collector.py -v`
Expected: ALL PASS

**Step 5: Run lint**

Run: `uv run ruff check pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py`

**Step 6: Commit**

```bash
git add pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py
git -c commit.gpgsign=false commit -m "feat: switch TH collector to Semafor cache, expand Zvi/Semafor to lookback window"
```

---

### Task 8: Wire consumer with lookback and cache dirs

**Files:**
- Modify: `pipeline/consumer.py`
- Modify: `pipeline/test_consumer.py`

**Step 1: Write implementation**

Modify `pipeline/consumer.py`:

1. Add import:
   ```python
   from pipeline.db import StateStore
   ```
   (StateStore is already available via the `store` parameter.)

2. Add a helper function:
   ```python
   def _compute_lookback(store: StateStore, feed_slug: str, default: int = 2, cap: int = 14) -> int:
       days = store.days_since_last_episode(feed_slug)
       if days is None:
           return default
       return min(max(2, days + 1), cap)
   ```

3. Update the `collect_all_artifacts` call (around line 304) to pass lookback and semafor cache:
   ```python
   lookback = _compute_lookback(store, "things-happen")
   collect_all_artifacts(
       job["id"],
       links_raw,
       work_dir,
       fp_routed_dir=Path("/persist/my-podcasts/fp-routed-links"),
       zvi_cache_dir=Path("/persist/my-podcasts/zvi-cache"),
       semafor_cache_dir=Path("/persist/my-podcasts/semafor-cache"),
       lookback_days=lookback,
   )
   ```

4. Update the `collect_fp_artifacts` call (if done via consumer — check if FP digest goes through consumer or CLI). Looking at the code: FP digest runs via both consumer (lines 312-393) and CLI (`__main__.py`). Update the consumer's `collect_fp_artifacts` call:
   ```python
   fp_lookback = _compute_lookback(store, "fp-digest")
   collect_fp_artifacts(
       job["id"],
       work_dir,
       fp_routed_dir=Path("/persist/my-podcasts/fp-routed-links"),
       homepage_cache_dir=Path("/persist/my-podcasts/antiwar-homepage-cache"),
       antiwar_rss_cache_dir=Path("/persist/my-podcasts/antiwar-rss-cache"),
       semafor_cache_dir=Path("/persist/my-podcasts/semafor-cache"),
       lookback_days=fp_lookback,
   )
   ```

5. Add three new cache cleanups in `_cleanup_old_work_dirs`:
   ```python
   for cache_name, cache_path in [
       ("Semafor cache", Path("/persist/my-podcasts/semafor-cache")),
       ("Antiwar RSS cache", Path("/persist/my-podcasts/antiwar-rss-cache")),
       ("Antiwar homepage cache", Path("/persist/my-podcasts/antiwar-homepage-cache")),
   ]:
       if cache_path.exists():
           for f in cache_path.glob("*.md"):
               try:
                   mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
                   if mtime < cutoff:
                       f.unlink()
                       print(f"Cleaned up old {cache_name}: {f.name}")
               except OSError:
                   pass
   ```

**Step 2: Run tests**

Run: `uv run pytest pipeline/test_consumer.py -v`
Expected: ALL PASS

**Step 3: Run full test suite**

Run: `uv run pytest -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add pipeline/consumer.py pipeline/test_consumer.py
git -c commit.gpgsign=false commit -m "feat: wire consumer with adaptive lookback and cache directories"
```

---

### Task 9: Update FP Digest CLI dry-run with cache support

**Files:**
- Modify: `pipeline/__main__.py`

**Step 1: Write implementation**

Update `_fp_digest_dry_run` and `_fp_digest_full_run` in `pipeline/__main__.py` to pass the new cache dir parameters to `collect_fp_artifacts`. The dry run uses defaults (no DB access, `lookback_days=2`). The full run can compute lookback from the store.

Add `--lookback` optional CLI flag to fp-digest for testing:

```python
@cli.command("fp-digest")
@click.option("--date", "date_str", default=None, help="Date to generate for (YYYY-MM-DD)")
@click.option("--dry-run", is_flag=True, help="Run collection only, no DB or TTS")
@click.option("--lookback", "lookback_days", default=None, type=int, help="Override lookback days")
def fp_digest_command(date_str: str | None, dry_run: bool, lookback_days: int | None) -> None:
```

**Step 2: Run tests**

Run: `uv run pytest -v`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add pipeline/__main__.py
git -c commit.gpgsign=false commit -m "feat: update FP Digest CLI with cache support and --lookback flag"
```

---

### Task 10: Add systemd timer for sync-sources

**Files:**
- Modify: `~/projects/workstation/hosts/devbox/configuration.nix`

**Step 1: Add service and timer**

Follow the fp-digest pattern. Add after the fp-digest timer block:

```nix
systemd.services.sync-sources = {
    description = "Sync podcast source caches (Zvi, Semafor, Antiwar)";
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];

    path = [ pkgs.python314 pkgs.uv pkgs.bash pkgs.coreutils ];

    serviceConfig = {
      Type = "oneshot";
      User = "dev";
      Group = "dev";
      WorkingDirectory = "/home/dev/projects/my-podcasts";
      Environment = [
        "HOME=/home/dev"
        "NLTK_DATA=/persist/my-podcasts/nltk_data"
        "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      ];
      ExecStart = "${pkgs.writeShellScript "sync-sources-start" ''
        set -euo pipefail
        export PYTHONUNBUFFERED=1
        cd /home/dev/projects/my-podcasts
        exec ${pkgs.uv}/bin/uv run python -m pipeline sync-sources
      ''}";
      TimeoutStartSec = 300;
    };
  };

  systemd.timers.sync-sources = {
    description = "Sync source caches daily at noon";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* 12:00:00";
      Persistent = true;
      RandomizedDelaySec = "5min";
    };
  };
```

Note: No API keys needed — sync-sources only fetches public RSS feeds and web pages. No `opencode-serve` dependency either.

**Step 2: Deploy**

```bash
cd ~/projects/workstation
git add hosts/devbox/configuration.nix
git -c commit.gpgsign=false commit -m "feat: add sync-sources systemd timer for daily source caching"
sudo nixos-rebuild switch --flake .
```

**Step 3: Verify**

```bash
sudo systemctl status sync-sources.timer --no-pager
sudo systemctl start sync-sources  # test manual run
sudo systemctl status sync-sources --no-pager
```

---

### Task 11: Update AGENTS.md

**Files:**
- Modify: `AGENTS.md`

Update the following sections:

1. **Quick Start**: Add `6. Sync source caches: uv run python -m pipeline sync-sources`

2. **Things Happen Pipeline > Sources**: Add note about Semafor being read from cache with adaptive lookback.

3. **FP Digest Pipeline > Sources**: Add note about all sources being read from persistent caches.

4. Add new section **Source Caching**:
   ```
   ## Source Caching

   All external sources are cached daily to persistent storage by the `sync-sources` timer (noon ET daily).
   Podcasts read from caches instead of fetching live, with an adaptive lookback window based on
   days since the last episode (min 2, max 14 days).

   **Caches:**
   - Zvi: `/persist/my-podcasts/zvi-cache/` (also synced on-demand by Things Happen collector)
   - Semafor: `/persist/my-podcasts/semafor-cache/`
   - Antiwar RSS: `/persist/my-podcasts/antiwar-rss-cache/`
   - Antiwar Homepage: `/persist/my-podcasts/antiwar-homepage-cache/`

   All caches use 180-day retention. Files are markdown with metadata headers (URL, Published, Source, Category/Region).

   **CLI:** `uv run python -m pipeline sync-sources`
   **Timer:** `sync-sources.timer` — daily at noon ET
   ```

5. Add `pipeline/source_cache.py` to Key modules lists.

**Commit:**

```bash
git add AGENTS.md
git -c commit.gpgsign=false commit -m "docs: update AGENTS.md with source caching and adaptive lookback"
```
