# Generic Source Adapters + arXiv — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Generalize one-off episode ingestion behind a source-adapter interface and add an arXiv (HTML paper) adapter, exposed through a single generic `episode` CLI command that replaces `substack`.

**Architecture:** A shared `Document` dataclass (its own module `pipeline/document.py` to avoid import cycles) + a tiny in-code adapter registry (`pipeline/sources.py`) decouples "fetch + normalize a URL" from "write the episode". Adapters: substack (wraps existing `pipeline/substack.py`) and arXiv (`pipeline/arxiv.py`, metadata via the arXiv Atom API + body via the LaTeXML HTML rendering, report-only). The report writer (`pipeline/report_writer.py`) selects a prompt by the `style` the `Document` declares (`interview` vs `paper`).

**Tech Stack:** Python 3.14, `click`, `requests`, `beautifulsoup4` (html.parser), stdlib `xml.etree.ElementTree`, `pytest`, `uv`. Tests are mock/fixture based (no network). Run with `uv run pytest` and `uv run ruff check <files>`.

**Design doc:** `docs/plans/2026-06-16-generic-source-adapters-design.md`
**Review incorporated:** `/tmp/opencode/plan-review.md` (gpt-5.5; verdict needs-rework → addressed below)
**Issue:** my-podcasts-i83

