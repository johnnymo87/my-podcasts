# Pipeline Restructuring Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Route foreign policy content exclusively to FP Digest, add Semaphore as a source split by category, and clean up the Things Happen pipeline to focus on finance/business/tech.

**Architecture:** The Things Happen editor already classifies links with `is_foreign_policy: bool`. We use this flag to route FP links to a shared staging directory (`/persist/my-podcasts/fp-routed-links/`). FP Digest's collector picks them up at 6 PM. Semaphore's RSS feed (`semafor.com/rss.xml`) is added with client-side category filtering to split articles between the two podcasts.

**Tech Stack:** Python, feedparser, Pydantic, pytest, existing pipeline infrastructure.

---

### Task 1: Add Semaphore RSS Source + Category Router

**Files:**
- Modify: `pipeline/rss_sources.py`
- Test: `pipeline/test_rss_sources.py`

**Step 1: Write the failing test**

Add to `pipeline/test_rss_sources.py`:

```python
from pipeline.rss_sources import SEMAFOR, categorize_semafor_article

def test_semafor_source_exists():
    assert SEMAFOR.name == "semafor"
    assert SEMAFOR.feed_url == "https://www.semafor.com/rss.xml"

def test_categorize_semafor_fp():
    assert categorize_semafor_article("Africa") == "fp"
    assert categorize_semafor_article("Gulf") == "fp"
    assert categorize_semafor_article("Security") == "fp"

def test_categorize_semafor_th():
    assert categorize_semafor_article("Business") == "th"
    assert categorize_semafor_article("Technology") == "th"
    assert categorize_semafor_article("Media") == "th"
    assert categorize_semafor_article("CEO") == "th"
    assert categorize_semafor_article("Energy") == "th"

def test_categorize_semafor_both():
    assert categorize_semafor_article("Politics") == "both"

def test_categorize_semafor_unknown_defaults_to_both():
    assert categorize_semafor_article("SomeNewCategory") == "both"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_rss_sources.py -k semafor -v`
Expected: ImportError

**Step 3: Write implementation**

Add to `pipeline/rss_sources.py`:

```python
SEMAFOR = RssSource(
    name="semafor",
    feed_url="https://www.semafor.com/rss.xml",
    wp_search_base=None,
)

_SEMAFOR_FP_CATEGORIES = {"africa", "gulf", "security"}
_SEMAFOR_TH_CATEGORIES = {"business", "technology", "media", "ceo", "energy"}

def categorize_semafor_article(category: str) -> str:
    """Return 'fp', 'th', or 'both' based on Semafor category tag."""
    cat = category.strip().lower()
    if cat in _SEMAFOR_FP_CATEGORIES:
        return "fp"
    if cat in _SEMAFOR_TH_CATEGORIES:
        return "th"
    return "both"
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest pipeline/test_rss_sources.py -k semafor -v`
Expected: PASS

**Step 5: Commit**

```bash
git add pipeline/rss_sources.py pipeline/test_rss_sources.py
git commit -m "feat: add Semaphore RSS source and category routing function"
```

---

### Task 2: Route FP Links From Things Happen to Staging Directory

**Files:**
- Modify: `pipeline/things_happen_collector.py`
- Test: `pipeline/test_things_happen_collector.py`

**Step 1: Write the failing test**

Add to `pipeline/test_things_happen_collector.py`:

