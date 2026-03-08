# Zvi Substack Source Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add thezvi.substack.com as a first-class source for Things Happen, with a persistent cache, roundup section splitting, and keyword-based AI enrichment across the full cache.

**Architecture:** New `zvi_cache.py` module handles RSS fetch, post parsing (roundup splitting vs essay), persistent caching, and keyword search. The Things Happen collector calls into it for day-of articles and AI enrichment. The existing `AI_SOURCES` / `search_rss_sources` path for Zvi is removed.

**Tech Stack:** Python, feedparser, trafilatura, requests (via existing `fetch_feed`)

---

### Task 1: Create `zvi_cache.py` with `sync_zvi_cache`

**Files:**
- Create: `pipeline/zvi_cache.py`
- Create: `pipeline/test_zvi_cache.py`

**Step 1: Write the failing test**

Add to `pipeline/test_zvi_cache.py`:

```python
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from pipeline.zvi_cache import sync_zvi_cache


def _make_entry(title: str, html: str, published_str: str = "2026-03-07") -> dict:
    """Create a fake feedparser entry."""
    # Parse date string into time.struct_time
    dt = datetime.strptime(published_str, "%Y-%m-%d").replace(tzinfo=UTC)
    st = dt.timetuple()
    return {
        "title": title,
        "link": f"https://thezvi.substack.com/p/{title.lower().replace(' ', '-')[:30]}",
        "published_parsed": st,
        "content": [{"value": html}],
    }


def test_sync_essay_creates_single_file(tmp_path):
    """Non-roundup posts are cached as a single file."""
    entry = _make_entry(
        "A Tale of Three Contracts",
        "<p>Essay about contracts.</p><h4>Section One</h4><p>Details here.</p>",
    )
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.zvi_cache.fetch_feed", return_value=feed):
        new_files = sync_zvi_cache(tmp_path)

    assert len(new_files) == 1
    assert "a-tale-of-three-contracts" in new_files[0].name
    content = new_files[0].read_text()
    assert "# A Tale of Three Contracts" in content
    assert "Type: essay" in content
    assert "Essay about contracts" in content


def test_sync_roundup_splits_on_h4(tmp_path):
    """AI #N roundup posts are split into one file per <h4> section."""
    html = (
        "<p>Intro paragraph.</p>"
        "<h4>Deepfakes</h4><p>Deepfake news here.</p>"
        "<h4>Show Me The Money</h4><p>Funding round details.</p>"
    )
    entry = _make_entry("AI #158: The Department of War", html)
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.zvi_cache.fetch_feed", return_value=feed):
        new_files = sync_zvi_cache(tmp_path)

    assert len(new_files) == 2
    names = {f.name for f in new_files}
    assert any("deepfakes" in n for n in names)
    assert any("show-me-the-money" in n for n in names)

    # Verify section content
    deepfake_file = [f for f in new_files if "deepfakes" in f.name][0]
    content = deepfake_file.read_text()
    assert "# Deepfakes" in content
    assert "Post: AI #158: The Department of War" in content
    assert "Type: roundup-section" in content
    assert "Deepfake news here" in content


def test_sync_is_idempotent(tmp_path):
    """Running sync twice with the same feed does not create duplicates."""
    entry = _make_entry("Some Essay", "<p>Content.</p>")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.zvi_cache.fetch_feed", return_value=feed):
        first = sync_zvi_cache(tmp_path)
        second = sync_zvi_cache(tmp_path)

    assert len(first) == 1
    assert len(second) == 0


def test_sync_handles_fetch_failure(tmp_path):
    """If the RSS fetch fails, return empty list and don't crash."""
    with patch("pipeline.zvi_cache.fetch_feed", side_effect=Exception("timeout")):
        result = sync_zvi_cache(tmp_path)

    assert result == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_zvi_cache.py -v`