**Conventions for the executor:**
- TDD: write the failing test, see it fail, implement, see it pass, commit.
- Per-task commits are authorized for this repo; do NOT amend; push at session end.
- LSP errors about unresolved `click`/`bs4`/`requests`/`pytest` are environment noise (LSP isn't using the uv venv). Trust `uv run pytest`.
- ~67 pre-existing repo-wide ruff errors exist (3 in `__main__.py`: B007 ~:67, B904 ~:215, I001 in `poll_blogs_command`). Do NOT fix those; just keep files you touch free of *new* errors.
- After each task: `uv run pytest -q` must stay green (currently 360 passing) and `uv run ruff check <touched files>` clean of new errors.

**Key decisions from the review (apply throughout):**
- `Document` lives in its own module (`pipeline/document.py`) — breaks the `sources`↔`arxiv` cycle (BLOCKER 1).
- The adapter registry calls module attributes at **call time** via thin wrapper functions, so `@patch("pipeline.arxiv.resolve")` actually intercepts (BLOCKER 2).
- arXiv body extraction is article-level `get_text` after decomposing bibliography/math/images and replacing figures/tables with their captions — keeps intro, definitions, theorems, and captions (~6.7k words on the fixture, vs ~4.5k for section-only).
- `Document` carries `byline` and `default_category`; both are actually used (report prompt + show notes; per-source category default).
- arXiv uses the **versioned** id (from the Atom entry) for the HTML fetch / canonical URL / slug.

---

## Task 1: `report_writer.py` — style-keyed report writer (with byline)

A generalized writer holding both prompt styles and an optional author byline.
This is a near-copy of `pipeline/substack_writer.py` generalized with `style`
and `byline` params plus a `paper` template. `substack_writer.py` stays
untouched until Task 4 (suite stays green throughout).

**Files:**
- Create: `pipeline/report_writer.py`
- Create: `pipeline/test_report_writer.py`

**Step 1: Write the failing tests** (`pipeline/test_report_writer.py`)

```python
from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.report_writer import (
    ReportOutput,
    _extract_script,
    _extract_summary,
    build_report_prompt,
    generate_report,
)


def test_interview_style_prompt_mentions_interview():
    prompt = build_report_prompt(body="B", subject="S", style="interview")
    assert "interview" in prompt.lower()
    assert "S" in prompt and "B" in prompt


def test_paper_style_prompt_mentions_paper_framing():
    prompt = build_report_prompt(body="B", subject="S", style="paper")
    low = prompt.lower()
    assert "paper" in low or "research" in low
    assert "transcript" not in low


def test_byline_included_when_present():
    prompt = build_report_prompt(
        body="B", subject="S", style="paper", byline="Jane Doe, John Roe"
    )
    assert "Jane Doe, John Roe" in prompt


def test_byline_omitted_when_empty():
    prompt = build_report_prompt(body="B", subject="S", style="paper", byline="")
    assert "Authors" not in prompt


def test_unknown_style_raises():
    with pytest.raises(ValueError):
        build_report_prompt(body="B", subject="S", style="nope")


def test_extract_script_without_tags_returns_full_text():
    assert _extract_script("no tags here") == "no tags here"


def test_extract_summary_absent_returns_empty():
    assert _extract_summary("<script>x</script>") == ""


@patch("pipeline.report_writer.delete_session")
@patch("pipeline.report_writer.get_last_assistant_text")
@patch("pipeline.report_writer.get_messages")
@patch("pipeline.report_writer.wait_for_idle")
@patch("pipeline.report_writer.send_prompt_async")
@patch("pipeline.report_writer.create_session")
def test_generate_report_extracts_and_passes_paper_style(
    mock_create, mock_send, mock_wait, mock_msgs, mock_text, mock_del
):
    mock_create.return_value = "sess"
    mock_wait.return_value = True
    mock_msgs.return_value = []
    mock_text.return_value = (
        "<summary>Two sentence summary.</summary>"
        "<script>The full spoken script.</script>"
    )

    out = generate_report(
        body="body", subject="Subj", style="paper", byline="Jane Doe"
    )

    assert isinstance(out, ReportOutput)
    assert out.script == "The full spoken script."
    assert out.summary == "Two sentence summary."
    sent = mock_send.call_args.args[1]
    assert ("paper" in sent.lower()) or ("research" in sent.lower())
    assert "Jane Doe" in sent
    mock_del.assert_called_once()  # session cleaned up


@patch("pipeline.report_writer.delete_session")
@patch("pipeline.report_writer.get_last_assistant_text")
@patch("pipeline.report_writer.get_messages")
@patch("pipeline.report_writer.wait_for_idle")
@patch("pipeline.report_writer.send_prompt_async")
@patch("pipeline.report_writer.create_session")
def test_generate_report_rejects_empty_script(
    mock_create, mock_send, mock_wait, mock_msgs, mock_text, mock_del
):
    mock_create.return_value = "sess"
    mock_wait.return_value = True
    mock_msgs.return_value = []
    mock_text.return_value = "<summary>x</summary><script>   </script>"
    with pytest.raises(RuntimeError):
        generate_report(body="b", subject="s", style="interview")
    mock_del.assert_called_once()


@patch("pipeline.report_writer.delete_session")
@patch("pipeline.report_writer.wait_for_idle")
@patch("pipeline.report_writer.send_prompt_async")
@patch("pipeline.report_writer.create_session")
def test_generate_report_times_out_and_cleans_up(
    mock_create, mock_send, mock_wait, mock_del
):
    mock_create.return_value = "sess"
    mock_wait.return_value = False  # never idle
    with pytest.raises(RuntimeError):
        generate_report(body="b", subject="s")
    mock_del.assert_called_once_with("sess")
```

**Step 2: Run to verify they fail**

Run: `uv run pytest pipeline/test_report_writer.py -q` → FAIL (no module).

**Step 3: Implement `pipeline/report_writer.py`**

```python
from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.opencode_client import (
    create_session,
    delete_session,
    get_last_assistant_text,
    get_messages,
    send_prompt_async,
    wait_for_idle,
)

_INTERVIEW_TEMPLATE = """\
You are writing a spoken briefing about a long-form interview podcast.
Your listener does NOT want to hear the transcript read aloud — they want
a clear, structured report on what was discussed.

The post may open with a short written introduction before the transcript
begins; use it for context but focus your report on the conversation itself.

Title: {subject}
{byline}
Below is the transcript. Read it, then produce a spoken briefing
(roughly 1200–2000 words) covering:

- Who participated (host and guest, with affiliations if stated).
- The main themes and questions explored, in the order that best
  illuminates the conversation (not necessarily the order they appeared).
- The key claims, arguments, and evidence each participant offered,
  with attribution ("the guest argued...", "the host pushed back, asking...").
- Notable disagreements, uncertainties, or surprising points.
- Concrete details, numbers, names, and examples that gave the
  conversation weight.

Write for the ear: plain spoken English, no markdown, no bullet points,
no headers. Use natural transitions. You are a smart friend explaining
what an interview got into, not reading a summary out loud. Do not
editorialize beyond what the participants themselves said, and do not
invent facts.

SOURCE TEXT:

{body}
"""

_PAPER_TEMPLATE = """\
You are writing a spoken briefing about an academic research paper.
Your listener wants to understand what the paper argues and why it matters,
without reading it or hearing it read aloud verbatim.

Title: {subject}
{byline}
Below is the paper's text. References and equations have been removed and
figure/table bodies have been dropped, though brief captions may remain.
Read it, then produce a spoken briefing (roughly 1200–2000 words) covering:

- The authors and the research question or problem the paper takes on.
- The paper's central claims and contributions.
- The method, model, or framework used to argue them.
- The key findings or results, with the concrete details that matter.
- Why the work is significant, and any caveats, assumptions, or
  limitations the authors acknowledge.

Write for the ear: plain spoken English, no markdown, no bullet points,
no headers, no LaTeX or math notation. Explain technical ideas plainly,
as a knowledgeable friend would. Do not overstate the results and do not
invent findings the paper does not make.

SOURCE TEXT:

{body}
"""

_TEMPLATES = {"interview": _INTERVIEW_TEMPLATE, "paper": _PAPER_TEMPLATE}


@dataclass(frozen=True)
class ReportOutput:
    script: str
    summary: str  # structural parity with chinatalk_writer; not surfaced.


def build_report_prompt(
    *, body: str, subject: str, style: str = "interview", byline: str = ""
) -> str:
    try:
        template = _TEMPLATES[style]
    except KeyError:
        raise ValueError(f"Unknown report style: {style!r}") from None
    byline_line = f"Authors/Participants: {byline}\n" if byline else ""
    return template.format(subject=subject, body=body, byline=byline_line)


def _extract_script(text: str) -> str:
    m = re.search(r"<script>\s*(.*?)\s*</script>", text, re.DOTALL)
    return m.group(1).strip() if m else text


def _extract_summary(text: str) -> str:
    m = re.search(r"<summary>\s*(.*?)\s*</summary>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def generate_report(
    *, body: str, subject: str, style: str = "interview", byline: str = ""
) -> ReportOutput:
    """Generate a spoken-briefing report on a source document."""
    prompt = build_report_prompt(
        body=body, subject=subject, style=style, byline=byline
    )
    instruction = (
        "Read the following source text and produce the spoken briefing. "
        "First write a 2-3 sentence summary wrapped in <summary>...</summary> "
        "tags. Then write the full spoken script wrapped in "
        "<script>...</script> tags. Output nothing outside these tags.\n\n"
        + prompt
    )

    session_id = create_session()
    try:
        send_prompt_async(session_id, instruction)
        if not wait_for_idle(session_id, timeout=900):
            raise RuntimeError("report writer did not complete within 900 seconds")
        messages = get_messages(session_id)
        full_text = get_last_assistant_text(messages).strip()
        script = _extract_script(full_text)
        summary = _extract_summary(full_text)
        if not script.strip():
            raise RuntimeError("report writer returned empty script")
        return ReportOutput(script=script, summary=summary)
    finally:
        delete_session(session_id)
```

**Step 4: Run to verify pass**

Run: `uv run pytest pipeline/test_report_writer.py -q` → PASS
Run: `uv run ruff check pipeline/report_writer.py pipeline/test_report_writer.py` → clean

**Step 5: Commit**

```bash
git add pipeline/report_writer.py pipeline/test_report_writer.py
git commit -m "Add style-keyed report_writer (interview + paper, with byline)"
```

---

## Task 2: `Document` model + adapter registry

`Document` gets its own module so `sources` and `arxiv` can both import it with
no cycle. The registry calls module attributes at call time (patchable).

**Files:**
- Create: `pipeline/document.py`
- Create: `pipeline/sources.py`
- Create: `pipeline/arxiv.py` (stub; fleshed out in Task 3)
- Create: `pipeline/test_sources.py`

**Step 1: Write the failing tests** (`pipeline/test_sources.py`)

```python
from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.document import Document
from pipeline.sources import resolve_document


def _doc(**kw) -> Document:
    base = dict(
        title="T", byline="B", canonical_url="https://x/y", description="D",
        report_text="R", read_html="<p>H</p>", slug="s", style="interview",
        wordcount=1, default_category="Technology",
    )
    base.update(kw)
    return Document(**base)


@patch("pipeline.arxiv.resolve")
def test_dispatch_routes_arxiv_url(mock_arxiv_resolve):
    mock_arxiv_resolve.return_value = _doc(style="paper")
    doc = resolve_document("https://arxiv.org/html/2407.16314v1")
    assert doc.style == "paper"
    mock_arxiv_resolve.assert_called_once()


@patch("pipeline.sources._substack_resolve")
def test_dispatch_routes_substack_url(mock_sub_resolve):
    mock_sub_resolve.return_value = _doc(style="interview")
    doc = resolve_document("https://www.dwarkesh.com/p/david-reich-2")
    assert doc.style == "interview"
    mock_sub_resolve.assert_called_once()


@patch("pipeline.sources._substack_resolve")
def test_bare_numeric_id_routes_substack(mock_sub_resolve):
    mock_sub_resolve.return_value = _doc()
    resolve_document("123456")
    mock_sub_resolve.assert_called_once()


@patch("pipeline.arxiv.resolve")
def test_explicit_source_override(mock_arxiv_resolve):
    mock_arxiv_resolve.return_value = _doc(style="paper")
    resolve_document("anything", source="arxiv")
    mock_arxiv_resolve.assert_called_once()


def test_no_match_raises():
    with pytest.raises(ValueError):
        resolve_document("https://example.com/random")


def test_unknown_source_raises():
    with pytest.raises(ValueError):
        resolve_document("https://arxiv.org/abs/1", source="bogus")
```

**Step 2: Run to verify fail**

Run: `uv run pytest pipeline/test_sources.py -q` → FAIL.

**Step 3a: Implement `pipeline/document.py`**

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    title: str
    byline: str
    canonical_url: str
    description: str
    report_text: str
    read_html: str | None
    slug: str
    style: str
    wordcount: int
    default_category: str
```

**Step 3b: Implement `pipeline/arxiv.py` (stub)**

```python
from __future__ import annotations

import re
from urllib.parse import urlparse

from pipeline.document import Document  # noqa: F401  (used in Task 3)

_ID_RE = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?)")