```python
import json

def test_fp_links_routed_to_staging(tmp_path, monkeypatch):
    """FP-flagged links are written to fp-routed-links dir, not enriched."""
    from pipeline.things_happen_collector import collect_all_artifacts
    from pipeline.things_happen_editor import ResearchDirective

    # Mock all external calls
    monkeypatch.setattr("pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: [
        type("Art", (), {"headline": "Iran War Escalates", "url": "https://example.com/iran", "content": "war content"})(),
        type("Art", (), {"headline": "Bitcoin Rises", "url": "https://example.com/btc", "content": "crypto content"})(),
    ])
    monkeypatch.setattr("pipeline.things_happen_collector.resolve_redirect_url", lambda u: u)
    monkeypatch.setattr("pipeline.things_happen_collector.search_related", lambda *a, **kw: [])
    monkeypatch.setattr("pipeline.things_happen_collector.search_twitter", lambda *a, **kw: None)
    monkeypatch.setattr("pipeline.things_happen_collector.search_rss_sources", lambda *a, **kw: [])

    # Editor returns one FP link and one non-FP link
    monkeypatch.setattr("pipeline.things_happen_collector.generate_research_plan", lambda *a, **kw: [
        ResearchDirective(
            headline="Iran War Escalates", needs_exa=False, exa_query="",
            needs_xai=False, xai_query="", is_foreign_policy=True,
            fp_query="iran war", is_ai=False, ai_query="",
        ),
        ResearchDirective(
            headline="Bitcoin Rises", needs_exa=False, exa_query="",
            needs_xai=False, xai_query="", is_foreign_policy=False,
            fp_query="", is_ai=False, ai_query="",
        ),
    ])

    routed_dir = tmp_path / "fp-routed"
    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    links_raw = [
        {"raw_url": "https://example.com/iran", "headline": "Iran War Escalates"},
        {"raw_url": "https://example.com/btc", "headline": "Bitcoin Rises"},
    ]

    collect_all_artifacts(
        "test-job", links_raw, work_dir,
        scripts_source_dir=scripts_dir,
        fp_routed_dir=routed_dir,
    )

    # FP link should be in the routed file
    routed_files = list(routed_dir.glob("*.json"))
    assert len(routed_files) == 1
    routed = json.loads(routed_files[0].read_text())
    assert len(routed) == 1
    assert routed[0]["headline"] == "Iran War Escalates"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_collector.py::test_fp_links_routed_to_staging -v`
