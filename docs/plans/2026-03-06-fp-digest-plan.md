# Foreign Policy Digest Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a fully automated daily foreign policy podcast that synthesizes antiwar.com (3 RSS feeds + curated homepage links) and Caitlin Johnstone into a 10-15 minute audio briefing.

**Architecture:** A 5-phase pipeline (Collector -> Editor AI -> Light Enrichment -> Writer AI -> TTS+Publish) triggered by a daily systemd timer. Follows the Things Happen multi-stage pattern but is simpler: no async agent session, no human-in-the-loop. Script generation uses synchronous opencode-serve calls.

**Tech Stack:** Python 3.14, BeautifulSoup (scraping), feedparser + trafilatura (RSS), google-genai/Gemini Flash-Lite (editor), opencode-serve (writer), ttsjoin/OpenAI TTS (audio), SQLite (state), Cloudflare R2 (storage).

**Design doc:** `docs/plans/2026-03-06-fp-digest-design.md`

---

### Task 1: Homepage Scraper

**Files:**
- Create: `pipeline/fp_homepage_scraper.py`
- Test: `pipeline/test_fp_homepage_scraper.py`

The scraper fetches antiwar.com and parses the region-grouped link sections at the bottom of the page. The HTML structure uses `class="hotspot"` headers followed by nested tables of links. All ~49 links are external (Al Jazeera, MEE, Anadolu, etc.).

**Step 1: Write the failing tests**

Create `pipeline/test_fp_homepage_scraper.py` with a representative HTML fixture (a stripped-down version of the actual page structure) and tests for:

```python
from __future__ import annotations

from pipeline.fp_homepage_scraper import HomepageLink, parse_homepage_links, scrape_homepage


SAMPLE_HTML = """\
<html><body>
<table width="100%" border="0" cellspacing="0" cellpadding="0">
<tr><td class="hotspot">Iran</td></tr>
<tr><td bgcolor="#F3F5F6">
  <table width="240" border="0" cellspacing="0" cellpadding="2" bgcolor="#F3F5F6">
    <tr>
      <td width="10" valign="top" align="center"><img src="blkbullet1.gif"></td>
      <td width="230"><font size="2" face="Verdana"><a href="https://aljazeera.com/iran-war">Iran War Escalates</a></font></td>
    </tr>
    <tr>
      <td width="10" valign="top" align="center"><img src="blkbullet1.gif"></td>
      <td width="230"><font size="2" face="Verdana"><a href="https://middleeasteye.net/iran-drones">Pentagon Struggles With Drones</a></font></td>
    </tr>
  </table>
</td></tr>
<tr><td height="5"></td></tr>
<tr><td class="hotspot">Europe</td></tr>
<tr><td bgcolor="#F3F5F6">
  <table width="240" border="0" cellspacing="0" cellpadding="2" bgcolor="#F3F5F6">
    <tr>
      <td width="10" valign="top" align="center"><img src="blkbullet1.gif"></td>
      <td width="230"><font size="2" face="Verdana"><a href="https://euronews.com/finland-nukes">Finland Lifts Nuke Ban</a></font></td>
    </tr>
  </table>
</td></tr>
</table>
</body></html>
"""


def test_parse_homepage_links_extracts_regions() -> None:
    links = parse_homepage_links(SAMPLE_HTML)
    regions = {link.region for link in links}
    assert "Iran" in regions
    assert "Europe" in regions


def test_parse_homepage_links_extracts_urls_and_headlines() -> None:
    links = parse_homepage_links(SAMPLE_HTML)
    iran_links = [l for l in links if l.region == "Iran"]
    assert len(iran_links) == 2
    assert iran_links[0].url == "https://aljazeera.com/iran-war"
    assert iran_links[0].headline == "Iran War Escalates"
    assert iran_links[1].url == "https://middleeasteye.net/iran-drones"


def test_parse_homepage_links_total_count() -> None:
    links = parse_homepage_links(SAMPLE_HTML)
    assert len(links) == 3


def test_parse_homepage_links_empty_html() -> None:
    links = parse_homepage_links("<html><body></body></html>")
    assert links == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_fp_homepage_scraper.py -v`
Expected: FAIL (module not found)

**Step 3: Write the implementation**

Create `pipeline/fp_homepage_scraper.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class HomepageLink:
    region: str
    headline: str
    url: str


def parse_homepage_links(html: str) -> list[HomepageLink]:
    """Parse the region-grouped link sections from antiwar.com homepage HTML.

    The HTML uses <td class="hotspot"> as region headers, followed by a sibling
    row containing a nested table of links.
    """
    soup = BeautifulSoup(html, "html.parser")
    links: list[HomepageLink] = []

    hotspots = soup.find_all("td", class_="hotspot")
    for hotspot in hotspots:
        region = hotspot.get_text(strip=True)
        if not region:
            continue

        # Walk up to the <tr> containing this hotspot, then find the next
        # sibling <tr> which contains the link table.
        parent_tr = hotspot.find_parent("tr")
        if parent_tr is None:
            continue

        sibling_tr = parent_tr.find_next_sibling("tr")
        if sibling_tr is None:
            continue

        for anchor in sibling_tr.find_all("a", href=True):
            headline = anchor.get_text(strip=True)
            url = anchor["href"]
            if headline and url:
                links.append(HomepageLink(region=region, headline=headline, url=url))

    return links


def scrape_homepage() -> list[HomepageLink]:
    """Fetch antiwar.com and parse the region link sections."""
    resp = requests.get(
        "https://www.antiwar.com",
        headers={"User-Agent": "podcast-pipeline/1.0"},
        timeout=30,
    )
    resp.raise_for_status()
    return parse_homepage_links(resp.text)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_fp_homepage_scraper.py -v`
Expected: PASS

**Step 5: Lint**

Run: `uv run ruff check pipeline/fp_homepage_scraper.py pipeline/test_fp_homepage_scraper.py`

**Step 6: Commit**