def matches(url: str) -> bool:
    ref = url.strip()
    host = urlparse(ref).netloc.lower()
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        return True
    bare = re.sub(r"(?i)^arxiv:", "", ref)
    return bool(_ID_RE.fullmatch(bare))


def resolve(url: str):  # pragma: no cover - replaced in Task 3
    raise NotImplementedError
```

**Step 3c: Implement `pipeline/sources.py`**

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pipeline import arxiv
from pipeline import substack as substack_mod
from pipeline.document import Document


@dataclass(frozen=True)
class Adapter:
    name: str
    matches: Callable[[str], bool]
    resolve: Callable[[str], Document]


# Wrappers defer attribute lookup to call time so tests can patch
# pipeline.arxiv.resolve / pipeline.sources._substack_resolve.
def _arxiv_matches(url: str) -> bool:
    return arxiv.matches(url)


def _arxiv_resolve(url: str) -> Document:
    return arxiv.resolve(url)


def _substack_matches(url: str) -> bool:
    ref = url.strip().lower()
    if ref.isdigit():  # bare Substack post id (legacy substack-command behavior)
        return True
    return (
        "substack.com" in ref
        or "/p/" in ref
        or "/p-" in ref
        or ".dwarkesh.com" in ref
    )


def _substack_resolve(url: str) -> Document:
    post = substack_mod.resolve_post(url)
    return Document(
        title=post.title,
        byline=post.subtitle,
        canonical_url=post.canonical_url,
        description=post.subtitle or post.description,
        report_text=substack_mod.html_to_clean_text(post.body_html),
        read_html=post.body_html,
        slug=post.slug or "post",
        style="interview",
        wordcount=post.wordcount,
        default_category="Technology",
    )


# Order: arxiv first (specific), substack last (permissive fallback).
ADAPTERS: list[Adapter] = [
    Adapter("arxiv", _arxiv_matches, _arxiv_resolve),
    Adapter("substack", _substack_matches, _substack_resolve),
]


def resolve_document(url: str, *, source: str | None = None) -> Document:
    if source is not None:
        for a in ADAPTERS:
            if a.name == source:
                return a.resolve(url)
        raise ValueError(f"Unknown source adapter: {source!r}")
    for a in ADAPTERS:
        if a.matches(url):
            return a.resolve(url)
    raise ValueError(f"No source adapter matched: {url!r}")
```