Expected: FAIL (signature doesn't accept `fp_routed_dir`)

**Step 3: Write implementation**

Modify `pipeline/things_happen_collector.py`:

1. Add `fp_routed_dir: Path | None = None` parameter to `collect_all_artifacts`.
2. After the editor returns directives, partition into FP and non-FP:
   ```python
   fp_directives = [d for d in directives if d.is_foreign_policy]
   non_fp_directives = [d for d in directives if not d.is_foreign_policy]
   ```
3. Write FP directives to the staging directory:
   ```python
   if fp_directives and fp_routed_dir is not None:
       fp_routed_dir.mkdir(parents=True, exist_ok=True)
       today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
       routed_path = fp_routed_dir / f"{today}.json"
       routed_data = []
       for d in fp_directives:
           # Find the matching article for context
           art = next((a for a in articles if a.headline == d.headline), None)
           routed_data.append({
               "headline": d.headline,
               "url": art.url if art else "",
               "snippet": (art.content[:500] if art else ""),
           })
       routed_path.write_text(json.dumps(routed_data, indent=2), encoding="utf-8")
   ```
4. Only run enrichment on `non_fp_directives` (change the enrichment loop variable from `directives` to `non_fp_directives`).
5. Remove the FP RSS search block (lines 112-129) since FP links are no longer enriched here.

**Step 4: Run test to verify it passes**

Run: `uv run pytest pipeline/test_things_happen_collector.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py
git commit -m "feat: route FP-flagged links to staging directory for FP Digest"
```

---

### Task 3: Update Things Happen Agent Prompt

**Files:**
- Modify: `pipeline/things_happen_agent.py`

**Step 1: Update the prompt**

In `build_agent_prompt()`, modify step 3 to:

```python
3. **Check the Trailing Window:** Read the prior episode scripts in `{work_dir}/context/`. If a story is adequately covered in these prior scripts and has no new developments, **skip it**. Do not repeat background context we already explained to the listener yesterday. Note: foreign policy stories from the newsletter have been routed to a separate Foreign Policy Digest podcast. Focus on finance, business, technology, law, and other non-FP topics.
```

**Step 2: Run existing tests**

Run: `uv run pytest pipeline/test_things_happen_agent.py -v`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add pipeline/things_happen_agent.py
git commit -m "chore: update Things Happen prompt to note FP routing"
```

---

### Task 4: Add Semaphore Articles to Things Happen Collector

**Files:**
- Modify: `pipeline/things_happen_collector.py`
- Test: `pipeline/test_things_happen_collector.py`

**Step 1: Write the failing test**

Add to `pipeline/test_things_happen_collector.py`:

```python
def test_semafor_articles_added_to_work_dir(tmp_path, monkeypatch):
    """Semafor articles categorized as 'th' or 'both' appear in work dir."""
    from pipeline.things_happen_collector import collect_all_artifacts

    # Mock all external calls
    monkeypatch.setattr("pipeline.things_happen_collector.fetch_all_articles", lambda *a, **kw: [])
    monkeypatch.setattr("pipeline.things_happen_collector.resolve_redirect_url", lambda u: u)
    monkeypatch.setattr("pipeline.things_happen_collector.generate_research_plan", lambda *a, **kw: [])

    # Mock Semafor RSS fetch to return articles with categories
    import feedparser
    fake_feed = feedparser.FeedParserDict()
    fake_feed.entries = [
        feedparser.FeedParserDict({
            "title": "Tech Company IPO",
            "link": "https://semafor.com/tech-ipo",
            "summary": "A tech company goes public",
            "tags": [feedparser.FeedParserDict({"term": "Technology"})],
        }),
        feedparser.FeedParserDict({
            "title": "Gulf Tensions Rise",
            "link": "https://semafor.com/gulf-tensions",
            "summary": "Tensions in the Gulf",
            "tags": [feedparser.FeedParserDict({"term": "Gulf"})],
        }),
    ]
    monkeypatch.setattr("pipeline.things_happen_collector._fetch_semafor_articles", lambda: fake_feed.entries)

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_all_artifacts("test-job", [], work_dir, scripts_source_dir=scripts_dir)

    # Tech article should appear (category=Technology -> "th")
    semafor_dir = work_dir / "articles" / "semafor"
    assert semafor_dir.exists()
    semafor_files = list(semafor_dir.glob("*.md"))
    # Only the Tech article, not the Gulf one (FP-only)
    assert len(semafor_files) == 1
    assert "Tech Company IPO" in semafor_files[0].read_text()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_collector.py::test_semafor_articles_added_to_work_dir -v`
Expected: FAIL

**Step 3: Write implementation**

Add to `pipeline/things_happen_collector.py`:

1. Import: `from pipeline.rss_sources import SEMAFOR, categorize_semafor_article, _fetch_feed`
2. Add a helper function:
   ```python
   def _fetch_semafor_articles() -> list[dict]:
       """Fetch Semafor RSS and return parsed entries."""
       try:
           feed = _fetch_feed(SEMAFOR.feed_url)
           return list(feed.entries)
       except Exception as e:
           print(f"[collector] Failed to fetch Semafor RSS: {e}")
           return []
   ```
3. In `collect_all_artifacts`, after writing articles and before the editor AI call, fetch Semafor articles and write "th" and "both" categories to `{work_dir}/articles/semafor/`:
   ```python
   semafor_dir = work_dir / "articles" / "semafor"
   semafor_dir.mkdir(parents=True, exist_ok=True)
   semafor_entries = _fetch_semafor_articles()
   for entry in semafor_entries:
       category = ""
       if entry.get("tags"):
           category = entry["tags"][0].get("term", "")
       routing = categorize_semafor_article(category)
       if routing in ("th", "both"):
           headline = (entry.get("title") or "").strip()
           url = (entry.get("link") or "").strip()
           summary = (entry.get("summary") or "").strip()
           content_encoded = (entry.get("content", [{}])[0].get("value", "") if entry.get("content") else "")
           if headline:
               slug = _slugify(headline)
               text = content_encoded or summary
               art_path = semafor_dir / f"{slug}.md"
               art_path.write_text(
                   f"# {headline}\n\nURL: {url}\nSource: semafor\nCategory: {category}\n\n{text}",
                   encoding="utf-8",
               )
   ```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_collector.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py
git commit -m "feat: add Semafor business/tech articles to Things Happen collection"
```

---

### Task 5: FP Digest Collector Picks Up Routed Links + Semafor FP Articles

**Files:**
- Modify: `pipeline/fp_collector.py`
- Test: `pipeline/test_fp_collector.py`

**Step 1: Write the failing test**

Add to `pipeline/test_fp_collector.py`:

```python
import json

def test_routed_levine_links_included(tmp_path, monkeypatch):
    """FP Digest collector picks up routed links from Things Happen."""
    from pipeline.fp_collector import collect_fp_artifacts

    # Mock all external calls
    monkeypatch.setattr("pipeline.fp_collector.scrape_homepage", lambda: [])
    monkeypatch.setattr("pipeline.fp_collector._fetch_rss_articles", lambda *a, **kw: [])
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: f"Article text for {url}")
    monkeypatch.setattr("pipeline.fp_collector.generate_fp_research_plan", lambda *a, **kw: _make_empty_plan())
    monkeypatch.setattr("pipeline.fp_collector.search_related", lambda *a, **kw: [])
    monkeypatch.setattr("pipeline.fp_collector._fetch_semafor_fp_articles", lambda: [])

    # Create routed links file
    routed_dir = tmp_path / "fp-routed"
    routed_dir.mkdir()
    from datetime import UTC, datetime
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    (routed_dir / f"{today}.json").write_text(json.dumps([
        {"headline": "Iran Sanctions Tighten", "url": "https://example.com/iran", "snippet": "sanctions content"},
    ]))

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_fp_artifacts(
        "test-job", work_dir,
        scripts_source_dir=scripts_dir,
        fp_routed_dir=routed_dir,
    )

    # Routed article should appear in work_dir/articles/routed/
    routed_articles = list((work_dir / "articles" / "routed").glob("*.md"))
    assert len(routed_articles) == 1
    assert "Iran Sanctions Tighten" in routed_articles[0].read_text()


def test_semafor_fp_articles_included(tmp_path, monkeypatch):
    """FP Digest collector picks up Semafor FP-category articles."""
    from pipeline.fp_collector import collect_fp_artifacts

    monkeypatch.setattr("pipeline.fp_collector.scrape_homepage", lambda: [])
    monkeypatch.setattr("pipeline.fp_collector._fetch_rss_articles", lambda *a, **kw: [])
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: "text")
    monkeypatch.setattr("pipeline.fp_collector.generate_fp_research_plan", lambda *a, **kw: _make_empty_plan())
    monkeypatch.setattr("pipeline.fp_collector.search_related", lambda *a, **kw: [])

    # Mock Semafor FP fetch
    monkeypatch.setattr("pipeline.fp_collector._fetch_semafor_fp_articles", lambda: [
        {"headline": "Gulf Crisis Deepens", "url": "https://semafor.com/gulf", "text": "Gulf article text", "category": "Gulf"},
    ])

    work_dir = tmp_path / "work"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    collect_fp_artifacts("test-job", work_dir, scripts_source_dir=scripts_dir)

    semafor_dir = work_dir / "articles" / "semafor"
    assert semafor_dir.exists()
    semafor_files = list(semafor_dir.glob("*.md"))
    assert len(semafor_files) == 1
    assert "Gulf Crisis Deepens" in semafor_files[0].read_text()
```

Note: `_make_empty_plan()` is a test helper that returns a minimal `FPResearchPlan` with empty directives and themes. You may need to define it or reuse an existing fixture.

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_fp_collector.py -k "routed or semafor" -v`
Expected: FAIL

**Step 3: Write implementation**

Modify `pipeline/fp_collector.py`:

1. Add `fp_routed_dir: Path | None = None` parameter to `collect_fp_artifacts`.
2. Add imports: `import json` and `from pipeline.rss_sources import SEMAFOR, categorize_semafor_article, _fetch_feed`
3. Add a helper:
   ```python
   def _fetch_semafor_fp_articles() -> list[dict]:
       """Fetch Semafor RSS and return FP-category articles."""
       try:
           feed = _fetch_feed(SEMAFOR.feed_url)
       except Exception as e:
           print(f"[fp_collector] Failed to fetch Semafor RSS: {e}")
           return []
       articles = []
       for entry in feed.entries:
           category = ""
           if entry.get("tags"):
               category = entry["tags"][0].get("term", "")
           routing = categorize_semafor_article(category)
           if routing in ("fp", "both"):
               headline = (entry.get("title") or "").strip()
               url = (entry.get("link") or "").strip()
               content_encoded = (entry.get("content", [{}])[0].get("value", "") if entry.get("content") else "")
               summary = (entry.get("summary") or "").strip()
               if headline:
                   articles.append({
                       "headline": headline,
                       "url": url,
                       "text": content_encoded or summary,
                       "category": category,
                   })
       return articles
   ```
4. In `collect_fp_artifacts`, after Phase 2 (RSS) and before Phase 3 (context):
   - Read routed links:
     ```python
     # Phase 2b: Pick up routed links from Things Happen
     routed_links_dir = fp_routed_dir if fp_routed_dir is not None else Path("/persist/my-podcasts/fp-routed-links")
     articles_routed_dir = work_dir / "articles" / "routed"
     articles_routed_dir.mkdir(parents=True, exist_ok=True)
     if routed_links_dir.exists():
         today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
         yesterday = (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
         for date_str in (today, yesterday):
             routed_path = routed_links_dir / f"{date_str}.json"
             if routed_path.exists():
                 routed_data = json.loads(routed_path.read_text())
                 for item in routed_data:
                     headline = item["headline"]
                     url = item.get("url", "")
                     if url not in homepage_urls:
                         text = _extract_article_text(url) if url else item.get("snippet", "")
                         slug = _slugify(headline)
                         art_path = articles_routed_dir / f"{slug}.md"
                         art_path.write_text(
                             f"# {headline}\n\nURL: {url}\nSource: levine-routed\n\n{text}",
                             encoding="utf-8",
                         )
     ```
   - Fetch Semafor FP articles:
     ```python
     # Phase 2c: Semafor FP articles
     articles_semafor_dir = work_dir / "articles" / "semafor"
     articles_semafor_dir.mkdir(parents=True, exist_ok=True)
     semafor_articles = _fetch_semafor_fp_articles()
     for article in semafor_articles:
         slug = _slugify(article["headline"])
         art_path = articles_semafor_dir / f"{slug}.md"
         art_path.write_text(
             f"# {article['headline']}\n\nURL: {article['url']}\nSource: semafor\nCategory: {article['category']}\n\n{article['text']}",
             encoding="utf-8",
         )
     ```
   - Include routed + Semafor headlines in the `headlines_with_snippets` list for the editor.

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_fp_collector.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add pipeline/fp_collector.py pipeline/test_fp_collector.py
git commit -m "feat: FP Digest picks up routed Levine links and Semafor FP articles"
```

---

### Task 6: Update Consumer Cleanup + Wire Default Paths

**Files:**
- Modify: `pipeline/consumer.py`

**Step 1: Update `_cleanup_old_work_dirs`**

Add cleanup of `/persist/my-podcasts/fp-routed-links/` files older than 7 days:

```python
# Clean up old routed link files
routed_dir = Path("/persist/my-podcasts/fp-routed-links")
if routed_dir.exists():
    for f in routed_dir.glob("*.json"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
            if mtime < cutoff:
                f.unlink()
                print(f"Cleaned up old routed links: {f.name}")
        except OSError:
            pass
```

**Step 2: Wire default `fp_routed_dir` in consumer**

In the consumer's Things Happen processing section (around line 256), pass `fp_routed_dir` to `collect_all_artifacts`:

```python
collect_all_artifacts(
    job["id"], links_raw, work_dir,
    fp_routed_dir=Path("/persist/my-podcasts/fp-routed-links"),
)
```

**Step 3: Run existing tests**

Run: `uv run pytest pipeline/test_consumer.py -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add pipeline/consumer.py
git commit -m "chore: wire FP routing dir in consumer, add routed-links cleanup"
```

---

### Task 7: Update `_find_article_text` for New Directories

**Files:**
- Modify: `pipeline/consumer.py` (the `_find_article_text` helper)

**Step 1: Extend search paths**

The `_find_article_text` function searches `articles/homepage/`, `articles/rss/`, and `enrichment/exa/`. Add `articles/routed/` and `articles/semafor/` to the search:

```python
# Search routed articles
for match in (work_dir / "articles" / "routed").rglob(f"{slug}.md"):
    return match.read_text(encoding="utf-8")

# Search semafor articles
for match in (work_dir / "articles" / "semafor").rglob(f"{slug}.md"):
    return match.read_text(encoding="utf-8")
```

**Step 2: Run tests**

Run: `uv run pytest pipeline/test_consumer.py -v`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add pipeline/consumer.py
git commit -m "chore: extend _find_article_text to search routed and semafor dirs"
```

---

### Task 8: Full Test Suite + Lint

**Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: ALL PASS

**Step 2: Run lint**

Run: `uv run ruff check pipeline/`
Expected: No new errors from our changes (pre-existing errors acceptable)

**Step 3: Fix any issues found**

**Step 4: Commit any fixes**

```bash
git commit -m "fix: address lint/test issues from pipeline restructuring"
```

---

### Task 9: Update AGENTS.md

**Files:**
- Modify: `AGENTS.md`

Update the FP Digest and Things Happen sections to reflect:
- FP Digest now also ingests routed Levine FP links and Semafor FP/Security/Africa/Gulf articles
- Things Happen now routes FP links to FP Digest and includes Semafor business/tech articles
- Add Semafor to the source list

**Step 1: Make the edits**
**Step 2: Commit**

```bash
git add AGENTS.md
git commit -m "docs: update AGENTS.md for pipeline restructuring"
```
