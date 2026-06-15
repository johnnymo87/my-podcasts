# Substack Post -> Podcast Command Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a reusable `substack` CLI command that turns any Substack post (by URL or id) into a one-off podcast episode, in either `report` (briefing) or `read` (full reading) mode.

**Architecture:** A new `pipeline/substack.py` ingests via the Substack JSON API and normalizes the HTML (stripping timestamps, Sponsors, and the Timestamps TOC). A new `pipeline/substack_writer.py` generates a briefing via opencode-serve (mirroring `yglesias_writer.py`). Read mode reuses `blog_poller.adapt_for_audio`. Both modes publish through the existing `script_processor.publish_script` (extended with an optional `source_url`).

**Tech Stack:** Python 3, click, requests, beautifulsoup4 (html.parser), opencode-serve client, Gemini (read mode), ttsjoin/ffprobe, pytest.

Design doc: `docs/plans/2026-06-15-substack-podcast-report-design.md`. Beads: my-podcasts-78k.

---

## Task 1: Extend `publish_script` with optional `source_url`

**Files:**
- Modify: `pipeline/script_processor.py:152` (signature) and `:221` (`source_url=None`)
- Test: `pipeline/test_script_processor.py`

**Step 1: Write the failing test**

Add to `pipeline/test_script_processor.py`:

```python
def test_publish_script_sets_source_url(tmp_path, monkeypatch) -> None:
    """When source_url is passed, it is stored on the episode (feed <link>)."""
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    script_file = tmp_path / "script.md"
    script_file.write_text("Plain script.", encoding="utf-8")

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
        "pipeline.script_processor.regenerate_and_upload_feed", lambda s, r: None
    )
    monkeypatch.setattr(
        "pipeline.script_processor.SCRIPT_ARCHIVE_ROOT", tmp_path / "arch"
    )

    publish_script(
        script_file=script_file,
        title="Linked Episode",
        feed_slug="dwarkesh",
        store=store,
        r2_client=r2_client,
        source_url="https://www.dwarkesh.com/p/david-reich-2",
        date_str="2026-06-15",
    )

    episodes = store.list_episodes(feed_slug="dwarkesh")
    assert len(episodes) == 1
    assert episodes[0].source_url == "https://www.dwarkesh.com/p/david-reich-2"

    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_script_processor.py::test_publish_script_sets_source_url -v`
Expected: FAIL — `publish_script() got an unexpected keyword argument 'source_url'`.

**Step 3: Write minimal implementation**

In `pipeline/script_processor.py`, add the parameter to `publish_script` (after `category`):

```python
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
    source_url: str | None = None,
) -> PublishResult:
```

And change the `Episode(...)` construction (currently `source_url=None,`) to:

```python
        source_url=source_url,
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_script_processor.py -v`
Expected: PASS (all, including the new test and the existing ones).

**Step 5: Commit**

```bash
git add pipeline/script_processor.py pipeline/test_script_processor.py
git commit -m "feat: add optional source_url to publish_script"
```

---

## Task 2: Substack post resolution + fetch (`pipeline/substack.py`)

**Files:**
- Create: `pipeline/substack.py`
- Test: `pipeline/test_substack.py`

**Step 1: Write the failing tests**