**Step 4: Run to verify pass**

Run: `uv run pytest pipeline/test_sources.py -q` → PASS
(`test_dispatch_routes_arxiv_url` works because `_arxiv_resolve` calls
`arxiv.resolve` at call time, which `@patch("pipeline.arxiv.resolve")` replaces.)

**Step 5: Commit**

```bash
git add pipeline/document.py pipeline/sources.py pipeline/arxiv.py pipeline/test_sources.py
git commit -m "Add Document model + source-adapter registry (substack + arxiv stub)"
```

---

## Task 3: arXiv adapter — full implementation

Implement id parsing, Atom metadata (versioned id), HTML body extraction.

**Files:**
- Modify: `pipeline/arxiv.py`
- Create: `pipeline/test_arxiv.py`
- Create fixtures: `pipeline/fixtures/arxiv_api.xml`, `pipeline/fixtures/arxiv_paper.html`

**Step 0: Save fixtures**

```bash
mkdir -p pipeline/fixtures
cp /tmp/opencode/arxiv_api.xml pipeline/fixtures/arxiv_api.xml
cp /tmp/opencode/arxiv_html.html pipeline/fixtures/arxiv_paper.html
```
If the temp files are gone, re-fetch:
```bash
curl -sSL "http://export.arxiv.org/api/query?id_list=2407.16314" -o pipeline/fixtures/arxiv_api.xml
curl -sSL "https://arxiv.org/html/2407.16314v1" -o pipeline/fixtures/arxiv_paper.html
```

**Step 1: Write the failing tests** (`pipeline/test_arxiv.py`)

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline import arxiv

_FIX = Path(__file__).parent / "fixtures"


def _resp(text: str, status: int = 200):
    r = MagicMock()
    r.text = text
    r.status_code = status
    r.raise_for_status = MagicMock()
    return r


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("https://arxiv.org/html/2407.16314v1", "2407.16314v1"),
        ("https://arxiv.org/abs/2407.16314", "2407.16314"),
        ("https://arxiv.org/pdf/2407.16314v2", "2407.16314v2"),
        ("2407.16314", "2407.16314"),
        ("arXiv:2407.16314v1", "2407.16314v1"),
        ("ARXIV:2407.16314", "2407.16314"),
    ],
)
def test_parse_id(ref, expected):
    assert arxiv.parse_arxiv_id(ref) == expected


def test_parse_id_rejects_garbage():
    with pytest.raises(ValueError):
        arxiv.parse_arxiv_id("https://example.com/not-a-paper")


def test_matches_host_and_bare_id():
    assert arxiv.matches("https://arxiv.org/html/2407.16314v1")
    assert arxiv.matches("2407.16314")
    assert arxiv.matches("arXiv:2407.16314v1")
    assert not arxiv.matches("https://www.dwarkesh.com/p/x")
    assert not arxiv.matches("https://notarxiv.org/abs/2407.16314")


def test_fixture_sanity():
    # Guard against future fixture replacement silently weakening coverage.
    from bs4 import BeautifulSoup

    soup = BeautifulSoup((_FIX / "arxiv_paper.html").read_text("utf-8"), "html.parser")
    assert len(soup.find_all("section", class_="ltx_section")) == 6
    assert soup.select("section.ltx_bibliography, ul.ltx_biblist")


@patch("pipeline.arxiv.requests.get")
def test_resolve_builds_paper_document(mock_get):
    api_xml = (_FIX / "arxiv_api.xml").read_text("utf-8")
    paper_html = (_FIX / "arxiv_paper.html").read_text("utf-8")
    calls = []

    def _by_url(url, *a, **k):
        calls.append(url)
        return _resp(api_xml if "export.arxiv.org" in url else paper_html)

    mock_get.side_effect = _by_url
    doc = arxiv.resolve("https://arxiv.org/abs/2407.16314")  # unversioned input

    assert doc.title == "Capital as Artificial Intelligence"
    assert "Carissimo" in doc.byline and "Korecki" in doc.byline
    assert doc.canonical_url == "https://arxiv.org/abs/2407.16314v1"
    assert doc.style == "paper"
    assert doc.read_html is None
    assert doc.default_category == "Science"
    assert doc.slug == "2407-16314v1"
    assert "Capital" in doc.description
    # HTML fetched with the *versioned* id derived from the Atom entry.
    html_calls = [c for c in calls if "/html/" in c]
    assert html_calls == ["https://arxiv.org/html/2407.16314v1"]