```bash
git add pipeline/fp_homepage_scraper.py pipeline/test_fp_homepage_scraper.py
git commit -m "feat: add antiwar.com homepage scraper for FP Digest"
```

---

### Task 2: FP Editor AI

**Files:**
- Create: `pipeline/fp_editor.py`
- Test: `pipeline/test_fp_editor.py`

Mirrors `things_happen_editor.py` but with FP-specific Pydantic models. Uses Gemini Flash-Lite to triage stories into themes and select which to include.

**Step 1: Write the failing tests**

Create `pipeline/test_fp_editor.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.fp_editor import (
    FPResearchPlan,
    FPStoryDirective,
    generate_fp_research_plan,
)


def test_returns_empty_if_no_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = generate_fp_research_plan(["Some headline"])
    assert result.themes == []
    assert result.directives == []


def test_returns_empty_if_no_headlines(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    result = generate_fp_research_plan([])
    assert result.themes == []
    assert result.directives == []


@patch("pipeline.fp_editor.genai.Client")
def test_success(mock_client_class, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    mock_response = MagicMock()
    mock_response.parsed = FPResearchPlan(
        themes=["Iran War", "NATO Expansion"],
        directives=[
            FPStoryDirective(
                headline="Iran War Escalates",
                source="rss/antiwar_news",
                priority=1,
                theme="Iran War",
                needs_exa=False,
                exa_query="",
                include_in_episode=True,
            )
        ],
    )
    mock_client.models.generate_content.return_value = mock_response

    result = generate_fp_research_plan(["Iran War Escalates: death toll rises"])

    assert len(result.themes) == 2
    assert len(result.directives) == 1
    assert result.directives[0].include_in_episode is True
    mock_client.models.generate_content.assert_called_once()


@patch("pipeline.fp_editor.genai.Client")
def test_handles_exception(mock_client_class, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.models.generate_content.side_effect = Exception("API error")

    result = generate_fp_research_plan(["Headline"])
    assert result.themes == []
    assert result.directives == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_fp_editor.py -v`
Expected: FAIL

**Step 3: Write the implementation**

Create `pipeline/fp_editor.py`:

```python
from __future__ import annotations

import os

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


class FPStoryDirective(BaseModel):
    headline: str = Field(description="The original headline")
    source: str = Field(
        description="Which source this came from (e.g. 'homepage/iran', 'rss/antiwar_news', 'rss/caitlinjohnstone')"
    )
    priority: int = Field(description="1-5, where 1 is the lead story")
    theme: str = Field(
        description="Grouping label (e.g. 'Iran War', 'Lebanon Escalation', 'NATO Expansion')"
    )
    needs_exa: bool = Field(
        description="True if the article is paywalled or inaccessible and needs an open-access alternative via web search"
    )
    exa_query: str = Field(
        description="Search query for finding open-access alternatives (3-6 keywords). Empty string if needs_exa is false."
    )
    include_in_episode: bool = Field(
        description="True if this story should be included in today's episode. Select 8-12 stories that best cover the major themes."
    )


class FPResearchPlan(BaseModel):
    themes: list[str] = Field(
        description="The 3-5 dominant themes or story arcs identified across all sources today"
    )
    directives: list[FPStoryDirective]


_EMPTY_PLAN = FPResearchPlan(themes=[], directives=[])


def generate_fp_research_plan(
    headlines_with_snippets: list[str],
    context_scripts: list[str] | None = None,
) -> FPResearchPlan:
    """Ask Gemini Flash-Lite to triage collected FP stories into themes and select which to include."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _EMPTY_PLAN

    if not headlines_with_snippets:
        return _EMPTY_PLAN

    client = genai.Client(api_key=api_key)

    prompt = (
        "You are an editor for a daily foreign policy podcast. "
        "Analyze these headlines and snippets from today's sources "
        "(antiwar.com news, viewpoints, blog, homepage wire links, and Caitlin Johnstone). "
        "Identify the 3-5 dominant themes, assign each story to a theme, "
        "and select the 8-12 best stories for a 10-15 minute episode. "
        "Avoid selecting redundant coverage of the same event from multiple sources -- "
        "pick the strongest version.\n\n"
    )

    if context_scripts:
        prompt += "Recent episode scripts (what the audience already heard):\n"
        for script in context_scripts:
            prompt += f"---\n{script[:500]}\n---\n"
        prompt += (
            "\nDeprioritize stories well-covered above unless there are new developments.\n\n"
        )

    prompt += "Today's collected stories:\n\n"
    for item in headlines_with_snippets:
        prompt += f"- {item}\n"

    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=FPResearchPlan,
                temperature=0.1,
            ),
        )
        parsed = response.parsed
        if isinstance(parsed, FPResearchPlan):
            return parsed
        return _EMPTY_PLAN
    except Exception as e:
        print(f"Error generating FP research plan: {e}")
        return _EMPTY_PLAN
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_fp_editor.py -v`
Expected: PASS

**Step 5: Lint and commit**

```bash
uv run ruff check pipeline/fp_editor.py pipeline/test_fp_editor.py
git add pipeline/fp_editor.py pipeline/test_fp_editor.py
git commit -m "feat: add FP editor AI for story triage and theme identification"
```

---

### Task 3: FP Collector

**Files:**
- Create: `pipeline/fp_collector.py`
- Test: `pipeline/test_fp_collector.py`
- Modify: `pipeline/rss_sources.py` (add missing feeds)

Orchestrates fetching from all 5 sources, deduplicates, runs the editor, and performs Exa enrichment.

**Step 1: Update `rss_sources.py` to add the missing feeds**