Create `pipeline/test_substack.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.substack import SubstackPost, _api_url, resolve_post


def test_api_url_numeric_id():
    assert _api_url("196892360") == (
        "https://substack.com/api/v1/posts/by-id/196892360"
    )


def test_api_url_short_link():
    assert _api_url("https://substack.com/@dwarkesh/p-196892360") == (
        "https://substack.com/api/v1/posts/by-id/196892360"
    )


def test_api_url_canonical_slug():
    assert _api_url("https://www.dwarkesh.com/p/david-reich-2") == (
        "https://www.dwarkesh.com/api/v1/posts/david-reich-2"
    )


def test_api_url_rejects_garbage():
    with pytest.raises(ValueError):
        _api_url("https://www.dwarkesh.com/about")


def _fake_response(post: dict):
    resp = MagicMock()
    resp.json.return_value = {"post": post}
    resp.raise_for_status.return_value = None
    return resp


@patch("pipeline.substack.requests.get")
def test_resolve_post_happy_path(mock_get):
    mock_get.return_value = _fake_response(
        {
            "title": "David Reich – Bronze Age",
            "subtitle": "A subtitle",
            "description": "desc",
            "canonical_url": "https://www.dwarkesh.com/p/david-reich-2",
            "slug": "david-reich-2",
            "audience": "everyone",
            "wordcount": 21163,
            "body_html": "<p>Hello world.</p>",
        }
    )

    post = resolve_post("https://www.dwarkesh.com/p/david-reich-2")

    assert isinstance(post, SubstackPost)
    assert post.title == "David Reich – Bronze Age"
    assert post.canonical_url == "https://www.dwarkesh.com/p/david-reich-2"
    assert post.body_html == "<p>Hello world.</p>"
    mock_get.assert_called_once()


@patch("pipeline.substack.requests.get")
def test_resolve_post_rejects_paywalled(mock_get):
    mock_get.return_value = _fake_response(
        {
            "title": "Paid Post",
            "audience": "only_paid",
            "should_send_free_preview": True,
            "truncated_body_text": "preview...",
            "body_html": "<p>preview only</p>",
            "canonical_url": "https://x.substack.com/p/paid",
            "slug": "paid",
        }
    )
    with pytest.raises(ValueError, match="paywall"):
        resolve_post("https://x.substack.com/p/paid")


@patch("pipeline.substack.requests.get")
def test_resolve_post_rejects_empty_body(mock_get):
    mock_get.return_value = _fake_response(
        {
            "title": "Empty",
            "audience": "everyone",
            "body_html": "",
            "canonical_url": "https://x.substack.com/p/empty",
            "slug": "empty",
        }
    )
    with pytest.raises(ValueError, match="no body"):
        resolve_post("https://x.substack.com/p/empty")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_substack.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.substack'`.

**Step 3: Write minimal implementation**

Create `pipeline/substack.py` (this step adds only resolution/fetch; normalization comes in Task 3):

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests


@dataclass(frozen=True)
class SubstackPost:
    title: str
    subtitle: str
    description: str
    canonical_url: str
    body_html: str
    slug: str
    host: str
    audience: str
    wordcount: int


_SHORT_LINK_RE = re.compile(r"/p-(\d+)\b")
_SLUG_RE = re.compile(r"/p/([^/?#]+)")


def _api_url(url_or_id: str) -> str:
    """Map a Substack post reference to its JSON API URL.

    Accepts a bare numeric id, a short link (.../p-<id>), or a canonical
    slug URL (.../p/<slug>).
    """
    ref = url_or_id.strip()
    if ref.isdigit():
        return f"https://substack.com/api/v1/posts/by-id/{ref}"

    parsed = urlparse(ref)
    host = parsed.netloc or "substack.com"
    path = parsed.path

    m = _SHORT_LINK_RE.search(path)
    if m:
        return f"https://{host}/api/v1/posts/by-id/{m.group(1)}"

    m = _SLUG_RE.search(path)
    if m:
        return f"https://{host}/api/v1/posts/{m.group(1)}"

    raise ValueError(f"Unrecognized Substack post reference: {url_or_id!r}")