@patch("pipeline.arxiv.requests.get")
def test_report_text_quality(mock_get):
    api_xml = (_FIX / "arxiv_api.xml").read_text("utf-8")
    paper_html = (_FIX / "arxiv_paper.html").read_text("utf-8")
    mock_get.side_effect = lambda url, *a, **k: _resp(
        api_xml if "export.arxiv.org" in url else paper_html
    )
    doc = arxiv.resolve("https://arxiv.org/html/2407.16314v1")
    t = doc.report_text
    for heading in [
        "Capital as a Historical Agential System",
        "The Agents of Capital",
        "The Entropy of Capital",
        "The Artificial Intelligence in Capital",
        "A Pragmatic Perspective",
        "Conclusion",
    ]:
        assert heading in t
    assert "Definition 1" in t                       # theorem/definition kept
    assert "The relationship between generating processes" in t  # figure caption kept
    assert "Barreto" not in t                         # bibliography excluded
    assert 5000 < len(t.split()) < 8500               # realistic body, no bib blow-up


@patch("pipeline.arxiv.requests.get")
def test_resolve_no_html_rendering_raises(mock_get):
    api_xml = (_FIX / "arxiv_api.xml").read_text("utf-8")

    def _by_url(url, *a, **k):
        if "export.arxiv.org" in url:
            return _resp(api_xml)
        return _resp("", status=404)

    mock_get.side_effect = _by_url
    with pytest.raises(ValueError, match="(?i)html"):
        arxiv.resolve("https://arxiv.org/abs/2407.16314")
```

**Step 2: Run to verify fail**

Run: `uv run pytest pipeline/test_arxiv.py -q` → FAIL.

**Step 3: Implement `pipeline/arxiv.py`** (replace the stub `resolve`, keep
`matches` from Task 2, add the rest)

```python
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from pipeline.document import Document

_ID_RE = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?)")
_ATOM = "{http://www.w3.org/2005/Atom}"


def matches(url: str) -> bool:
    ref = url.strip()
    host = urlparse(ref).netloc.lower()
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        return True
    bare = re.sub(r"(?i)^arxiv:", "", ref)
    return bool(_ID_RE.fullmatch(bare))


def parse_arxiv_id(url_or_id: str) -> str:
    ref = re.sub(r"(?i)^arxiv:", "", url_or_id.strip())
    m = _ID_RE.search(ref)
    if not m:
        raise ValueError(f"Unrecognized arXiv reference: {url_or_id!r}")
    return m.group(1)


