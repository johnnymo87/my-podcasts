# Semafor LLM Routing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace hardcoded Semafor category mapping with a Gemini Flash-Lite classification call at cache sync time, so every article gets a `Routing:` header (`fp`, `th`, `both`, or `skip`).

**Architecture:** A new function `classify_semafor_articles()` in `source_cache.py` sends a batch of `(index, title, description)` tuples to Gemini Flash-Lite and returns routing values. `sync_semafor_cache()` calls it after writing new files and patches the `Routing:` header into each. Both collectors switch to reading `Routing:` first, falling back to the old `categorize_semafor_article()` for legacy files.

**Tech Stack:** google-genai SDK (already a dependency), Gemini 3.1 Flash-Lite, Pydantic for structured output.

---

### Task 1: Add `classify_semafor_articles()` function with tests

**Files:**
- Modify: `pipeline/source_cache.py`
- Modify: `pipeline/test_source_cache.py`

**Step 1: Write the failing test for the classification function**

In `pipeline/test_source_cache.py`, add:

```python
from pipeline.source_cache import classify_semafor_articles


def test_classify_semafor_articles_returns_routing(monkeypatch):
    """LLM classifier returns routing for each article."""
    from unittest.mock import MagicMock

    articles = [
        {"title": "Hungary blocks EU loan to Ukraine", "description": "Orban demands Russian oil access."},
        {"title": "Nvidia's Claw Bar was a pricey wakeup call", "description": "A demo at GTC was really a sales pitch."},
        {"title": "Iran war reshapes Gulf diplomacy", "description": ""},
    ]

    mock_parsed = MagicMock()
    mock_parsed.articles = [
        MagicMock(index=0, routing="fp"),
        MagicMock(index=1, routing="th"),
        MagicMock(index=2, routing="fp"),
    ]
    mock_response = MagicMock()
    mock_response.parsed = mock_parsed

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        result = classify_semafor_articles(articles)

    assert result == {0: "fp", 1: "th", 2: "fp"}
    # Verify we called with the right model
    call_args = mock_client.models.generate_content.call_args
    assert "gemini-3.1-flash-lite-preview" in str(call_args)


def test_classify_semafor_articles_fallback_no_api_key(monkeypatch):
    """Without GEMINI_API_KEY, all articles get 'both'."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    articles = [
        {"title": "Some Article", "description": "Desc."},
        {"title": "Another Article", "description": ""},
    ]
    result = classify_semafor_articles(articles)
    assert result == {0: "both", 1: "both"}


def test_classify_semafor_articles_fallback_on_error(monkeypatch):
    """On Gemini API error, all articles get 'both'."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value.models.generate_content.side_effect = Exception("API down")
        result = classify_semafor_articles([{"title": "Test", "description": ""}])

    assert result == {0: "both"}


def test_classify_semafor_articles_empty_list():
    """Empty input returns empty dict."""
    result = classify_semafor_articles([])
    assert result == {}
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_source_cache.py -k classify -v`
Expected: FAIL with `ImportError` (function doesn't exist yet)

**Step 3: Implement `classify_semafor_articles()`**

In `pipeline/source_cache.py`, add imports near the top:

```python
import logging
import os

from google import genai
from google.genai import types
from pydantic import BaseModel
```

Add the Pydantic models and function after `_parse_publish_date()`:

```python
logger = logging.getLogger(__name__)


class _ArticleRouting(BaseModel):
    index: int
    routing: str


class _RoutingResult(BaseModel):
    articles: list[_ArticleRouting]


_ROUTING_PROMPT = """\
Classify each article for two daily podcasts.

**FP Digest** (`fp`): foreign policy, geopolitics, military conflicts, diplomacy, \
international relations, wars, sanctions, territorial disputes, international \
humanitarian crises, international organizations.

**The Rundown** (`th`): business, technology, AI, science, culture, entertainment, \
domestic US politics, media, energy markets, startups, corporate news.

**Both** (`both`): articles that clearly span both (e.g., US trade policy, sanctions \
with major business impact, arms deals).

**Skip** (`skip`): publisher self-promotion, event announcements, sponsored content, \
meta-content about the publisher itself.

For each article, return its index and routing.

Articles:
"""


def classify_semafor_articles(
    articles: list[dict],
) -> dict[int, str]:
    """Classify Semafor articles into fp/th/both/skip using Gemini.

    Args:
        articles: list of dicts with 'title' and 'description' keys.

    Returns:
        dict mapping article index to routing string.
        Falls back to 'both' for all articles on any failure.
    """
    if not articles:
        return {}

    fallback = {i: "both" for i in range(len(articles))}

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set; defaulting all Semafor routing to 'both'")
        return fallback

    lines = []
    for i, art in enumerate(articles):
        desc = art.get("description", "")
        entry = f"{i}. {art['title']}"
        if desc:
            entry += f" — {desc}"
        lines.append(entry)

    prompt = _ROUTING_PROMPT + "\n".join(lines)

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_RoutingResult,
                temperature=0.1,
            ),
        )
        parsed = response.parsed
        if parsed and parsed.articles:
            result = {}
            for art in parsed.articles:
                routing = art.routing.strip().lower()
                if routing in ("fp", "th", "both", "skip"):
                    result[art.index] = routing
                else:
                    result[art.index] = "both"
            # Fill in any missing indices
            for i in range(len(articles)):
                if i not in result:
                    result[i] = "both"
            return result
    except Exception:
        logger.exception("Gemini Semafor classification failed; defaulting to 'both'")

    return fallback
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_source_cache.py -k classify -v`
Expected: all 4 new tests PASS

**Step 5: Commit**

```bash
git add pipeline/source_cache.py pipeline/test_source_cache.py
git commit -m "feat: add classify_semafor_articles() for LLM-based routing"
```

---

### Task 2: Integrate classification into `sync_semafor_cache()`

**Files:**
- Modify: `pipeline/source_cache.py`
- Modify: `pipeline/test_source_cache.py`

**Step 1: Write the failing test**

In `pipeline/test_source_cache.py`, add:

```python
def test_sync_semafor_writes_routing_header(tmp_path, monkeypatch):
    """sync_semafor_cache writes a Routing: header from LLM classification."""
    entry = _make_semafor_entry("Iran War Update", "", "2026-03-20")
    feed = MagicMock()
    feed.entries = [entry]

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with (
        patch("pipeline.source_cache.fetch_feed", return_value=feed),
        patch(
            "pipeline.source_cache.classify_semafor_articles",
            return_value={0: "fp"},
        ),
    ):
        new_files = sync_semafor_cache(tmp_path)

    assert len(new_files) == 1
    content = new_files[0].read_text()
    assert "Routing: fp" in content
    assert "Category:" in content  # Category still present


def test_sync_semafor_routing_fallback_on_classification_failure(tmp_path, monkeypatch):
    """If classification returns empty, Routing defaults to 'both'."""
    entry = _make_semafor_entry("Some Article", "Technology", "2026-03-20")
    feed = MagicMock()
    feed.entries = [entry]

    with (
        patch("pipeline.source_cache.fetch_feed", return_value=feed),
        patch(
            "pipeline.source_cache.classify_semafor_articles",
            return_value={0: "both"},
        ),
    ):
        new_files = sync_semafor_cache(tmp_path)

    content = new_files[0].read_text()
    assert "Routing: both" in content
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_source_cache.py -k routing_header -v`
Expected: FAIL (Routing header not in content)

**Step 3: Modify `sync_semafor_cache()`**

Change the function to:
1. Collect all new entries with their titles and descriptions into a list
2. Write files without `Routing:` initially
3. Call `classify_semafor_articles()` on the batch
4. Patch `Routing:` into each newly written file

In `sync_semafor_cache()`, after the loop that writes files, add classification and patching:

```python
def sync_semafor_cache(cache_dir: Path) -> list[Path]:
    """Fetch Semafor RSS and cache all articles. Returns newly-written paths."""
    try:
        feed = fetch_feed(SEMAFOR.feed_url)
    except Exception as e:
        print(f"[source_cache] Failed to fetch Semafor RSS: {e}")
        return []

    cache_dir.mkdir(parents=True, exist_ok=True)
    new_files: list[Path] = []
    new_articles: list[dict] = []

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

        description = (entry.get("summary") or "").strip()

        content_html = (
            entry.get("content", [{}])[0].get("value", "")
            if entry.get("content")
            else ""
        )
        text = (
            _strip_html(content_html)
            if content_html
            else description
        )

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
            f"Routing: __PENDING__\n"
            f"Type: article\n\n"
            f"{text}"
        )
        path.write_text(md, encoding="utf-8")
        new_files.append(path)
        new_articles.append({"title": title, "description": description})

    # Classify new articles and patch routing into files
    if new_articles:
        routings = classify_semafor_articles(new_articles)
        for i, path in enumerate(new_files):
            routing = routings.get(i, "both")
            content = path.read_text(encoding="utf-8")
            content = content.replace("Routing: __PENDING__", f"Routing: {routing}", 1)
            path.write_text(content, encoding="utf-8")

    return new_files
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_source_cache.py -v`
Expected: all tests PASS (including existing idempotency and failure tests)

**Step 5: Commit**

```bash
git add pipeline/source_cache.py pipeline/test_source_cache.py
git commit -m "feat: integrate LLM routing into sync_semafor_cache"
```

---

### Task 3: Update The Rundown collector to read `Routing:` header

**Files:**
- Modify: `pipeline/things_happen_collector.py`
- Modify: `pipeline/test_things_happen_collector.py`

**Step 1: Write the failing test**

In `pipeline/test_things_happen_collector.py`, add a test that uses the `Routing:` header:

```python
def test_semafor_routing_header_preferred_over_category(tmp_path, monkeypatch):
    """When Routing: header is present, collector uses it instead of Category."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    work_dir = tmp_path / "work"

    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()

    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()

    # Article with Gulf category (normally FP-only) but Routing: th
    (semafor_cache / f"{today}-gulf-tech-deal.md").write_text(
        f"# Gulf Tech Investment Deal\n\n"
        f"URL: https://semafor.com/gulf-tech\n"
        f"Published: {today}\n"
        f"Source: semafor\n"
        f"Category: Gulf\n"
        f"Routing: th\n"
        f"Type: article\n\n"
        f"Gulf sovereign fund invests in AI startups"
    )
    # Article with no category but Routing: fp (should be excluded from TH)
    (semafor_cache / f"{today}-iran-update.md").write_text(
        f"# Iran War Update\n\n"
        f"URL: https://semafor.com/iran\n"
        f"Published: {today}\n"
        f"Source: semafor\n"
        f"Category: \n"
        f"Routing: fp\n"
        f"Type: article\n\n"
        f"Latest on the Iran conflict"
    )
    # Article with Routing: skip (should be excluded)
    (semafor_cache / f"{today}-semafor-promo.md").write_text(
        f"# Semafor Launches New Show\n\n"
        f"URL: https://semafor.com/promo\n"
        f"Published: {today}\n"
        f"Source: semafor\n"
        f"Category: \n"
        f"Routing: skip\n"
        f"Type: article\n\n"
        f"Self-promotional content"
    )

    zvi_cache = tmp_path / "zvi-cache"
    zvi_cache.mkdir()

    captured_snippets = []

    def mock_editor(headlines, *a, **kw):
        captured_snippets.extend(headlines)
        return RundownResearchPlan(themes=[], stories=[])

    monkeypatch.setattr(
        "pipeline.things_happen_collector.generate_rundown_research_plan",
        mock_editor,
    )

    collect_all_artifacts(
        work_dir,
        "test-routing-header",
        today,
        levine_cache_dir=levine_cache,
        semafor_cache_dir=semafor_cache,
        zvi_cache_dir=zvi_cache,
        lookback_days=2,
    )

    semafor_dir = work_dir / "articles" / "semafor"
    semafor_files = list(semafor_dir.glob("*.md"))
    filenames = [f.name for f in semafor_files]

    # Gulf Tech Investment Deal should be included (Routing: th overrides Category: Gulf)
    assert any("gulf-tech" in n for n in filenames)
    # Iran War Update should be excluded (Routing: fp)
    assert not any("iran-update" in n for n in filenames)
    # Semafor promo should be excluded (Routing: skip)
    assert not any("semafor-promo" in n for n in filenames)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_collector.py::test_semafor_routing_header_preferred_over_category -v`
Expected: FAIL (Gulf article excluded because Category: Gulf → fp)

**Step 3: Update collector routing logic**

In `pipeline/things_happen_collector.py`, modify the Semafor section (around line 120-132):

```python
            category = ""
            url = ""
            routing = ""
            for line in lines[1:8]:
                if line.startswith("Category: "):
                    category = line[10:].strip()
                elif line.startswith("URL: "):
                    url = line[5:].strip()
                elif line.startswith("Routing: "):
                    routing = line[9:].strip()
            # Prefer Routing header; fall back to category-based classification
            if routing:
                effective_routing = routing
            else:
                effective_routing = categorize_semafor_article(category)
            if effective_routing not in ("th", "both"):
                continue
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_things_happen_collector.py -v`
Expected: all tests PASS

**Step 5: Commit**

```bash
git add pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py
git commit -m "feat: Rundown collector reads Routing: header from Semafor cache"
```

---

### Task 4: Update FP Digest collector to read `Routing:` header

**Files:**
- Modify: `pipeline/fp_collector.py`
- Modify: `pipeline/test_fp_collector.py`

**Step 1: Write the failing test**

In `pipeline/test_fp_collector.py`, add a test:

```python
def test_semafor_routing_header_preferred_over_category(tmp_path, monkeypatch):
    """When Routing: header is present, FP collector uses it instead of Category."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    work_dir = tmp_path / "fp-work"

    semafor_cache = tmp_path / "semafor-cache"
    semafor_cache.mkdir()

    # Write a helper function variant with Routing header
    def write_with_routing(headline, url, category, routing, text="Article text."):
        slug = _slugify(headline)
        content = (
            f"# {headline}\n\n"
            f"URL: {url}\n"
            f"Published: {today}\n"
            f"Source: semafor\n"
            f"Category: {category}\n"
            f"Routing: {routing}\n"
            f"Type: article\n\n"
            f"{text}"
        )
        (semafor_cache / f"{today}-{slug}.md").write_text(content, encoding="utf-8")

    # Business category (normally TH-only) but Routing: fp
    write_with_routing("Sanctions Hit Tech Firms", "https://semafor.com/sanctions", "Business", "fp")
    # Routing: th (should be excluded from FP)
    write_with_routing("Nvidia Earnings Beat", "https://semafor.com/nvidia", "Technology", "th")
    # Routing: skip (should be excluded)
    write_with_routing("Semafor Event Promo", "https://semafor.com/promo", "", "skip")

    # Empty homepage / RSS caches
    homepage_cache = tmp_path / "homepage-cache"
    homepage_cache.mkdir()
    rss_cache = tmp_path / "rss-cache"
    rss_cache.mkdir()

    monkeypatch.setattr(
        "pipeline.fp_collector.generate_fp_research_plan",
        lambda *a, **kw: __import__("pipeline.fp_editor", fromlist=["FPResearchPlan"]).FPResearchPlan(themes=[], stories=[]),
    )

    from pipeline.fp_collector import collect_fp_artifacts

    collect_fp_artifacts(
        work_dir=work_dir,
        job_id="test-routing",
        date_str=today,
        homepage_cache_dir=homepage_cache,
        rss_cache_dir=rss_cache,
        semafor_cache_dir=semafor_cache,
        lookback_days=2,
    )

    semafor_dir = work_dir / "articles" / "semafor"
    if semafor_dir.exists():
        semafor_files = list(semafor_dir.glob("*.md"))
        filenames = [f.name for f in semafor_files]
    else:
        filenames = []

    # Sanctions article should be included (Routing: fp overrides Category: Business)
    assert any("sanctions" in n for n in filenames)
    # Nvidia should be excluded (Routing: th)
    assert not any("nvidia" in n for n in filenames)
    # Promo should be excluded (Routing: skip)
    assert not any("promo" in n for n in filenames)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_fp_collector.py::test_semafor_routing_header_preferred_over_category -v`
Expected: FAIL (Sanctions article excluded because Category: Business → th)

**Step 3: Update FP collector routing logic**

In `pipeline/fp_collector.py`, modify the Semafor section (around line 255-268):

```python
            url = ""
            category = ""
            routing = ""
            for line in lines[1:]:
                if line.startswith("URL: "):
                    url = line[5:].strip()
                elif line.startswith("Category: "):
                    category = line[10:].strip()
                elif line.startswith("Routing: "):
                    routing = line[9:].strip()

            if not title or not url:
                continue

            # Prefer Routing header; fall back to category-based classification
            if routing:
                effective_routing = routing
            else:
                effective_routing = categorize_semafor_article(category)
            if effective_routing not in ("fp", "both"):
                continue
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_fp_collector.py -v`
Expected: all tests PASS

**Step 5: Commit**

```bash
git add pipeline/fp_collector.py pipeline/test_fp_collector.py
git commit -m "feat: FP collector reads Routing: header from Semafor cache"
```

---

### Task 5: Run full test suite and verify

**Step 1: Run all tests**

Run: `uv run pytest -v`
Expected: all tests PASS

**Step 2: Verify existing Semafor cache backward compatibility**

Manually verify that existing cache files (without `Routing:` header) still work by checking that the `categorize_semafor_article()` fallback path is exercised in existing tests that write cache files without a `Routing:` line.

**Step 3: Commit any fixups**

If any test adjustments were needed, commit them.

---

### Task 6: Update AGENTS.md documentation

**Files:**
- Modify: `AGENTS.md`

**Step 1: Update the Content Routing section**

Update the Semafor routing description to reflect that routing is now LLM-based at cache sync time, with fallback to category mapping for legacy files.

**Step 2: Commit**

```bash
git add AGENTS.md
git commit -m "docs: update Semafor routing docs for LLM classification"
```