def resolve_post(url_or_id: str, *, timeout: int = 30) -> SubstackPost:
    """Fetch a Substack post via the JSON API.

    Raises ValueError if the post has no usable body (empty or paywalled).
    """
    api_url = _api_url(url_or_id)
    resp = requests.get(api_url, timeout=timeout)
    resp.raise_for_status()
    post = resp.json()["post"]

    body_html = post.get("body_html") or ""
    audience = post.get("audience") or ""
    if not body_html:
        raise ValueError(f"Substack post has no body: {url_or_id!r}")
    if audience != "everyone" and (
        post.get("should_send_free_preview") or post.get("truncated_body_text")
    ):
        raise ValueError(
            f"Substack post is behind a paywall (audience={audience!r}); "
            f"cannot fetch full text: {url_or_id!r}"
        )

    parsed = urlparse(post.get("canonical_url") or api_url)
    return SubstackPost(
        title=post.get("title") or "",
        subtitle=post.get("subtitle") or "",
        description=post.get("description") or "",
        canonical_url=post.get("canonical_url") or "",
        body_html=body_html,
        slug=post.get("slug") or "",
        host=parsed.netloc,
        audience=audience,
        wordcount=int(post.get("wordcount") or 0),
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_substack.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add pipeline/substack.py pipeline/test_substack.py
git commit -m "feat: add Substack post resolution and fetch"
```

---

## Task 3: HTML normalization (`html_to_clean_text`)

**Files:**
- Modify: `pipeline/substack.py`
- Test: `pipeline/test_substack.py`

**Step 1: Write the failing test**

Add to `pipeline/test_substack.py`:

```python
from pipeline.substack import html_to_clean_text


_SAMPLE_HTML = (
    '<p>I had no idea how wild human history was, says the <a href="x">host</a>.</p>'
    "<h3>Sponsors</h3>"
    "<p>This episode is brought to you by Sponsor X.</p>"
    "<h3>Timestamps</h3>"
    '<p><a href="#a">(00:00:00) – Topic one</a></p>'
    '<p><a href="#b">(00:15:45) – Topic two</a></p>'
    "<h3>Transcript</h3>"
    "<h3>00:00:00 – Topic one</h3>"
    "<p><strong>Dwarkesh Patel</strong> <em>00:00:00</em></p>"
    "<p>Welcome to the show.</p>"
    "<p><strong>David Reich</strong></p>"
    "<p>Glad to be here at 3:00 PM.</p>"
)


def test_html_to_clean_text_strips_boilerplate_and_timestamps():
    out = html_to_clean_text(_SAMPLE_HTML)

    # Intro prose kept (link text kept, URL dropped)
    assert "I had no idea how wild human history was" in out
    assert "host" in out
    assert "href" not in out and "http" not in out

    # Sponsors and Timestamps sections dropped
    assert "Sponsor X" not in out
    assert "Sponsors" not in out
    assert "Timestamps" not in out
    assert "(00:00:00)" not in out

    # Section header kept but timestamp prefix stripped
    assert "Topic one" in out

    # Speaker labels and content kept
    assert "Dwarkesh Patel" in out
    assert "Welcome to the show." in out
    assert "David Reich" in out
    assert "Glad to be here" in out

    # The structural word "Transcript" is dropped
    assert "Transcript" not in out

    # No leftover HH:MM:SS timestamp tokens (inline em + header prefix removed).
    import re as _re
    assert not _re.search(r"\b\d{1,2}:\d{2}:\d{2}\b", out)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_substack.py::test_html_to_clean_text_strips_boilerplate_and_timestamps -v`
Expected: FAIL — `cannot import name 'html_to_clean_text'`.

**Step 3: Write minimal implementation**

Add to `pipeline/substack.py` (add `from bs4 import BeautifulSoup` and `from bs4.element import Tag` to imports):

```python
_TS_PREFIX_RE = re.compile(r"^\(?\d{1,2}:\d{2}(?::\d{2})?\)?\s*[–-]\s*")
_TS_TOKEN_RE = re.compile(r"^\(?\d{1,2}:\d{2}(?::\d{2})?\)?$")
_SKIP_HEADERS = {"sponsor", "sponsors", "timestamps"}
_STRUCTURAL_HEADERS = _SKIP_HEADERS | {"transcript"}


def html_to_clean_text(body_html: str) -> str:
    """Convert Substack post body HTML to clean text for the model.

    Drops the Sponsors and Timestamps (TOC) sections, strips ``HH:MM:SS``
    timestamps from section headers and inline speaker turns, and renders the
    intro prose and transcript as plain labeled paragraphs.
    """
    soup = BeautifulSoup(body_html, "html.parser")

    # Remove inline timestamp <em> tags (e.g. "<strong>Name</strong> <em>00:00:00</em>").
    for em in soup.find_all("em"):
        if _TS_TOKEN_RE.match(em.get_text(strip=True)):
            em.decompose()

    lines: list[str] = []
    skipping = False
    for el in soup.children:
        if not isinstance(el, Tag):
            continue

        if el.name and re.fullmatch(r"h[1-6]", el.name):
            text = _TS_PREFIX_RE.sub("", el.get_text(" ", strip=True)).strip()
            lower = text.lower()
            if lower in _SKIP_HEADERS:
                skipping = True
                continue
            skipping = False
            if lower not in _STRUCTURAL_HEADERS:
                lines.append(text)
            continue

        if skipping:
            continue

        text = el.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            lines.append(text)

    return "\n\n".join(lines)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_substack.py -v`
Expected: PASS.

**Step 5: Verify against the real post (manual sanity check, no commit gate)**

Run:
```bash
uv run python -c "
from pipeline.substack import resolve_post, html_to_clean_text
p = resolve_post('https://www.dwarkesh.com/p/david-reich-2')
t = html_to_clean_text(p.body_html)
print('title:', p.title)
print('chars:', len(t))
print(t[:600])
"
```
Expected: prints the title, a character count well below the raw 179 KB, and clean intro + transcript text with no `00:00:00` tokens, no "Sponsors"/"Timestamps".

**Step 6: Commit**

```bash
git add pipeline/substack.py pipeline/test_substack.py
git commit -m "feat: normalize Substack post HTML to clean transcript text"
```

---

## Task 4: Report writer (`pipeline/substack_writer.py`)

**Files:**
- Create: `pipeline/substack_writer.py`
- Test: `pipeline/test_substack_writer.py`

This mirrors `pipeline/yglesias_writer.py` exactly in structure (session lifecycle, 900 s timeout, `<summary>`/`<script>` extraction, empty-output guard), differing only in the prompt.

**Step 1: Write the failing tests**

Create `pipeline/test_substack_writer.py`:

```python
from __future__ import annotations

from unittest.mock import patch

from pipeline.substack_writer import (
    ReportOutput,
    _extract_script,
    _extract_summary,
    build_report_prompt,
    generate_report,
)


def test_build_prompt_includes_subject_and_body():
    prompt = build_report_prompt(
        body="Dwarkesh Patel\nWelcome.\nDavid Reich\nThanks.",
        subject="Why the Bronze Age was an inflection point",
    )
    assert "Why the Bronze Age was an inflection point" in prompt
    assert "Welcome." in prompt
    assert "Thanks." in prompt


def test_report_output_dataclass_fields():
    r = ReportOutput(script="text", summary="sum")
    assert r.script == "text"
    assert r.summary == "sum"


@patch("pipeline.substack_writer.delete_session")
@patch("pipeline.substack_writer.get_last_assistant_text")
@patch("pipeline.substack_writer.get_messages")
@patch("pipeline.substack_writer.wait_for_idle")
@patch("pipeline.substack_writer.send_prompt_async")
@patch("pipeline.substack_writer.create_session")
def test_generate_report_happy_path(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_sub"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<summary>Brief on the interview.</summary>\n\n"
        "<script>On this episode, Dwarkesh spoke with David Reich...</script>"
    )

    result = generate_report(body="Dwarkesh Patel\nhi", subject="X")

    assert result.script.startswith("On this episode")
    assert result.summary == "Brief on the interview."
    mock_wait.assert_called_once_with("ses_sub", timeout=900)
    mock_delete.assert_called_once_with("ses_sub")


@patch("pipeline.substack_writer.delete_session")
@patch("pipeline.substack_writer.wait_for_idle")
@patch("pipeline.substack_writer.send_prompt_async")
@patch("pipeline.substack_writer.create_session")
def test_generate_report_timeout_raises_and_deletes_session(
    mock_create, mock_send, mock_wait, mock_delete
):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False
    try:
        generate_report(body="x", subject="y")
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "900 seconds" in str(e)
    mock_delete.assert_called_once_with("ses_timeout")


@patch("pipeline.substack_writer.delete_session")
@patch("pipeline.substack_writer.get_last_assistant_text")
@patch("pipeline.substack_writer.get_messages")
@patch("pipeline.substack_writer.wait_for_idle")
@patch("pipeline.substack_writer.send_prompt_async")
@patch("pipeline.substack_writer.create_session")
def test_generate_report_rejects_empty_script(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_empty"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Anything.</summary>\n\n<script>   </script>"
    try:
        generate_report(body="x", subject="y")
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "empty script" in str(e)
    mock_delete.assert_called_once_with("ses_empty")


def test_extract_script_with_tags():
    assert _extract_script("Reasoning.\n<script>The briefing.</script>") == (
        "The briefing."
    )


def test_extract_script_no_tags_returns_full_text():
    assert _extract_script("No tags here.") == "No tags here."


def test_extract_summary_with_tags():
    assert _extract_summary("<summary>Brief.</summary>\nRest.") == "Brief."


def test_extract_summary_no_tags_returns_empty():
    assert _extract_summary("No summary.") == ""
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_substack_writer.py -v`
Expected: FAIL — `No module named 'pipeline.substack_writer'`.

**Step 3: Write minimal implementation**

Create `pipeline/substack_writer.py` (copy `pipeline/yglesias_writer.py` and replace only `PROMPT_TEMPLATE` and the error-message strings):

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


PROMPT_TEMPLATE = """\
You are writing a spoken briefing about a long-form interview podcast.
Your listener does NOT want to hear the transcript read aloud — they want
a clear, structured report on what was discussed.

The post may open with a short written introduction before the transcript
begins; use it for context but focus your report on the conversation itself.

Title: {subject}

Below is the transcript. Read it, then produce a spoken briefing
(roughly 1200–2000 words) covering:

- Who participated (host and guest, with affiliations if stated).
- The main themes and questions explored, in the order that best
  illuminates the conversation (not necessarily the order they appeared).
- The key claims, arguments, and evidence each participant offered,
  with attribution ("Reich argued...", "Dwarkesh pushed back, asking...").
- Notable disagreements, uncertainties, or surprising points.
- Concrete details, numbers, names, and examples that gave the
  conversation weight.

Write for the ear: plain spoken English, no markdown, no bullet points,
no headers. Use natural transitions. You are a smart friend explaining
what an interview got into, not reading a summary out loud. Do not
editorialize beyond what the participants themselves said, and do not
invent facts.

TRANSCRIPT:

{body}
"""


@dataclass(frozen=True)
class ReportOutput:
    script: str
    summary: str


def build_report_prompt(*, body: str, subject: str) -> str:
    return PROMPT_TEMPLATE.format(subject=subject, body=body)


def _extract_script(text: str) -> str:
    m = re.search(r"<script>\s*(.*?)\s*</script>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _extract_summary(text: str) -> str:
    m = re.search(r"<summary>\s*(.*?)\s*</summary>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def generate_report(*, body: str, subject: str) -> ReportOutput:
    """Generate a spoken-briefing report on a Substack interview transcript."""
    prompt = build_report_prompt(body=body, subject=subject)
    instruction = (
        "Read the following transcript and produce the spoken briefing. "
        "First write a 2-3 sentence summary wrapped in <summary>...</summary> "
        "tags. Then write the full spoken script wrapped in "
        "<script>...</script> tags. Output nothing outside these tags.\n\n"
        + prompt
    )

    session_id = create_session()
    try:
        send_prompt_async(session_id, instruction)
        if not wait_for_idle(session_id, timeout=900):
            raise RuntimeError(
                "substack report writer did not complete within 900 seconds"
            )
        messages = get_messages(session_id)
        full_text = get_last_assistant_text(messages).strip()
        script = _extract_script(full_text)
        summary = _extract_summary(full_text)
        if not script.strip():
            raise RuntimeError("substack report writer returned empty script")
        return ReportOutput(script=script, summary=summary)
    finally:
        delete_session(session_id)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_substack_writer.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add pipeline/substack_writer.py pipeline/test_substack_writer.py
git commit -m "feat: add Substack interview report writer"
```

---

## Task 5: CLI `substack` command (report + read modes)

**Files:**
- Modify: `pipeline/__main__.py` (add the command + register on `cli`)
- Test: `pipeline/test_substack_cli.py`

**Step 1: Write the failing tests**

Create `pipeline/test_substack_cli.py`:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from pipeline.__main__ import cli
from pipeline.substack import SubstackPost
from pipeline.substack_writer import ReportOutput


def _post() -> SubstackPost:
    return SubstackPost(
        title="David Reich – Bronze Age",
        subtitle="A subtitle",
        description="desc",
        canonical_url="https://www.dwarkesh.com/p/david-reich-2",
        body_html="<p><strong>Dwarkesh Patel</strong></p><p>Hi.</p>",
        slug="david-reich-2",
        host="www.dwarkesh.com",
        audience="everyone",
        wordcount=21163,
    )


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.substack_writer.generate_report")
@patch("pipeline.substack.resolve_post")
def test_report_mode_prefixes_title_and_passes_source_url(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "pipeline.__main__._default_state_db_path", lambda: tmp_path / "s.sqlite3"
    )
    monkeypatch.setattr("pipeline.__main__.R2Client", lambda: object())
    mock_resolve.return_value = _post()
    mock_report.return_value = ReportOutput(script="The briefing.", summary="Brief.")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "substack",
            "--url", "https://www.dwarkesh.com/p/david-reich-2",
            "--mode", "report",
            "--feed-slug", "dwarkesh",
        ],
    )

    assert result.exit_code == 0, result.output
    mock_report.assert_called_once()
    assert mock_publish.call_count == 1
    kwargs = mock_publish.call_args.kwargs
    assert kwargs["title"] == "Report: David Reich – Bronze Age"
    assert kwargs["feed_slug"] == "dwarkesh"
    assert kwargs["source_url"] == "https://www.dwarkesh.com/p/david-reich-2"


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.blog_poller.adapt_for_audio")
@patch("pipeline.substack.resolve_post")
def test_read_mode_uses_adapter_and_plain_title(
    mock_resolve, mock_adapt, mock_publish, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "pipeline.__main__._default_state_db_path", lambda: tmp_path / "s.sqlite3"
    )
    monkeypatch.setattr("pipeline.__main__.R2Client", lambda: object())
    mock_resolve.return_value = _post()
    mock_adapt.return_value = "Spoken adaptation of the essay."

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "substack",
            "--url", "https://www.dwarkesh.com/p/david-reich-2",
            "--mode", "read",
            "--feed-slug", "dwarkesh",
        ],
    )

    assert result.exit_code == 0, result.output
    mock_adapt.assert_called_once()
    kwargs = mock_publish.call_args.kwargs
    assert kwargs["title"] == "David Reich – Bronze Age"


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.substack_writer.generate_report")
@patch("pipeline.substack.resolve_post")
def test_dry_run_does_not_publish_and_writes_script(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    mock_resolve.return_value = _post()
    mock_report.return_value = ReportOutput(script="The briefing.", summary="Brief.")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "substack",
            "--url", "https://www.dwarkesh.com/p/david-reich-2",
            "--feed-slug", "dwarkesh",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    mock_publish.assert_not_called()
    # The dry-run script path is echoed and the file exists with the script.
    printed = result.output.strip().splitlines()[-1]
    path = Path(printed.split(": ", 1)[1]) if ": " in printed else None
    assert path is not None and path.exists()
    assert "The briefing." in path.read_text(encoding="utf-8")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_substack_cli.py -v`
Expected: FAIL — no `substack` command on the group (`exit_code != 0`).

**Step 3: Write minimal implementation**

In `pipeline/__main__.py`, add this command (place it after `publish_script_command`, before `sync-sources`). Note `publish_script` is already imported at module top (`from pipeline.script_processor import publish_script`); reference it as `script_processor.publish_script` so tests can patch it — to keep the existing import working AND be patchable, import the module:

At the top of `pipeline/__main__.py`, add:
```python
from pipeline import script_processor
```

Then the command:

```python
@cli.command("substack")
@click.option("--url", required=True, type=str, help="Substack post URL or id.")
@click.option(
    "--mode",
    type=click.Choice(["report", "read"]),
    default="report",
    show_default=True,
    help="report: spoken briefing; read: faithful full reading.",
)
@click.option("--feed-slug", "feed_slug", required=True, type=str)
@click.option(
    "--title",
    default=None,
    type=str,
    help="Override episode title (default: post title; report mode prefixes 'Report: ').",
)
@click.option("--voice", default="nova", show_default=True, type=str)
@click.option("--category", default="Technology", show_default=True, type=str)
@click.option(
    "--date", "date_str", default=None, type=str, help="Date (YYYY-MM-DD)."
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Generate the script only; skip TTS and publish.",
)
def substack_command(
    url: str,
    mode: str,
    feed_slug: str,
    title: str | None,
    voice: str,
    category: str,
    date_str: str | None,
    dry_run: bool,
) -> None:
    """Turn a Substack post into a one-off podcast episode."""
    import tempfile
    from datetime import UTC, datetime

    from pipeline import substack as substack_mod
    from pipeline import substack_writer

    if date_str is None:
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    click.echo(f"Fetching {url} ...")
    post = substack_mod.resolve_post(url)
    click.echo(f"Title: {post.title} ({post.wordcount} words)")

    if mode == "report":
        clean = substack_mod.html_to_clean_text(post.body_html)
        click.echo(f"Normalized transcript: {len(clean)} chars. Generating report...")
        out = substack_writer.generate_report(body=clean, subject=post.title)
        script_text = out.script
        episode_title = title or f"Report: {post.title}"
    else:  # read
        from pipeline.blog_poller import adapt_for_audio

        click.echo("Adapting post for audio...")
        adapted = adapt_for_audio(post.body_html, post.title)
        if not adapted:
            raise click.ClickException(
                "Audio adaptation failed (is GEMINI_API_KEY set?)."
            )
        script_text = adapted
        episode_title = title or post.title

    if dry_run:
        out_path = Path(tempfile.gettempdir()) / f"substack-{post.slug or 'post'}.txt"
        out_path.write_text(script_text, encoding="utf-8")
        click.echo("Dry run complete. No episode published.")
        click.echo(f"Script: {out_path}")
        return

    notes_md = (
        f"## Episode Summary\n\n{post.subtitle or post.description}\n\n"
        f"---\n\n[Original post]({post.canonical_url})\n"
    )

    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()
        with tempfile.TemporaryDirectory(prefix="substack-") as tmp_dir:
            tmp = Path(tmp_dir)
            script_file = tmp / "script.md"
            script_file.write_text(script_text, encoding="utf-8")
            notes_file = tmp / "notes.md"
            notes_file.write_text(notes_md, encoding="utf-8")

            result = script_processor.publish_script(
                script_file=script_file,
                title=episode_title,
                feed_slug=feed_slug,
                store=store,
                r2_client=r2_client,
                show_notes_file=notes_file,
                voice=voice,
                category=category,
                date_str=date_str,
                source_url=post.canonical_url or None,
            )
        click.echo(f"Published: {result.r2_key}")
        click.echo(f"Title: {result.title}")
        click.echo(f"Feed: {result.feed_slug}")
        if result.duration_seconds is not None:
            click.echo(f"Duration: {result.duration_seconds} sec")
    finally:
        store.close()
```

Note for the test: `test_dry_run_*` does not patch `_default_state_db_path`/`R2Client` because dry-run returns before constructing them — verify the command returns before `StateStore(...)` in dry-run mode (it does).

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_substack_cli.py -v`
Expected: PASS.

**Step 5: Run the full suite + lint**

Run: `uv run pytest -q && uv run ruff check pipeline/substack.py pipeline/substack_writer.py pipeline/__main__.py`
Expected: PASS / no lint errors.

**Step 6: Commit**

```bash
git add pipeline/__main__.py pipeline/test_substack_cli.py
git commit -m "feat: add 'substack' CLI command (report + read modes)"
```

---

## Task 6: Docs + design-doc commit + full verification

**Files:**
- Modify: `AGENTS.md` (root)
- Commit: the two design/plan docs from this session

**Step 1: Update `AGENTS.md`**

Add a short section documenting the new capability (place near "Blog Polling"):

```markdown
## One-Off Substack Episodes

Turn any Substack post into a one-off episode via the `substack` command.
Ingests through the Substack JSON API, normalizes the HTML (stripping
timestamps, sponsors, and the Timestamps TOC), then either generates a
spoken **report** (briefing, default — for interview/transcript posts) or a
faithful **read** (full reading via Gemini adaptation — for essays).

**CLI:**
`uv run python -m pipeline substack --url <post-url-or-id> --mode {report|read} --feed-slug <slug> [--title ...] [--voice nova] [--category Technology] [--date YYYY-MM-DD] [--dry-run]`

**Key modules:**
- `pipeline/substack.py` — `resolve_post` (Substack API), `html_to_clean_text` (HTML normalization)
- `pipeline/substack_writer.py` — interview report writer (opencode-serve), mirrors `chinatalk_writer.py` / `yglesias_writer.py`
- Publishes via `pipeline/script_processor.py:publish_script` (extended with `source_url`)
```

Also add the two module bullets to the "Core Paths" list if appropriate.

**Step 2: Full verification**

Run: `uv run pytest -q`
Expected: entire suite PASS.

Run: `uv run ruff check pipeline/`
Expected: no errors (run `uv run ruff format pipeline/substack.py pipeline/substack_writer.py` if format differs).

**Step 3: Commit**

```bash
git add AGENTS.md docs/plans/2026-06-15-substack-podcast-report-design.md docs/plans/2026-06-15-substack-podcast-report-plan.md
git commit -m "docs: document one-off Substack episode command"
```

---

## Task 7: Dry-run the Dwarkesh episode, review, then publish

**Step 1: Dry-run and review**

Run:
```bash
uv run python -m pipeline substack \
  --url https://substack.com/@dwarkesh/p-196892360 \
  --mode report --feed-slug dwarkesh --dry-run
```
Read the generated script file printed at the end. Confirm: correct guest/host
attribution, no transcript artifacts, no invented facts, sensible length.

**Step 2: Publish (after operator approval)**

Run (drop `--dry-run`):
```bash
uv run python -m pipeline substack \
  --url https://substack.com/@dwarkesh/p-196892360 \
  --mode report --feed-slug dwarkesh
```
Expected: prints `Published: episodes/dwarkesh/<date>-report-david-reich-...mp3`,
title `Report: David Reich – Why the Bronze Age...`, feed `dwarkesh`.

**Step 3: Verify the feed**

Subscription URL: `https://podcast.mohrbacher.dev/feeds/dwarkesh.xml`
(Optionally add `assets/podcast/cover-dwarkesh.jpg` later; missing art is cosmetic.)

---

## Notes / gotchas

- `publish_script` is imported by name in `__main__.py`; the new command calls
  it via `script_processor.publish_script` so tests can patch it. Keep both the
  `from pipeline import script_processor` import and the existing
  `from pipeline.script_processor import publish_script` (used by
  `publish-script`).
- `_slugify` in `script_processor` builds the episode slug from the title, so
  `Report: David Reich – ...` becomes `report-david-reich-...` — fine.
- The report sends the full normalized transcript (~tens of thousands of words)
  to opencode-serve in one prompt, matching the chinatalk/yglesias precedent.
- Read mode requires `GEMINI_API_KEY`; the command fails clearly if adaptation
  returns nothing.
```