def _fetch_metadata(arxiv_id: str, *, timeout: int) -> dict:
    api_url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
    resp = requests.get(api_url, timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    entry = root.find(f"{_ATOM}entry")
    if entry is None:
        raise ValueError(f"arXiv API returned no entry for {arxiv_id!r}")
    title = " ".join((entry.findtext(f"{_ATOM}title") or "").split())
    summary = " ".join((entry.findtext(f"{_ATOM}summary") or "").split())
    authors = [
        " ".join((a.findtext(f"{_ATOM}name") or "").split())
        for a in entry.findall(f"{_ATOM}author")
    ]
    abs_id = (entry.findtext(f"{_ATOM}id") or "").strip()
    # abs_id looks like http://arxiv.org/abs/2407.16314v1 — derive versioned id.
    versioned = parse_arxiv_id(abs_id) if abs_id else arxiv_id
    canonical = (
        abs_id.replace("http://", "https://")
        if abs_id
        else f"https://arxiv.org/abs/{versioned}"
    )
    return {
        "title": title,
        "summary": summary,
        "authors": [a for a in authors if a],
        "canonical_url": canonical,
        "versioned_id": versioned,
    }


def _fetch_body_html(versioned_id: str, *, timeout: int) -> str:
    html_url = f"https://arxiv.org/html/{versioned_id}"
    resp = requests.get(html_url, timeout=timeout)
    if resp.status_code == 404:
        raise ValueError(
            f"arXiv paper {versioned_id!r} has no HTML rendering (PDF only); "
            "cannot build a report."
        )
    resp.raise_for_status()
    return resp.text


def _html_to_report_text(body_html: str) -> str:
    soup = BeautifulSoup(body_html, "html.parser")
    article = soup.find("article") or soup
    for tag in ("script", "style", "nav"):
        for n in article.find_all(tag):
            n.decompose()
    for n in article.select("section.ltx_bibliography, ul.ltx_biblist"):
        n.decompose()
    for n in article.find_all("math"):
        n.decompose()
    for n in article.find_all("img"):
        n.decompose()
    # Replace figures (incl. ltx_table figures) with their caption text only.
    for fig in article.find_all("figure"):
        cap = fig.find("figcaption")
        fig.replace_with(cap.get_text(" ", strip=True) if cap else "")
    # Replace any remaining standalone tables with their caption text only.
    for tbl in article.find_all("table"):
        cap = tbl.find("caption")
        tbl.replace_with(cap.get_text(" ", strip=True) if cap else "")
    text = article.get_text(separator="\n\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def resolve(url: str, *, timeout: int = 30) -> Document:
    arxiv_id = parse_arxiv_id(url)
    meta = _fetch_metadata(arxiv_id, timeout=timeout)
    versioned = meta["versioned_id"]
    body_html = _fetch_body_html(versioned, timeout=timeout)
    report_text = _html_to_report_text(body_html)
    if not report_text.strip():
        raise ValueError(f"arXiv HTML produced empty report text for {versioned!r}")
    return Document(
        title=meta["title"],
        byline=", ".join(meta["authors"]),
        canonical_url=meta["canonical_url"],
        description=meta["summary"],
        report_text=report_text,
        read_html=None,
        slug=versioned.replace(".", "-"),
        style="paper",
        wordcount=len(report_text.split()),
        default_category="Science",
    )
```

> NOTE: old-style arXiv ids (e.g. `math.GT/0309136`) are **out of scope**;
> `_ID_RE` only matches modern `NNNN.NNNNN[vN]` ids. Acceptable for current use.

**Step 4: Run to verify pass**

Run: `uv run pytest pipeline/test_arxiv.py pipeline/test_sources.py -q` → PASS
Run: `uv run ruff check pipeline/arxiv.py pipeline/test_arxiv.py` → clean

**Step 5: Commit**

```bash
git add pipeline/arxiv.py pipeline/test_arxiv.py pipeline/fixtures/
git commit -m "Implement arXiv adapter (versioned-id metadata + full HTML body extraction)"
```

---

## Task 4: `episode` CLI command (replaces `substack`)

**Files:**
- Modify: `pipeline/__main__.py` (remove `substack_command` ~:840-954; add `episode_command`)
- Create: `pipeline/test_episode_cli.py`
- Delete: `pipeline/test_substack_cli.py`, `pipeline/substack_writer.py`, `pipeline/test_substack_writer.py`
  (`pipeline/substack.py` stays — it's the substack adapter's engine.)

**Step 1: Write the failing tests** (`pipeline/test_episode_cli.py`)

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from pipeline.__main__ import cli
from pipeline.document import Document
from pipeline.report_writer import ReportOutput


def _interview_doc() -> Document:
    return Document(
        title="David Reich – Bronze Age", byline="A subtitle",
        canonical_url="https://www.dwarkesh.com/p/david-reich-2",
        description="desc", report_text="Dwarkesh Patel: Hi.",
        read_html="<p>Hi.</p>", slug="david-reich-2", style="interview",
        wordcount=21163, default_category="Technology",
    )


def _paper_doc() -> Document:
    return Document(
        title="Capital as Artificial Intelligence",
        byline="Cesare Carissimo, Marcin Korecki",
        canonical_url="https://arxiv.org/abs/2407.16314v1",
        description="We gather many perspectives on Capital.",
        report_text="Abstract\n\nWe gather...", read_html=None,
        slug="2407-16314v1", style="paper", wordcount=9000,
        default_category="Science",
    )


def _patch_env(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pipeline.__main__._default_state_db_path", lambda: tmp_path / "s.sqlite3"
    )
    monkeypatch.setattr("pipeline.__main__.R2Client", lambda: object())


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_interview_report(mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _interview_doc()
    mock_report.return_value = ReportOutput(script="Briefing.", summary="B.")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://www.dwarkesh.com/p/david-reich-2",
        "--mode", "report", "--feed-slug", "dwarkesh"])
    assert res.exit_code == 0, res.output
    assert mock_report.call_args.kwargs["style"] == "interview"
    assert mock_report.call_args.kwargs["byline"] == "A subtitle"
    kw = mock_publish.call_args.kwargs
    assert kw["title"] == "Report: David Reich – Bronze Age"
    assert kw["source_url"] == "https://www.dwarkesh.com/p/david-reich-2"
    assert kw["category"] == "Technology"  # from doc.default_category


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_paper_report_uses_paper_style_and_science_category(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _paper_doc()
    mock_report.return_value = ReportOutput(script="Paper briefing.", summary="B.")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://arxiv.org/html/2407.16314v1",
        "--feed-slug", "papers"])
    assert res.exit_code == 0, res.output
    assert mock_report.call_args.kwargs["style"] == "paper"
    kw = mock_publish.call_args.kwargs
    assert kw["title"] == "Report: Capital as Artificial Intelligence"
    assert kw["category"] == "Science"  # from doc.default_category


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_style_override(mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _paper_doc()
    mock_report.return_value = ReportOutput(script="x", summary="y")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://arxiv.org/html/2407.16314v1",
        "--feed-slug", "papers", "--style", "interview"])
    assert res.exit_code == 0, res.output
    assert mock_report.call_args.kwargs["style"] == "interview"


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_category_override_wins(mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _paper_doc()
    mock_report.return_value = ReportOutput(script="x", summary="y")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://arxiv.org/html/2407.16314v1",
        "--feed-slug", "papers", "--category", "News"])
    assert res.exit_code == 0, res.output
    assert mock_publish.call_args.kwargs["category"] == "News"


@patch("pipeline.blog_poller.adapt_for_audio")
@patch("pipeline.script_processor.publish_script")
@patch("pipeline.sources.resolve_document")
def test_substack_read_mode_uses_adapter_and_plain_title(
    mock_resolve, mock_publish, mock_adapt, tmp_path, monkeypatch
):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _interview_doc()
    mock_adapt.return_value = "Spoken adaptation."
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://www.dwarkesh.com/p/david-reich-2",
        "--mode", "read", "--feed-slug", "dwarkesh"])
    assert res.exit_code == 0, res.output
    mock_adapt.assert_called_once()
    kw = mock_publish.call_args.kwargs
    assert kw["title"] == "David Reich – Bronze Age"
    assert kw["source_url"] == "https://www.dwarkesh.com/p/david-reich-2"


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.sources.resolve_document")
def test_arxiv_read_mode_unsupported_errors(
    mock_resolve, mock_publish, tmp_path, monkeypatch
):
    mock_resolve.return_value = _paper_doc()  # read_html is None
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://arxiv.org/html/2407.16314v1",
        "--mode", "read", "--feed-slug", "papers"])
    assert res.exit_code != 0
    assert "read" in res.output.lower()
    mock_publish.assert_not_called()


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_script_file_skips_generation(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _interview_doc()
    captured = {}

    def _cap(**kw):
        captured["title"] = kw["title"]
        captured["feed_slug"] = kw["feed_slug"]
        captured["source_url"] = kw["source_url"]
        captured["script"] = Path(kw["script_file"]).read_text("utf-8")
        return MagicMock()

    mock_publish.side_effect = _cap
    reviewed = tmp_path / "r.txt"
    reviewed.write_text("Exact reviewed text.", encoding="utf-8")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://www.dwarkesh.com/p/david-reich-2",
        "--feed-slug", "dwarkesh", "--script-file", str(reviewed)])
    assert res.exit_code == 0, res.output
    mock_report.assert_not_called()
    assert captured["script"] == "Exact reviewed text."
    assert captured["title"] == "Report: David Reich – Bronze Age"
    assert captured["feed_slug"] == "dwarkesh"
    assert captured["source_url"] == "https://www.dwarkesh.com/p/david-reich-2"


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_dry_run_writes_and_does_not_publish(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    mock_resolve.return_value = _interview_doc()
    mock_report.return_value = ReportOutput(script="The briefing.", summary="B.")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://www.dwarkesh.com/p/david-reich-2",
        "--feed-slug", "dwarkesh", "--dry-run"])
    assert res.exit_code == 0, res.output
    mock_publish.assert_not_called()
    printed = res.output.strip().splitlines()[-1]
    path = Path(printed.split(": ", 1)[1])
    assert path.exists() and "The briefing." in path.read_text("utf-8")
```

**Step 2: Run to verify fail**

Run: `uv run pytest pipeline/test_episode_cli.py -q` → FAIL (no `episode` command).

**Step 3: Implement** — replace the entire `substack_command` block (~:840-954)
in `pipeline/__main__.py` with:

```python
@cli.command("episode")
@click.option("--url", required=True, type=str, help="Source URL or id.")
@click.option(
    "--source", default=None, type=click.Choice(["arxiv", "substack"]),
    help="Force a source adapter (otherwise auto-detected from the URL).",
)
@click.option(
    "--mode", type=click.Choice(["report", "read"]), default="report",
    show_default=True, help="report: spoken briefing; read: faithful full reading.",
)
@click.option("--feed-slug", "feed_slug", required=True, type=str)
@click.option(
    "--style", default=None, type=click.Choice(["interview", "paper"]),
    help="Override the report prompt style (defaults to the source's style).",
)
@click.option(
    "--title", default=None, type=str,
    help="Override episode title (report mode prepends 'Report: ' if not set).",
)
@click.option("--voice", default="nova", show_default=True, type=str)
@click.option(
    "--category", default=None, type=str,
    help="Override iTunes category (defaults to the source's category).",
)
@click.option("--date", "date_str", default=None, type=str, help="Date (YYYY-MM-DD).")
@click.option(
    "--script-file", "script_file_opt", default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=("Publish this pre-written script verbatim, skipping generation. "
          "Metadata (title, source link, show notes) still comes from the source."),
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Generate the script only; skip TTS and publish.",
)
def episode_command(
    url: str, source: str | None, mode: str, feed_slug: str, style: str | None,
    title: str | None, voice: str, category: str | None, date_str: str | None,
    script_file_opt: Path | None, dry_run: bool,
) -> None:
    """Turn a source URL (Substack post, arXiv paper, ...) into a one-off episode."""
    import tempfile
    from datetime import UTC, datetime

    from pipeline import report_writer, sources

    if date_str is None:
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    click.echo(f"Resolving {url} ...")
    try:
        doc = sources.resolve_document(url, source=source)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Title: {doc.title} ({doc.wordcount} words, style={doc.style})")

    if mode == "read" and doc.read_html is None:
        raise click.ClickException(
            f"read mode is not supported for this source (style={doc.style}); "
            "use --mode report."
        )

    if script_file_opt is not None:
        script_text = script_file_opt.read_text(encoding="utf-8")
        episode_title = title or (
            f"Report: {doc.title}" if mode == "report" else doc.title
        )
        click.echo(
            f"Using pre-written script ({len(script_text)} chars); skipping generation."
        )
    elif mode == "report":
        out = report_writer.generate_report(
            body=doc.report_text, subject=doc.title,
            style=style or doc.style, byline=doc.byline,
        )
        script_text = out.script
        episode_title = title or f"Report: {doc.title}"
    else:  # read
        from pipeline.blog_poller import adapt_for_audio

        click.echo("Adapting source for audio...")
        adapted = adapt_for_audio(doc.read_html, doc.title)
        if not adapted:
            raise click.ClickException("Audio adaptation failed (is GEMINI_API_KEY set?).")
        script_text = adapted
        episode_title = title or doc.title

    if dry_run:
        out_path = (
            Path(tempfile.gettempdir()) / f"episode-{doc.slug or 'post'}-{date_str}.txt"
        )
        out_path.write_text(script_text, encoding="utf-8")
        click.echo("Dry run complete. No episode published.")
        click.echo(f"Script: {out_path}")
        return

    notes_md = f"## Episode Summary\n\n{doc.description}\n\n"
    if doc.byline:
        notes_md += f"By {doc.byline}\n\n"
    notes_md += f"---\n\n[Original source]({doc.canonical_url})\n"

    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()
        with tempfile.TemporaryDirectory(prefix="episode-") as tmp_dir:
            tmp = Path(tmp_dir)
            script_file = tmp / "script.md"
            script_file.write_text(script_text, encoding="utf-8")
            notes_file = tmp / "notes.md"
            notes_file.write_text(notes_md, encoding="utf-8")

            result = script_processor.publish_script(
                script_file=script_file, title=episode_title, feed_slug=feed_slug,
                store=store, r2_client=r2_client, show_notes_file=notes_file,
                voice=voice, category=(category or doc.default_category),
                date_str=date_str, source_url=doc.canonical_url or None,
            )
        click.echo(f"Published: {result.r2_key}")
        click.echo(f"Title: {result.title}")
        click.echo(f"Feed: {result.feed_slug}")
        if result.duration_seconds is not None:
            click.echo(f"Duration: {result.duration_seconds} sec")
    finally:
        store.close()
```

**Step 3b: Delete superseded files**

```bash
git rm pipeline/substack_writer.py pipeline/test_substack_writer.py pipeline/test_substack_cli.py
```

**Step 4: Run the full suite**

Run: `uv run pytest -q` → all green.
Run: `uv run ruff check pipeline/__main__.py pipeline/test_episode_cli.py` → no new errors.

**Step 5: Commit**

```bash
git add -A pipeline/
git commit -m "Replace substack command with generic episode command (URL-dispatched)"
```

---

## Task 5: Docs — AGENTS.md

**Files:** Modify `AGENTS.md` ("One-Off Substack Episodes" → "One-Off Episodes (Source Adapters)").

**Step 1:** Rewrite that section:
- New CLI: `uv run python -m pipeline episode --url <url-or-id> [--source {arxiv,substack}] --mode {report|read} --feed-slug <slug> [--style {interview,paper}] [--title ...] [--voice ...] [--category ...] [--date ...] [--script-file PATH] [--dry-run]`
- Note `substack` is **replaced** by `episode` (substack URLs/ids auto-detected; bare numeric Substack ids still route to substack).
- Adapter model: `pipeline/document.py` (`Document`, with `byline` + `default_category`), `pipeline/sources.py` (registry + dispatch). substack adapter = style `interview`, read supported, category `Technology`. arXiv adapter = style `paper`, **report-only**, category `Science`; metadata via Atom API (versioned id), body via `/html` LaTeXML (drops refs/math/figure bodies, keeps captions).
- Key modules: `pipeline/sources.py`, `pipeline/document.py`, `pipeline/arxiv.py`, `pipeline/report_writer.py`, `pipeline/substack.py`.
- Update "Core Paths" bullets.

**Step 2: Commit**

```bash
git add AGENTS.md
git commit -m "Docs: generalize one-off episodes to source adapters + arXiv"
```

---

## Task 6: Publish the arXiv "Capital as AI" episode (operational — needs human OK)

Touches production R2 + DB. **Do not run unattended.** Requires the operator to
(a) pick a feed slug, (b) review the dry-run, (c) approve.

**Step 1: Dry-run (real network, no publish)**

```bash
uv run python -m pipeline episode \
  --url https://arxiv.org/html/2407.16314v1 \
  --mode report --feed-slug <DECIDE> --dry-run
```
Read the printed script artifact: authors named, claims/method/findings covered,
no LaTeX/markdown, coherent.

**Step 2: Human approval gate.** Present the script; get explicit go-ahead.

**Step 3: Publish the reviewed script verbatim** (export prod secrets from
`/run/secrets/*` first — `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`, `OPENAI_API_KEY`):

```bash
R2_ACCOUNT_ID="$(cat /run/secrets/r2_account_id)" \
R2_ACCESS_KEY_ID="$(cat /run/secrets/r2_access_key_id)" \
R2_SECRET_ACCESS_KEY="$(cat /run/secrets/r2_secret_access_key)" \
OPENAI_API_KEY="$(cat /run/secrets/openai_api_key)" \
uv run python -m pipeline episode \
  --url https://arxiv.org/html/2407.16314v1 \
  --feed-slug <DECIDE> --script-file <reviewed-artifact-path>
```
(`--category` defaults to `Science` for arXiv; pass `--category` to override.)

**Step 4: Verify** the feed lists the item with the source `<link>` and the mp3
is HTTP 200 (`curl -sSI https://podcast.mohrbacher.dev/feeds/<slug>.xml`).

**Step 5:** If a new feed was created, add its subscription URL to the root
`AGENTS.md` Quick Start and (optionally) add cover art per the cover-art flow.

---

## Closeout

- `uv run pytest -q` green; touched files ruff-clean of new errors.
- `git pull --rebase && git push`; confirm `up to date with origin`.
- `bd close my-podcasts-i83` (file a follow-up for Task 6 if the episode isn't
  published this session).