Expected: FAIL (module doesn't exist)

**Step 3: Write implementation**

Create `pipeline/zvi_cache.py`:

```python
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path

import trafilatura

from pipeline.rss_sources import fetch_feed

_ZVI_FEED_URL = "https://thezvi.substack.com/feed"
_ROUNDUP_RE = re.compile(r"AI\s*#\d+", re.IGNORECASE)
_H4_SPLIT_RE = re.compile(r"<h4[^>]*>(.*?)</h4>", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _slugify(text: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:50]


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode entities."""
    import html as html_mod
    return html_mod.unescape(_HTML_TAG_RE.sub("", html)).strip()


def _parse_publish_date(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(time.mktime(st), tz=UTC)
    return None


def _split_roundup_sections(html: str) -> list[tuple[str, str]]:
    """Split roundup HTML on <h4> tags. Returns [(section_title, section_text), ...]."""
    # Find all h4 positions
    matches = list(_H4_SPLIT_RE.finditer(html))
    if not matches:
        return []

    sections = []
    for i, match in enumerate(matches):
        title = _strip_html(match.group(1))
        if not title or title.lower() == "table of contents":
            continue
        # Content is from end of this h4 tag to start of next h4 (or end of HTML)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        section_html = html[start:end]
        text = _strip_html(section_html)
        if text:
            sections.append((title, text))
    return sections


def _extract_essay_text(html: str) -> str:
    """Extract full text from an essay post."""
    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        include_formatting=False,
        favor_precision=True,
    )
    return (text or "").strip()


def sync_zvi_cache(cache_dir: Path) -> list[Path]:
    """Fetch Zvi RSS and cache any new posts. Returns list of newly-written paths."""
    try:
        feed = fetch_feed(_ZVI_FEED_URL)
    except Exception as e:
        print(f"[zvi_cache] Failed to fetch Zvi RSS: {e}")
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
        html = (entry.get("content", [{}])[0].get("value", "") if entry.get("content") else "")

        post_slug = _slugify(title)

        if _ROUNDUP_RE.search(title):
            # Roundup: split on h4
            sections = _split_roundup_sections(html)
            for section_title, section_text in sections:
                section_slug = _slugify(section_title)
                filename = f"{date_str}-{post_slug}--{section_slug}.md"
                path = cache_dir / filename
                if path.exists():
                    continue
                content = (
                    f"# {section_title}\n\n"
                    f"Post: {title}\n"
                    f"URL: {url}\n"
                    f"Published: {date_str}\n"
                    f"Type: roundup-section\n\n"
                    f"{section_text}"
                )
                path.write_text(content, encoding="utf-8")
                new_files.append(path)
        else:
            # Essay: keep whole
            filename = f"{date_str}-{post_slug}.md"
            path = cache_dir / filename
            if path.exists():
                continue
            text = _extract_essay_text(html)
            content = (
                f"# {title}\n\n"
                f"Post: {title}\n"
                f"URL: {url}\n"
                f"Published: {date_str}\n"
                f"Type: essay\n\n"
                f"{text}"
            )
            path.write_text(content, encoding="utf-8")
            new_files.append(path)

    return new_files
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_zvi_cache.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/zvi_cache.py pipeline/test_zvi_cache.py
git -c commit.gpgsign=false commit -m "feat: add zvi_cache module with sync_zvi_cache for persistent caching"
```

---

### Task 2: Add `search_zvi_cache` to `zvi_cache.py`

**Files:**
- Modify: `pipeline/zvi_cache.py`
- Modify: `pipeline/test_zvi_cache.py`

**Step 1: Write the failing test**

Add to `pipeline/test_zvi_cache.py`:

```python
from pipeline.zvi_cache import search_zvi_cache


def test_search_finds_matching_content(tmp_path):
    """Keyword search finds files with matching terms."""
    (tmp_path / "2026-03-07-ai-158--deepfakes.md").write_text(
        "# Deepfakes\n\nPost: AI #158\nURL: https://zvi.com\nPublished: 2026-03-07\nType: roundup-section\n\n"
        "Deepfake detection tools are improving rapidly. OpenAI released a new classifier."
    )
    (tmp_path / "2026-03-07-ai-158--funding.md").write_text(
        "# Show Me The Money\n\nPost: AI #158\nURL: https://zvi.com\nPublished: 2026-03-07\nType: roundup-section\n\n"
        "VC funding for AI startups reached record levels this quarter."
    )

    results = search_zvi_cache("deepfake detection", tmp_path)
    assert len(results) >= 1
    assert results[0]["headline"] == "Deepfakes"


def test_search_respects_max_results(tmp_path):
    """max_results limits the number of returned matches."""
    for i in range(10):
        (tmp_path / f"2026-03-07-post-{i}.md").write_text(
            f"# Post {i}\n\nPost: Post {i}\nURL: https://zvi.com\nPublished: 2026-03-07\nType: essay\n\n"
            f"AI and machine learning topic number {i}."
        )

    results = search_zvi_cache("AI machine learning", tmp_path, max_results=3)
    assert len(results) == 3


def test_search_returns_empty_for_no_match(tmp_path):
    """No results for queries that don't match any cached content."""
    (tmp_path / "2026-03-07-essay.md").write_text(
        "# Some Essay\n\nPost: Some Essay\nURL: https://zvi.com\nPublished: 2026-03-07\nType: essay\n\n"
        "This essay is about cooking recipes."
    )

    results = search_zvi_cache("quantum computing", tmp_path)
    assert results == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_zvi_cache.py -k search -v`
Expected: FAIL (function doesn't exist)

**Step 3: Write implementation**

Add to `pipeline/zvi_cache.py`:

```python
import math

_WORD_RE = re.compile(r"\w+")


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _keyword_score(query: str, title: str, body: str) -> float:
    """Score a document against a query using token overlap."""
    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0
    t_tokens = set(_tokenize(title))
    b_tokens = set(_tokenize(body))
    idf = 1.0 / math.sqrt(len(q_tokens))
    score = 0.0
    for tok in q_tokens:
        if tok in t_tokens:
            score += 3.0 * idf
        if tok in b_tokens:
            score += 1.0 * idf
    return score


def search_zvi_cache(
    query: str, cache_dir: Path, max_results: int = 5
) -> list[dict]:
    """Keyword search across cached Zvi files. Returns top matches.

    Each result is a dict with keys: headline, path, snippet, score.
    """
    if not cache_dir.exists():
        return []

    scored: list[tuple[float, Path, str, str]] = []
    for md_file in cache_dir.glob("*.md"):
        text = md_file.read_text(encoding="utf-8")
        lines = text.split("\n")
        headline = lines[0].lstrip("# ").strip() if lines else ""
        # Body is everything after the metadata block
        parts = text.split("\n\n", 2)
        body = parts[2] if len(parts) > 2 else ""

        score = _keyword_score(query, headline, body)
        if score > 0:
            # Build a snippet: first 300 chars of body
            snippet = body[:300].strip()
            if len(body) > 300:
                snippet += "..."
            scored.append((score, md_file, headline, snippet))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"headline": h, "path": str(p), "snippet": s, "score": sc}
        for sc, p, h, s in scored[:max_results]
    ]
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_zvi_cache.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/zvi_cache.py pipeline/test_zvi_cache.py
git -c commit.gpgsign=false commit -m "feat: add search_zvi_cache for keyword search across persistent cache"
```

---

### Task 3: Integrate Zvi into Things Happen collector

**Files:**
- Modify: `pipeline/things_happen_collector.py`
- Modify: `pipeline/test_things_happen_collector.py`

**Step 1: Write the failing test**

Add to `pipeline/test_things_happen_collector.py`:

```python
def test_zvi_articles_added_to_work_dir(tmp_path, monkeypatch):
    """Zvi posts published today appear in work_dir/articles/zvi/."""
    monkeypatch.setattr("pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: [])
    monkeypatch.setattr("pipeline.things_happen_collector.resolve_redirect_url", lambda u: u)
    monkeypatch.setattr("pipeline.things_happen_collector.generate_research_plan", lambda *a, **kw: [])
    monkeypatch.setattr("pipeline.things_happen_collector._fetch_semafor_articles", lambda: [])

    # Create a fake Zvi cache with one today's post and one old post
    from datetime import UTC, datetime
    zvi_cache = tmp_path / "zvi-cache"
    zvi_cache.mkdir()
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    (zvi_cache / f"{today}-fresh-essay.md").write_text(
        f"# Fresh Essay\n\nPost: Fresh Essay\nURL: https://zvi.com/fresh\nPublished: {today}\nType: essay\n\n"
        "Content about fresh AI topics."
    )
    (zvi_cache / "2026-01-01-old-essay.md").write_text(
        "# Old Essay\n\nPost: Old Essay\nURL: https://zvi.com/old\nPublished: 2026-01-01\nType: essay\n\n"
        "Content about old AI topics."
    )

    # Mock sync to be a no-op (cache already populated above)
    monkeypatch.setattr("pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: [])

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_all_artifacts(
        "test-job", [], work_dir,
        scripts_source_dir=scripts_dir,
        zvi_cache_dir=zvi_cache,
    )

    zvi_dir = work_dir / "articles" / "zvi"
    assert zvi_dir.exists()
    zvi_files = list(zvi_dir.glob("*.md"))
    assert len(zvi_files) == 1
    assert "Fresh Essay" in zvi_files[0].read_text()
```

**Step 2: Run tests to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_collector.py::test_zvi_articles_added_to_work_dir -v`
Expected: FAIL

**Step 3: Write implementation**

Modify `pipeline/things_happen_collector.py`:

1. Add imports:
   ```python
   from pipeline.zvi_cache import sync_zvi_cache
   ```

2. Add `zvi_cache_dir: Path | None = None` parameter to `collect_all_artifacts`.

3. After Phase 1b (Semafor), add Phase 1c:
   ```python
   # Phase 1c: Zvi articles (day-of posts from persistent cache)
   zvi_cache = zvi_cache_dir if zvi_cache_dir is not None else Path("/persist/my-podcasts/zvi-cache")
   sync_zvi_cache(zvi_cache)
   zvi_dir = articles_dir / "zvi"
   zvi_dir.mkdir(parents=True, exist_ok=True)
   today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
   yesterday = (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
   for cached_file in zvi_cache.glob("*.md"):
       if cached_file.name.startswith(today) or cached_file.name.startswith(yesterday):
           target = zvi_dir / cached_file.name
           if not target.exists():
               target.write_text(cached_file.read_text(encoding="utf-8"), encoding="utf-8")
   ```

4. Add `from datetime import timedelta` to the imports (it's not currently imported).

5. After the Semafor headlines section (before the trailing window copy), add Zvi headlines:
   ```python
   # Zvi headlines
   for zvi_path in zvi_dir.glob("*.md"):
       text = zvi_path.read_text(encoding="utf-8")
       parts = text.split("\n\n", 2)
       headline = parts[0].lstrip("# ").strip() if parts else ""
       body = parts[2].strip() if len(parts) > 2 else ""
       truncated = body[:300]
       suffix = "..." if len(body) > 300 else ""
       snippet = f"[zvi] {headline}\nContext: {truncated}{suffix}"
       headlines_with_snippets.append(snippet)
   ```

6. Update existing tests to mock `sync_zvi_cache`:
   - `test_collect_all_artifacts`: add `@patch("pipeline.things_happen_collector.sync_zvi_cache")` returning `[]`
   - `test_fp_links_routed_to_staging`: add `monkeypatch.setattr("pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: [])`
   - `test_semafor_articles_added_to_work_dir`: same

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_collector.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py
git -c commit.gpgsign=false commit -m "feat: integrate Zvi day-of articles into Things Happen collector"
```

---

### Task 4: Replace AI enrichment with Zvi cache search

**Files:**
- Modify: `pipeline/things_happen_collector.py`
- Modify: `pipeline/test_things_happen_collector.py`

**Step 1: Write the failing test**

Add to `pipeline/test_things_happen_collector.py`:

```python
def test_ai_enrichment_uses_zvi_cache(tmp_path, monkeypatch):
    """When a directive has is_ai=True, enrichment searches the Zvi cache."""
    monkeypatch.setattr("pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: [
        Article(headline="New AI Model", url="https://example.com/ai", content="AI model content"),
    ])
    monkeypatch.setattr("pipeline.things_happen_collector.resolve_redirect_url", lambda u: u)
    monkeypatch.setattr("pipeline.things_happen_collector.search_related", lambda *a, **kw: [])
    monkeypatch.setattr("pipeline.things_happen_collector.search_twitter", lambda *a, **kw: None)
    monkeypatch.setattr("pipeline.things_happen_collector._fetch_semafor_articles", lambda: [])
    monkeypatch.setattr("pipeline.things_happen_collector.sync_zvi_cache", lambda cache_dir: [])

    monkeypatch.setattr("pipeline.things_happen_collector.generate_research_plan", lambda *a, **kw: [
        ResearchDirective(
            headline="New AI Model",
            needs_exa=False, exa_query="",
            needs_xai=False, xai_query="",
            is_foreign_policy=False, fp_query="",
            is_ai=True, ai_query="new AI model release",
        ),
    ])

    # Create a Zvi cache with a matching article
    zvi_cache = tmp_path / "zvi-cache"
    zvi_cache.mkdir()
    (zvi_cache / "2026-03-07-ai-models.md").write_text(
        "# AI Models Overview\n\nPost: AI Models\nURL: https://zvi.com\nPublished: 2026-03-07\nType: essay\n\n"
        "A new AI model was released this week, achieving state of the art results."
    )

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_all_artifacts(
        "test-job",
        [{"raw_url": "https://example.com/ai", "headline": "New AI Model"}],
        work_dir,
        scripts_source_dir=scripts_dir,
        zvi_cache_dir=zvi_cache,
    )

    # Enrichment should have written a Zvi match file
    rss_dir = work_dir / "enrichment" / "rss"
    ai_files = list(rss_dir.glob("*-ai.md"))
    assert len(ai_files) == 1
    content = ai_files[0].read_text()
    assert "AI Models Overview" in content
    assert "zvi" in content.lower()
```

**Step 2: Run tests to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_collector.py::test_ai_enrichment_uses_zvi_cache -v`
Expected: FAIL (still uses old search_rss_sources)

**Step 3: Write implementation**

Modify `pipeline/things_happen_collector.py`:

1. Add import:
   ```python
   from pipeline.zvi_cache import search_zvi_cache
   ```

2. Remove `AI_SOURCES` and `search_rss_sources` from the `rss_sources` import (they are no longer used after this change — check if `search_rss_sources` is used elsewhere in the file first; it should not be since FP RSS search was removed in Task 2 of the restructuring).

3. Replace the Phase 3 "RSS search (AI)" block (the `if directive.is_ai and directive.ai_query:` section) with:
   ```python
   # Zvi cache search (AI)
   if directive.is_ai and directive.ai_query:
       try:
           zvi_results = search_zvi_cache(directive.ai_query, zvi_cache)
           if zvi_results:
               out = f"# Zvi Perspectives for: {directive.headline}\nQuery: {directive.ai_query}\n\n"
               for r in zvi_results:
                   out += f"## [{r['headline']}]\n{r['snippet']}\n\n"
               (rss_dir / f"{slug}-ai.md").write_text(out, encoding="utf-8")
       except Exception as e:
           print(f"[collector] Zvi cache search failed for '{directive.ai_query}': {e}")
   ```

   Note: `zvi_cache` is the variable from Phase 1c (the cache dir path). Make sure it's accessible in this scope (it should be, since it's defined earlier in the same function).

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_collector.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py
git -c commit.gpgsign=false commit -m "feat: replace RSS AI enrichment with Zvi cache keyword search"
```

---

### Task 5: Remove `AI_SOURCES` and wire consumer

**Files:**
- Modify: `pipeline/rss_sources.py`
- Modify: `pipeline/consumer.py`
- Modify: `pipeline/test_things_happen_collector.py` (if AI_SOURCES import remains)

**Step 1: Remove `AI_SOURCES` from `rss_sources.py`**

Delete these lines from `pipeline/rss_sources.py`:

```python
AI_SOURCES: list[RssSource] = [
    RssSource(
        name="zvi_substack",
        feed_url="https://thezvi.substack.com/feed",
        wp_search_base=None,  # Substack RSS ignores ?s=, relies on fallback keyword scoring
    ),
]
```

**Step 2: Wire `zvi_cache_dir` in consumer**

In `pipeline/consumer.py`, update the `collect_all_artifacts` call to pass `zvi_cache_dir`:

```python
collect_all_artifacts(
    job["id"],
    links_raw,
    work_dir,
    fp_routed_dir=Path("/persist/my-podcasts/fp-routed-links"),
    zvi_cache_dir=Path("/persist/my-podcasts/zvi-cache"),
)
```

**Step 3: Add Zvi cache cleanup to `_cleanup_old_work_dirs`**

Add after the routed links cleanup block:

```python
# Clean up old Zvi cache files (180-day retention)
zvi_cache_dir = Path("/persist/my-podcasts/zvi-cache")
if zvi_cache_dir.exists():
    for f in zvi_cache_dir.glob("*.md"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
            if mtime < cutoff:
                f.unlink()
                print(f"Cleaned up old Zvi cache: {f.name}")
        except OSError:
            pass
```

**Step 4: Verify no remaining references to `AI_SOURCES`**

Run: `grep -r "AI_SOURCES" pipeline/`
Expected: no matches

**Step 5: Run full test suite**

Run: `uv run pytest -v`
Expected: ALL PASS

**Step 6: Run lint**

Run: `uv run ruff check pipeline/`
Expected: no new errors from our changes

**Step 7: Commit**

```bash
git add pipeline/rss_sources.py pipeline/consumer.py pipeline/things_happen_collector.py
git -c commit.gpgsign=false commit -m "chore: remove AI_SOURCES, wire Zvi cache in consumer, add cache cleanup"
```

---

### Task 6: Update AGENTS.md

**Files:**
- Modify: `AGENTS.md`

Update the Things Happen Pipeline section to mention Zvi as a source. Add under Sources:

```
- Zvi Mowshowitz / "Don't Worry About the Vase" (`thezvi.substack.com/feed`) — AI roundup sections split by topic, essays kept whole. Persistent cache at `/persist/my-podcasts/zvi-cache/` (180-day retention). Also used for AI enrichment via keyword search.
```

Add `pipeline/zvi_cache.py` to the key modules list.

**Commit:**

```bash
git add AGENTS.md
git -c commit.gpgsign=false commit -m "docs: update AGENTS.md with Zvi source integration"
```