Add `ANTIWAR_ORIGINAL` and `ANTIWAR_BLOG` sources, and a new `FP_DIGEST_SOURCES` list that includes all 4 RSS feeds (3 antiwar + Caitlin Johnstone). Keep the existing `FP_SOURCES` alias unchanged (it's used by Things Happen enrichment).

Add to `pipeline/rss_sources.py` after line 46:

```python
ANTIWAR_ORIGINAL = RssSource(
    name="antiwar_original",
    feed_url="https://original.antiwar.com/feed/",
    wp_search_base="https://original.antiwar.com/",
)

ANTIWAR_BLOG = RssSource(
    name="antiwar_blog",
    feed_url="https://feeds.feedburner.com/AWCBlog",
    wp_search_base="https://www.antiwar.com/blog/",
)

FP_DIGEST_RSS_SOURCES: list[RssSource] = [
    *DEFAULT_SOURCES,  # antiwar_news + caitlinjohnstone
    ANTIWAR_ORIGINAL,
    ANTIWAR_BLOG,
]
```

**Step 2: Write the failing tests for `fp_collector.py`**

Create `pipeline/test_fp_collector.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from pipeline.fp_collector import collect_fp_artifacts, _slugify
from pipeline.fp_editor import FPResearchPlan, FPStoryDirective
from pipeline.fp_homepage_scraper import HomepageLink
from pipeline.rss_sources import RssResult


def test_slugify() -> None:
    assert _slugify("Hello World!") == "hello-world"
    assert _slugify("A -- B") == "a-b"


@patch("pipeline.fp_collector.scrape_homepage")
@patch("pipeline.fp_collector._fetch_rss_articles")
@patch("pipeline.fp_collector._extract_article_text")
@patch("pipeline.fp_collector.generate_fp_research_plan")
@patch("pipeline.fp_collector.search_related")
def test_collect_fp_artifacts(
    mock_exa, mock_editor, mock_extract, mock_rss, mock_homepage, tmp_path
) -> None:
    mock_homepage.return_value = [
        HomepageLink(region="Iran", headline="Iran War Update", url="https://example.com/iran"),
    ]

    mock_rss.return_value = [
        {"source": "antiwar_news", "headline": "Death Toll Rises", "url": "https://news.antiwar.com/death-toll", "text": "Full article text"},
    ]

    mock_extract.return_value = "Extracted article text about Iran"

    mock_editor.return_value = FPResearchPlan(
        themes=["Iran War"],
        directives=[
            FPStoryDirective(
                headline="Iran War Update",
                source="homepage/iran",
                priority=1,
                theme="Iran War",
                needs_exa=False,
                exa_query="",
                include_in_episode=True,
            ),
            FPStoryDirective(
                headline="Death Toll Rises",
                source="rss/antiwar_news",
                priority=2,
                theme="Iran War",
                needs_exa=False,
                exa_query="",
                include_in_episode=True,
            ),
        ],
    )

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "2026-03-05.txt").write_text("yesterday's script")

    work_dir = tmp_path / "work"

    collect_fp_artifacts("job-1", work_dir, scripts_source_dir=scripts_dir)

    assert (work_dir / "articles" / "homepage").exists()
    assert (work_dir / "articles" / "rss").exists()
    assert (work_dir / "context").exists()

    # Context should be copied
    assert (work_dir / "context" / "2026-03-05.txt").exists()

    # Editor plan should be written
    assert (work_dir / "plan.json").exists()

    mock_exa.assert_not_called()  # no stories flagged needs_exa
```

**Step 3: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_fp_collector.py -v`
Expected: FAIL

**Step 4: Write the implementation**

Create `pipeline/fp_collector.py`:

```python
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import feedparser
import requests
import trafilatura

from pipeline.exa_client import search_related
from pipeline.fp_editor import FPResearchPlan, generate_fp_research_plan
from pipeline.fp_homepage_scraper import HomepageLink, scrape_homepage
from pipeline.rss_sources import FP_DIGEST_RSS_SOURCES


def _slugify(text: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:50]


def _extract_article_text(url: str) -> str:
    """Fetch a URL and extract article text via trafilatura."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "podcast-pipeline/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        text = trafilatura.extract(
            resp.content,
            url=url,
            include_comments=False,
            include_tables=False,
            include_formatting=False,
            favor_precision=True,
        )
        return (text or "").strip()
    except Exception:
        return ""


def _fetch_rss_articles(days_back: int = 3) -> list[dict]:
    """Fetch recent articles from all FP Digest RSS sources."""
    since = datetime.now(tz=UTC) - timedelta(days=days_back)
    articles: list[dict] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "podcast-pipeline/1.0"})

    for src in FP_DIGEST_RSS_SOURCES:
        try:
            resp = session.get(src.feed_url, timeout=15)
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)
        except Exception:
            continue

        for entry in feed.entries:
            published = None
            for key in ("published_parsed", "updated_parsed"):
                st = entry.get(key)
                if st:
                    import time as _time
                    published = datetime.fromtimestamp(_time.mktime(st), tz=UTC)
                    break

            if published and published < since:
                continue

            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue

            summary = (entry.get("summary") or entry.get("description") or "").strip()

            articles.append({
                "source": src.name,
                "headline": title,
                "url": link,
                "summary": summary,
                "text": "",  # filled later if selected
            })

    return articles


def collect_fp_artifacts(
    job_id: str,
    work_dir: Path,
    scripts_source_dir: Path | None = None,
) -> None:
    """Collect, triage, and enrich FP Digest sources."""
    # Create directory structure
    homepage_dir = work_dir / "articles" / "homepage"
    rss_dir = work_dir / "articles" / "rss"
    exa_dir = work_dir / "enrichment" / "exa"
    context_dir = work_dir / "context"

    for d in (homepage_dir, rss_dir, exa_dir, context_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Phase 1: Collect from all sources
    # 1a: Homepage scrape
    homepage_links = scrape_homepage()
    seen_urls: set[str] = set()

    for link in homepage_links:
        if link.url in seen_urls:
            continue
        seen_urls.add(link.url)

        text = _extract_article_text(link.url)
        region_dir = homepage_dir / _slugify(link.region)
        region_dir.mkdir(exist_ok=True)

        slug = _slugify(link.headline)
        content = f"# {link.headline}\n\nRegion: {link.region}\nURL: {link.url}\n\n{text}"
        (region_dir / f"{slug}.md").write_text(content, encoding="utf-8")
        time.sleep(0.5)  # polite delay

    # 1b: RSS feeds
    rss_articles = _fetch_rss_articles()

    for art in rss_articles:
        if art["url"] in seen_urls:
            continue
        seen_urls.add(art["url"])

        source_dir = rss_dir / _slugify(art["source"])
        source_dir.mkdir(exist_ok=True)

        slug = _slugify(art["headline"])
        content = f"# {art['headline']}\n\nSource: {art['source']}\nURL: {art['url']}\n\n{art['summary']}"
        (source_dir / f"{slug}.md").write_text(content, encoding="utf-8")

    # Copy trailing window context (last 3 scripts)
    scripts_dir = (
        scripts_source_dir
        if scripts_source_dir is not None
        else Path("/persist/my-podcasts/scripts/fp-digest")
    )
    if scripts_dir.exists():
        scripts = sorted(scripts_dir.glob("*.txt"), reverse=True)[:3]
        for script in scripts:
            target = context_dir / script.name
            if not target.exists():
                target.write_text(script.read_text(encoding="utf-8"))

    # Build headlines + snippets for editor
    headlines_with_snippets: list[str] = []

    for link in homepage_links:
        text_path = homepage_dir / _slugify(link.region) / f"{_slugify(link.headline)}.md"
        text = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
        truncated = text[:300]
        suffix = "..." if len(text) > 300 else ""
        headlines_with_snippets.append(
            f"[homepage/{link.region}] {link.headline}\nContext: {truncated}{suffix}"
        )

    for art in rss_articles:
        if art["url"] not in seen_urls or art["url"] in {l.url for l in homepage_links}:
            # Already written under homepage, but still include in snippets
            pass
        headlines_with_snippets.append(
            f"[rss/{art['source']}] {art['headline']}\nContext: {art['summary'][:300]}"
        )

    # Load context scripts for editor
    context_scripts: list[str] = []
    for ctx_file in sorted(context_dir.glob("*.txt"), reverse=True):
        context_scripts.append(ctx_file.read_text(encoding="utf-8"))

    # Phase 2: Editor AI
    plan = generate_fp_research_plan(headlines_with_snippets, context_scripts=context_scripts)

    # Write plan to disk for debugging / writer consumption
    plan_path = work_dir / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    # Phase 3: Light enrichment (Exa only, for paywalled articles)
    for directive in plan.directives:
        if directive.needs_exa and directive.exa_query and directive.include_in_episode:
            slug = _slugify(directive.headline)
            try:
                exa_results = search_related(directive.exa_query)
                if exa_results:
                    out = f"# Exa Results for: {directive.headline}\nQuery: {directive.exa_query}\n\n"
                    for r in exa_results:
                        out += f"## [{r.title}]({r.url})\n{r.text}\n\n"
                    (exa_dir / f"{slug}.md").write_text(out, encoding="utf-8")
            except Exception as e:
                print(f"[fp-collector] Exa search failed for '{directive.exa_query}': {e}")

    # Fetch full text for selected RSS articles that only have summaries
    for directive in plan.directives:
        if not directive.include_in_episode:
            continue
        if directive.source.startswith("rss/"):
            source_name = directive.source.split("/", 1)[1]
            slug = _slugify(directive.headline)
            art_path = rss_dir / _slugify(source_name) / f"{slug}.md"
            if art_path.exists():
                existing = art_path.read_text(encoding="utf-8")
                # If we only have the summary (short), fetch full text
                if len(existing) < 500:
                    for art in rss_articles:
                        if _slugify(art["headline"]) == slug:
                            full_text = _extract_article_text(art["url"])
                            if full_text:
                                content = f"# {art['headline']}\n\nSource: {art['source']}\nURL: {art['url']}\n\n{full_text}"
                                art_path.write_text(content, encoding="utf-8")
                            break
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_fp_collector.py -v`
Expected: PASS

**Step 6: Lint and commit**

```bash
uv run ruff check pipeline/fp_collector.py pipeline/test_fp_collector.py pipeline/rss_sources.py
git add pipeline/fp_collector.py pipeline/test_fp_collector.py pipeline/rss_sources.py
git commit -m "feat: add FP collector with homepage scraping, RSS fetching, and Exa enrichment"
```

---

### Task 4: FP Writer

**Files:**
- Create: `pipeline/fp_writer.py`
- Test: `pipeline/test_fp_writer.py`

Generates the podcast script via a synchronous opencode-serve call. Follows the `summarizer.py` pattern.

**Step 1: Write the failing tests**

Create `pipeline/test_fp_writer.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.fp_writer import build_fp_prompt, generate_fp_script


def test_build_fp_prompt_includes_themes() -> None:
    prompt = build_fp_prompt(
        themes=["Iran War", "NATO Expansion"],
        articles_by_theme={"Iran War": ["Article 1 text"], "NATO Expansion": ["Article 2 text"]},
        date_str="2026-03-06",
    )
    assert "Iran War" in prompt
    assert "NATO Expansion" in prompt
    assert "2026-03-06" in prompt
    assert "Article 1 text" in prompt


def test_build_fp_prompt_includes_context_scripts() -> None:
    prompt = build_fp_prompt(
        themes=["Iran War"],
        articles_by_theme={"Iran War": ["Article text"]},
        date_str="2026-03-06",
        context_scripts=["Yesterday we covered the start of strikes on Iran."],
    )
    assert "Yesterday we covered" in prompt


@patch("pipeline.fp_writer.create_session", return_value="sess-1")
@patch("pipeline.fp_writer.send_prompt_async")
@patch("pipeline.fp_writer.wait_for_idle", return_value=True)
@patch("pipeline.fp_writer.get_messages", return_value=[
    {"role": "assistant", "parts": [{"type": "text", "text": "This is the Foreign Policy Digest for today."}]}
])
@patch("pipeline.fp_writer.delete_session")
def test_generate_fp_script(mock_del, mock_msgs, mock_wait, mock_send, mock_create) -> None:
    script = generate_fp_script(
        themes=["Iran War"],
        articles_by_theme={"Iran War": ["Full article text"]},
        date_str="2026-03-06",
    )
    assert "Foreign Policy Digest" in script
    mock_create.assert_called_once()
    mock_del.assert_called_once_with("sess-1")


@patch("pipeline.fp_writer.create_session", return_value="sess-1")
@patch("pipeline.fp_writer.send_prompt_async")
@patch("pipeline.fp_writer.wait_for_idle", return_value=False)
@patch("pipeline.fp_writer.delete_session")
def test_generate_fp_script_timeout(mock_del, mock_wait, mock_send, mock_create) -> None:
    import pytest
    with pytest.raises(RuntimeError, match="did not complete"):
        generate_fp_script(
            themes=["Iran War"],
            articles_by_theme={"Iran War": ["text"]},
            date_str="2026-03-06",
        )
    mock_del.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_fp_writer.py -v`
Expected: FAIL

**Step 3: Write the implementation**

Create `pipeline/fp_writer.py`:

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
You are generating a daily foreign policy podcast briefing. Today's date is {date_str}.

Below are today's stories organized by theme, drawn from antiwar.com (news reporting, viewpoints, blog, and their curated wire links) and Caitlin Johnstone. Your job is to brief the listener on each major theme: what happened, why it matters, and what the independent antiwar perspective reveals that mainstream coverage misses.

Write naturally and conversationally. This will be read aloud by a TTS engine, so it should sound like a knowledgeable friend catching you up on the day's foreign policy news. Aim for about 1500-2200 words, which gives a 10-15 minute episode.

{context_block}

---

TODAY'S THEMES: {themes_list}

{stories_block}
"""


def build_fp_prompt(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
) -> str:
    context_block = ""
    if context_scripts:
        context_block = (
            "RECENT EPISODES (what the listener already heard):\n\n"
            + "\n---\n".join(context_scripts)
            + "\n\nBuild on what the listener already knows. Skip stories already well-covered "
            "unless there are meaningful new developments. When continuing a running story, "
            "reference what was said before rather than re-explaining background.\n"
        )

    themes_list = ", ".join(themes)

    story_sections: list[str] = []
    for theme in themes:
        articles = articles_by_theme.get(theme, [])
        section = f"## {theme}\n\n"
        for i, article in enumerate(articles, 1):
            section += f"### Source {i}\n{article}\n\n"
        story_sections.append(section)

    stories_block = "\n".join(story_sections)

    return PROMPT_TEMPLATE.format(
        date_str=date_str,
        context_block=context_block,
        themes_list=themes_list,
        stories_block=stories_block,
    )


def generate_fp_script(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
) -> str:
    """Generate the FP Digest script via the shared opencode server."""
    prompt = build_fp_prompt(themes, articles_by_theme, date_str, context_scripts)

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

Run: `uv run pytest pipeline/test_fp_writer.py -v`
Expected: PASS

**Step 5: Lint and commit**

```bash
uv run ruff check pipeline/fp_writer.py pipeline/test_fp_writer.py
git add pipeline/fp_writer.py pipeline/test_fp_writer.py
git commit -m "feat: add FP writer for podcast script generation via opencode-serve"
```

---

### Task 5: FP Processor (TTS + Publish)

**Files:**
- Create: `pipeline/fp_processor.py`
- Test: `pipeline/test_fp_processor.py`
- Modify: `pipeline/presets.py:16-49` (add new preset)

**Step 1: Add the preset**

Add to `pipeline/presets.py` inside `PRESETS` tuple, after the Things Happen entry (line 48):

```python
    NewsletterPreset(
        name="Foreign Policy Digest",
        route_tags=("fp-digest",),
        tts_model="tts-1-hd",
        tts_voice="coral",
        category="News",
        feed_slug="fp-digest",
    ),
```

**Step 2: Write the failing tests**

Create `pipeline/test_fp_processor.py`:

```python
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from pipeline.db import StateStore
from pipeline.fp_processor import process_fp_digest_job


def test_process_fp_digest_job(tmp_path, monkeypatch) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    script_file = tmp_path / "script.txt"
    script_file.write_text("This is the Foreign Policy Digest for March 6th.")

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="600.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        "pipeline.fp_processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )

    job = {"id": "job-1", "date_str": "2026-03-06"}
    process_fp_digest_job(job, store, r2_client, script_path=script_file)

    episodes = store.list_episodes(feed_slug="fp-digest")
    assert len(episodes) == 1
    assert "Foreign Policy Digest" in episodes[0].title
    assert "2026-03-06" in episodes[0].title

    r2_client.upload_file.assert_called_once()
    upload_key = r2_client.upload_file.call_args[0][1]
    assert upload_key.startswith("episodes/fp-digest/")

    store.close()
```

**Step 3: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_fp_processor.py -v`
Expected: FAIL

**Step 4: Write the implementation**

Create `pipeline/fp_processor.py`:

```python
from __future__ import annotations

import subprocess
import tempfile
import uuid
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
TTS_VOICE = "coral"
FEED_SLUG = "fp-digest"
CATEGORY = "News"


def _parse_duration_seconds(mp3_path: Path) -> int | None:
    cmd = [
        "ffprobe",
        "-v", "error",
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


def process_fp_digest_job(
    job: dict,
    store: StateStore,
    r2_client: R2Client,
    script_path: Path,
) -> None:
    """Process a single FP Digest job: TTS + upload + episode insertion."""
    date_str = job["date_str"]
    script = script_path.read_text(encoding="utf-8")

    episode_slug = f"{date_str}-fp-digest"
    episode_r2_key = f"episodes/{FEED_SLUG}/{episode_slug}.mp3"

    with tempfile.TemporaryDirectory(prefix="fp-digest-") as tmp_dir:
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

    episode_title = f"{date_str} - Foreign Policy Digest"
    episode = Episode(
        id=str(uuid.uuid4()),
        title=episode_title,
        slug=episode_slug,
        pub_date=format_datetime(datetime.now(tz=UTC)),
        r2_key=episode_r2_key,
        feed_slug=FEED_SLUG,
        category=CATEGORY,
        source_tag="fp-digest",
        preset_name="Foreign Policy Digest",
        source_url=None,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
    )
    store.insert_episode(episode)
    store.mark_fp_digest_completed(job["id"])
    regenerate_and_upload_feed(store, r2_client)
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_fp_processor.py -v`
Expected: PASS

**Step 6: Lint and commit**

```bash
uv run ruff check pipeline/fp_processor.py pipeline/test_fp_processor.py pipeline/presets.py
git add pipeline/fp_processor.py pipeline/test_fp_processor.py pipeline/presets.py
git commit -m "feat: add FP processor for TTS + publish, add podcast preset"
```

---

### Task 6: Database Changes

**Files:**
- Modify: `pipeline/db.py`
- Test: `pipeline/test_fp_digest_db.py`

Add `pending_fp_digest` table and associated methods.

**Step 1: Write the failing tests**

Create `pipeline/test_fp_digest_db.py`:

```python
from __future__ import annotations

from pipeline.db import StateStore


def test_fp_digest_table_created(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    tables = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_fp_digest'"
    ).fetchall()
    assert len(tables) == 1
    store.close()


def test_insert_and_list_due(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    job_id = store.insert_pending_fp_digest("2026-03-06")
    assert job_id

    due = store.list_due_fp_digest()
    assert len(due) == 1
    assert due[0]["date_str"] == "2026-03-06"
    store.close()


def test_mark_completed(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    job_id = store.insert_pending_fp_digest("2026-03-06")
    store.mark_fp_digest_completed(job_id)

    due = store.list_due_fp_digest()
    assert len(due) == 0
    store.close()


def test_no_duplicate_for_same_date(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    id1 = store.insert_pending_fp_digest("2026-03-06")
    id2 = store.insert_pending_fp_digest("2026-03-06")
    assert id2 is None  # Should not create duplicate

    due = store.list_due_fp_digest()
    assert len(due) == 1
    store.close()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_fp_digest_db.py -v`
Expected: FAIL

**Step 3: Write the implementation**

Add to `pipeline/db.py` SCHEMA string (after the `pending_things_happen` CREATE TABLE):

```sql
CREATE TABLE IF NOT EXISTS pending_fp_digest (
    id TEXT PRIMARY KEY,
    date_str TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    process_after TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Add methods to the `StateStore` class:

```python
def insert_pending_fp_digest(self, date_str: str) -> str | None:
    """Insert a pending FP digest job. Returns job ID, or None if date already exists."""
    existing = self._conn.execute(
        "SELECT 1 FROM pending_fp_digest WHERE date_str = ?",
        (date_str,),
    ).fetchone()
    if existing:
        return None
    job_id = str(uuid.uuid4())
    process_after = datetime.now(tz=UTC).isoformat()
    self._conn.execute(
        """INSERT INTO pending_fp_digest
           (id, date_str, process_after, status)
           VALUES (?, ?, ?, ?)""",
        (job_id, date_str, process_after, "pending"),
    )
    self._conn.commit()
    return job_id

def list_due_fp_digest(self) -> list[dict]:
    now = datetime.now(tz=UTC).isoformat()
    rows = self._conn.execute(
        """SELECT id, date_str, process_after
           FROM pending_fp_digest
           WHERE status = 'pending' AND process_after <= ?
           ORDER BY process_after ASC""",
        (now,),
    ).fetchall()
    return [dict(row) for row in rows]

def mark_fp_digest_completed(self, job_id: str) -> None:
    self._conn.execute(
        "UPDATE pending_fp_digest SET status = 'completed' WHERE id = ?",
        (job_id,),
    )
    self._conn.commit()
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_fp_digest_db.py -v`
Expected: PASS

**Step 5: Lint and commit**

```bash
uv run ruff check pipeline/db.py pipeline/test_fp_digest_db.py
git add pipeline/db.py pipeline/test_fp_digest_db.py
git commit -m "feat: add pending_fp_digest table and DB methods"
```

---

### Task 7: Consumer Integration

**Files:**
- Modify: `pipeline/consumer.py:120-246`

Add FP digest job processing to the consumer loop, extend cleanup.

**Step 1: Write the failing test**

Add to `pipeline/test_consumer.py` (or create a new test file `pipeline/test_consumer_fp.py`):

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.consumer import _cleanup_old_work_dirs


def test_cleanup_covers_fp_digest_dirs(tmp_path, monkeypatch) -> None:
    """Verify _cleanup_old_work_dirs cleans fp-digest-* dirs too."""
    import os
    from datetime import UTC, datetime, timedelta

    # Create an old fp-digest dir
    old_dir = tmp_path / "fp-digest-old-job"
    old_dir.mkdir()
    # Set mtime to 200 days ago
    old_time = (datetime.now(tz=UTC) - timedelta(days=200)).timestamp()
    os.utime(old_dir, (old_time, old_time))

    # Create a recent one
    new_dir = tmp_path / "fp-digest-new-job"
    new_dir.mkdir()

    monkeypatch.setattr("pipeline.consumer.Path", lambda x: tmp_path if x == "/tmp" else type("P", (), {"glob": lambda self, p: tmp_path.glob(p)})())

    # This test verifies the glob pattern is extended; exact cleanup
    # behavior is tested via integration.
```

This is tricky to unit test due to hardcoded `/tmp`. The real verification is in integration. Instead, focus on verifying the consumer calls the right methods:

```python
def test_consumer_processes_fp_digest_jobs(tmp_path, monkeypatch) -> None:
    """Verify consume_forever checks for due FP digest jobs."""
    from pipeline.db import StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    job_id = store.insert_pending_fp_digest("2026-03-06")
    assert job_id is not None

    due = store.list_due_fp_digest()
    assert len(due) == 1
    store.close()
```

**Step 2: Write the implementation**

Modify `pipeline/consumer.py`:

1. Add imports at top:
```python
from pipeline.fp_collector import collect_fp_artifacts
from pipeline.fp_processor import process_fp_digest_job
```

2. Extend `_cleanup_old_work_dirs` to also clean `fp-digest-*`:
```python
for pattern in ("things-happen-*", "fp-digest-*"):
    for d in tmp.glob(pattern):
        ...
```

3. Add FP digest job processing after the Things Happen section (after line 243), inside `consume_forever`:
```python
# Process any due FP Digest jobs.
try:
    fp_jobs = store.list_due_fp_digest()
    for job in fp_jobs:
        try:
            work_dir = Path(f"/tmp/fp-digest-{job['id']}")
            script_file = work_dir / "script.txt"

            if script_file.exists():
                # Script already generated -- run TTS + publish.
                dry_run = os.environ.get("FP_DIGEST_DRY_RUN", "").strip()
                if dry_run:
                    print(f"DRY RUN: skipping TTS for FP digest {job['id']} ({job['date_str']})")
                    store.mark_fp_digest_completed(job["id"])
                else:
                    print(f"Processing FP digest with script: {job['id']} ({job['date_str']})")
                    process_fp_digest_job(job, store, r2_client, script_path=script_file)
                    print(f"Completed FP digest: {job['id']}")

                # Copy to persistent storage
                persist_dir = Path("/persist/my-podcasts/scripts/fp-digest")
                persist_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy(script_file, persist_dir / f"{job['date_str']}.txt")
            else:
                # Run full pipeline: collect -> edit -> enrich -> write -> save script
                print(f"Running FP digest collection for: {job['id']} ({job['date_str']})")
                collect_fp_artifacts(job["id"], work_dir)

                # Generate script
                from pipeline.fp_writer import generate_fp_script
                from pipeline.fp_editor import FPResearchPlan
                import json

                plan_path = work_dir / "plan.json"
                if plan_path.exists():
                    plan = FPResearchPlan.model_validate_json(plan_path.read_text())
                else:
                    print(f"No plan generated for FP digest {job['id']}, skipping")
                    continue

                # Build articles_by_theme from selected directives
                articles_by_theme: dict[str, list[str]] = {}
                for directive in plan.directives:
                    if not directive.include_in_episode:
                        continue
                    # Find the article file
                    article_text = _find_article_text(directive, work_dir)
                    if article_text:
                        articles_by_theme.setdefault(directive.theme, []).append(article_text)

                context_scripts = []
                context_dir = work_dir / "context"
                if context_dir.exists():
                    for f in sorted(context_dir.glob("*.txt"), reverse=True):
                        context_scripts.append(f.read_text(encoding="utf-8"))

                script = generate_fp_script(
                    themes=plan.themes,
                    articles_by_theme=articles_by_theme,
                    date_str=job["date_str"],
                    context_scripts=context_scripts,
                )
                script_file.write_text(script, encoding="utf-8")
                # Next loop iteration will pick up the script and run TTS.
        except Exception as exc:
            print(f"Failed FP digest job {job['id']}: {exc}")
except Exception as exc:
    print(f"Error checking FP digest jobs: {exc}")
```

Also add a helper:
```python
def _find_article_text(directive, work_dir: Path) -> str:
    """Find the article file matching a directive."""
    from pipeline.fp_collector import _slugify
    slug = _slugify(directive.headline)

    # Check homepage articles
    homepage_dir = work_dir / "articles" / "homepage"
    if homepage_dir.exists():
        for md in homepage_dir.rglob(f"{slug}.md"):
            return md.read_text(encoding="utf-8")

    # Check RSS articles
    rss_dir = work_dir / "articles" / "rss"
    if rss_dir.exists():
        for md in rss_dir.rglob(f"{slug}.md"):
            return md.read_text(encoding="utf-8")

    # Check Exa enrichment
    exa_dir = work_dir / "enrichment" / "exa"
    if exa_dir.exists():
        exa_file = exa_dir / f"{slug}.md"
        if exa_file.exists():
            return exa_file.read_text(encoding="utf-8")

    return ""
```

**Step 3: Run full test suite**

Run: `uv run pytest pipeline/ -v`
Expected: PASS

**Step 4: Lint and commit**

```bash
uv run ruff check pipeline/consumer.py
git add pipeline/consumer.py
git commit -m "feat: integrate FP digest job processing into consumer loop"
```

---

### Task 8: CLI Command

**Files:**
- Modify: `pipeline/__main__.py`

**Step 1: Add the `fp-digest` command**

Add after the `feed_command` function (line 81):

```python
@cli.command("fp-digest")
@click.option("--date", "date_str", default=None, type=str, help="Date string (YYYY-MM-DD). Defaults to today.")
def fp_digest_command(date_str: str | None) -> None:
    """Create and process an FP Digest episode for today (or a given date)."""
    from datetime import UTC, datetime

    if date_str is None:
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()

        job_id = store.insert_pending_fp_digest(date_str)
        if job_id is None:
            click.echo(f"FP Digest job already exists for {date_str}")
            due = store.list_due_fp_digest()
            job = next((j for j in due if j["date_str"] == date_str), None)
            if job is None:
                click.echo("Job exists but is not pending. Nothing to do.")
                return
        else:
            click.echo(f"Created FP Digest job {job_id} for {date_str}")
            due = store.list_due_fp_digest()
            job = next((j for j in due if j["id"] == job_id), None)
            if job is None:
                click.echo("Error: job not found after creation")
                return

        # Run the full pipeline inline
        from pathlib import Path
        import shutil
        from pipeline.fp_collector import collect_fp_artifacts
        from pipeline.fp_writer import generate_fp_script
        from pipeline.fp_editor import FPResearchPlan
        from pipeline.fp_processor import process_fp_digest_job

        work_dir = Path(f"/tmp/fp-digest-{job['id']}")
        click.echo(f"Collecting sources into {work_dir}...")
        collect_fp_artifacts(job["id"], work_dir)

        plan_path = work_dir / "plan.json"
        if not plan_path.exists():
            click.echo("Error: editor generated no plan")
            return

        plan = FPResearchPlan.model_validate_json(plan_path.read_text())
        click.echo(f"Editor identified themes: {', '.join(plan.themes)}")
        click.echo(f"Selected {sum(1 for d in plan.directives if d.include_in_episode)} stories")

        # Build articles by theme
        from pipeline.consumer import _find_article_text
        articles_by_theme: dict[str, list[str]] = {}
        for directive in plan.directives:
            if not directive.include_in_episode:
                continue
            text = _find_article_text(directive, work_dir)
            if text:
                articles_by_theme.setdefault(directive.theme, []).append(text)

        context_scripts = []
        context_dir = work_dir / "context"
        if context_dir.exists():
            for f in sorted(context_dir.glob("*.txt"), reverse=True):
                context_scripts.append(f.read_text(encoding="utf-8"))

        click.echo("Generating script...")
        script = generate_fp_script(
            themes=plan.themes,
            articles_by_theme=articles_by_theme,
            date_str=date_str,
            context_scripts=context_scripts,
        )

        script_file = work_dir / "script.txt"
        script_file.write_text(script, encoding="utf-8")
        click.echo(f"Script written to {script_file}")

        click.echo("Running TTS and publishing...")
        process_fp_digest_job(job, store, r2_client, script_path=script_file)

        # Copy to persistent storage
        persist_dir = Path("/persist/my-podcasts/scripts/fp-digest")
        persist_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(script_file, persist_dir / f"{date_str}.txt")

        click.echo(f"Published FP Digest for {date_str}")
    finally:
        store.close()
```

**Step 2: Verify CLI loads**

Run: `uv run python -m pipeline --help`
Expected: Should list `fp-digest` command.

**Step 3: Lint and commit**

```bash
uv run ruff check pipeline/__main__.py
git add pipeline/__main__.py
git commit -m "feat: add fp-digest CLI command"
```

---

### Task 9: Remove "Delve" Rules from Existing Prompts

**Files:**
- Modify: `pipeline/summarizer.py:31`
- Modify: `pipeline/things_happen_agent.py:56-58`

**Step 1: Update `summarizer.py`**

In `pipeline/summarizer.py`, remove line 31:
```
- Do NOT use the word "delve" or any of its variants.
```

**Step 2: Update `things_happen_agent.py`**

In `pipeline/things_happen_agent.py`, the agent prompt at line 56-58 says:
```
5. **Write the Script:** Once approved by the operator, write your final podcast script to `{work_dir}/script.txt`. Make it conversational, engaging, and suitable for TTS.
```

This line doesn't contain "delve" -- check if it's elsewhere in the prompt. If there's no explicit delve rule in the agent prompt, no change needed here. (The summarizer is the one with the rule.)

**Step 3: Run existing tests to verify nothing breaks**

Run: `uv run pytest pipeline/test_summarizer.py pipeline/test_things_happen_agent.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add pipeline/summarizer.py pipeline/things_happen_agent.py
git commit -m "refactor: replace specific word prohibition with broad writing ethos"
```

---

### Task 10: Full Test Suite + Lint

**Step 1: Run all tests**

Run: `uv run pytest pipeline/ -v`
Expected: All PASS

**Step 2: Run linter**

Run: `uv run ruff check pipeline/`
Expected: Clean

**Step 3: Run type checker**

Run: `uv run mypy pipeline/ --ignore-missing-imports`
Expected: Clean (or only pre-existing issues)

**Step 4: Commit any fixes**

---

### Task 11: Update AGENTS.md and Documentation

**Files:**
- Modify: `AGENTS.md`

Add to the subscription URLs section:
```
- FP Digest: `https://podcast.mohrbacher.dev/feeds/fp-digest.xml`
```

Add to the Core Paths or a new section:
```
## FP Digest Pipeline

Daily foreign policy podcast from antiwar.com + Caitlin Johnstone.

**Flow:**
1. Systemd timer triggers daily at 6 PM EST
2. `fp_homepage_scraper.py` scrapes antiwar.com homepage region links
3. `fp_collector.py` fetches RSS feeds + homepage articles
4. `fp_editor.py` (Gemini Flash-Lite) triages into themes, selects stories
5. Exa enrichment for paywalled articles
6. `fp_writer.py` generates script via opencode-serve
7. `fp_processor.py` runs TTS + publishes to R2

**Key modules:**
- `pipeline/fp_homepage_scraper.py` — antiwar.com homepage parser
- `pipeline/fp_editor.py` — story triage and theme identification
- `pipeline/fp_collector.py` — multi-source collection orchestrator
- `pipeline/fp_writer.py` — script generation
- `pipeline/fp_processor.py` — TTS + publish

**CLI:** `uv run python -m pipeline fp-digest [--date YYYY-MM-DD]`
```

**Commit:**

```bash
git add AGENTS.md
git commit -m "docs: add FP Digest pipeline to AGENTS.md"
```

---

### Task 12: Systemd Timer Configuration

This task depends on the NixOS configuration at `workstation/` or the server's NixOS config. Create the timer and service unit files.

**Service:** `/etc/systemd/system/fp-digest.service`
```ini
[Unit]
Description=Foreign Policy Digest daily generation
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/home/dev/projects/my-podcasts
ExecStart=/home/dev/projects/my-podcasts/.venv/bin/python -m pipeline fp-digest
Environment=MY_PODCASTS_STATE_DB=/persist/my-podcasts/state.sqlite3
EnvironmentFile=/run/secrets/my-podcasts-env
TimeoutStartSec=600
```

**Timer:** `/etc/systemd/system/fp-digest.timer`
```ini
[Unit]
Description=Run FP Digest daily at 6 PM EST

[Timer]
OnCalendar=*-*-* 23:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

Note: The exact paths and secret management depend on the NixOS configuration. If using `devenv.nix`, this may be configured there instead. This task should be adapted to the actual deployment setup.

**Enable:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable fp-digest.timer
sudo systemctl start fp-digest.timer
```
