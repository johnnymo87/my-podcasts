# ChinaTalk Transcript Report Path Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When a ChinaTalk newsletter is a podcast transcript, replace the TTS body with an AI-written structured briefing about the conversation, prefix the title with "Report: ", and publish to the existing chinatalk feed. Fall back silently to standard reading on any failure.

**Architecture:** Add two new modules — `chinatalk_classifier.py` (Gemini Flash-Lite YES/NO classifier) and `chinatalk_writer.py` (opencode-serve briefing generator, modeled on `rundown_writer.py`). Add a small pure helper `maybe_rewrite_chinatalk` that wires them together. Call that helper from `processor.process_email_bytes` between body cleaning and TTS.

**Tech Stack:** Python 3, `google.genai` (Gemini Flash-Lite), `pipeline.opencode_client` (existing), `pytest`, `unittest.mock`.

**Design doc:** `docs/plans/2026-04-25-chinatalk-transcript-report-design.md`

---

## Conventions used in this plan

- **TDD:** write failing test → run it → implement → run it → commit.
- **Tests:** all under `pipeline/`, named `test_<module>.py`, run with `uv run pytest pipeline/test_<module>.py -v`.
- **Mocking:** patch external boundaries (`google.genai.Client`, `pipeline.opencode_client.*`); never reach the network in tests.
- **No new env vars.** Reuse `GEMINI_API_KEY` and the existing opencode env.
- **Commits:** one per task. Messages use `feat:`, `test:`, `refactor:`. Keep them short.

---

## Task 1: Skeleton classifier module with public API and dataclass-free signature

**Files:**
- Create: `pipeline/chinatalk_classifier.py`
- Create: `pipeline/test_chinatalk_classifier.py`

**Step 1: Write the failing test**

Create `pipeline/test_chinatalk_classifier.py`:

```python
from __future__ import annotations

from pipeline.chinatalk_classifier import is_transcript


def test_is_transcript_returns_false_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert is_transcript("any body", "any subject") is False
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_chinatalk_classifier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.chinatalk_classifier'`.

**Step 3: Write minimal implementation**

Create `pipeline/chinatalk_classifier.py`:

```python
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def is_transcript(body: str, subject: str) -> bool:
    """Return True iff the chinatalk newsletter body is a podcast transcript.

    Conservative: any failure (missing API key, network error, ambiguous
    response) returns False, so callers fall back to the standard reading.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set; skipping chinatalk classification")
        return False
    return False
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest pipeline/test_chinatalk_classifier.py -v`
Expected: PASS (1 test).

**Step 5: Commit**

```bash
git add pipeline/chinatalk_classifier.py pipeline/test_chinatalk_classifier.py
git commit -m "feat: scaffold chinatalk transcript classifier"
```

---

## Task 2: Classifier YES path

**Files:**
- Modify: `pipeline/chinatalk_classifier.py`
- Modify: `pipeline/test_chinatalk_classifier.py`

**Step 1: Write the failing test**

Append to `pipeline/test_chinatalk_classifier.py`:

```python
from unittest.mock import MagicMock, patch


def test_is_transcript_returns_true_on_yes(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_response = MagicMock()
    mock_response.text = "YES"
    with patch("pipeline.chinatalk_classifier.genai") as mock_genai:
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = mock_response

        assert is_transcript("Speaker A: Hello\nSpeaker B: Hi", "ChinaTalk: Episode 42") is True

        mock_genai.Client.assert_called_once_with(api_key="fake-key")
        call_kwargs = mock_client.models.generate_content.call_args[1]
        assert "gemini-3.1-flash-lite" in call_kwargs["model"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_chinatalk_classifier.py::test_is_transcript_returns_true_on_yes -v`
Expected: FAIL — current implementation returns `False` unconditionally; also `genai` is not imported in the module so the patch target won't resolve.

**Step 3: Write minimal implementation**

Replace `pipeline/chinatalk_classifier.py` with:

```python
from __future__ import annotations

import logging
import os

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are classifying a newsletter post. Decide whether it consists primarily
of a podcast transcript with multiple named speakers and verbatim dialogue
(YES) or whether it is an essay, article, or analysis (NO).

A transcript looks like alternating turns labeled with speaker names or
roles ("Jordan:", "Guest:", "Q:", "A:", etc.) and reads like a recorded
conversation. An essay reads like one author's prose, even if it contains
quoted excerpts.

Reply with exactly one token: YES or NO.
"""


def is_transcript(body: str, subject: str) -> bool:
    """Return True iff the chinatalk newsletter body is a podcast transcript.

    Conservative: any failure (missing API key, network error, ambiguous
    response) returns False, so callers fall back to the standard reading.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set; skipping chinatalk classification")
        return False

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite-preview",
        contents=f"Subject: {subject}\n\n{body}",
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            temperature=0.0,
        ),
    )
    text = (response.text or "").strip().upper()
    return text == "YES"
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest pipeline/test_chinatalk_classifier.py -v`
Expected: PASS (2 tests).

**Step 5: Commit**

```bash
git add pipeline/chinatalk_classifier.py pipeline/test_chinatalk_classifier.py
git commit -m "feat: implement chinatalk transcript classifier YES path"
```

---

## Task 3: Classifier NO and unparseable responses both return False

**Files:**
- Modify: `pipeline/test_chinatalk_classifier.py`

**Step 1: Write the failing tests**

Append to `pipeline/test_chinatalk_classifier.py`:

```python
def test_is_transcript_returns_false_on_no(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_response = MagicMock()
    mock_response.text = "NO"
    with patch("pipeline.chinatalk_classifier.genai") as mock_genai:
        mock_genai.Client.return_value.models.generate_content.return_value = mock_response
        assert is_transcript("essay body", "ChinaTalk: Some Essay") is False


def test_is_transcript_returns_false_on_unparseable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_response = MagicMock()
    mock_response.text = "Maybe? It's hard to say."
    with patch("pipeline.chinatalk_classifier.genai") as mock_genai:
        mock_genai.Client.return_value.models.generate_content.return_value = mock_response
        assert is_transcript("body", "subject") is False


def test_is_transcript_returns_false_on_empty_response(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_response = MagicMock()
    mock_response.text = None
    with patch("pipeline.chinatalk_classifier.genai") as mock_genai:
        mock_genai.Client.return_value.models.generate_content.return_value = mock_response
        assert is_transcript("body", "subject") is False
```

**Step 2: Run tests to verify they pass already (or fail meaningfully)**

Run: `uv run pytest pipeline/test_chinatalk_classifier.py -v`
Expected: PASS for all three. The implementation from Task 2 already handles these.

If any FAIL, fix the implementation in `pipeline/chinatalk_classifier.py` and re-run.

**Step 3: Commit**

```bash
git add pipeline/test_chinatalk_classifier.py
git commit -m "test: cover chinatalk classifier NO and unparseable responses"
```

---

## Task 4: Classifier swallows exceptions

**Files:**
- Modify: `pipeline/chinatalk_classifier.py`
- Modify: `pipeline/test_chinatalk_classifier.py`

**Step 1: Write the failing test**

Append to `pipeline/test_chinatalk_classifier.py`:

```python
def test_is_transcript_swallows_exceptions(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    with patch("pipeline.chinatalk_classifier.genai") as mock_genai:
        mock_genai.Client.side_effect = RuntimeError("boom")
        # Must not raise; must return False.
        assert is_transcript("body", "subject") is False
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_chinatalk_classifier.py::test_is_transcript_swallows_exceptions -v`
Expected: FAIL with `RuntimeError: boom`.

**Step 3: Update implementation**

In `pipeline/chinatalk_classifier.py`, wrap the API call in `try/except`:

```python
def is_transcript(body: str, subject: str) -> bool:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set; skipping chinatalk classification")
        return False

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=f"Subject: {subject}\n\n{body}",
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                temperature=0.0,
            ),
        )
        text = (response.text or "").strip().upper()
        return text == "YES"
    except Exception:
        logger.exception("chinatalk classifier failed; defaulting to NO")
        return False
```

**Step 4: Run all classifier tests**

Run: `uv run pytest pipeline/test_chinatalk_classifier.py -v`
Expected: PASS (5 tests).

**Step 5: Commit**

```bash
git add pipeline/chinatalk_classifier.py pipeline/test_chinatalk_classifier.py
git commit -m "feat: chinatalk classifier returns False on exceptions"
```

---

## Task 5: Skeleton report writer module

**Files:**
- Create: `pipeline/chinatalk_writer.py`
- Create: `pipeline/test_chinatalk_writer.py`

**Step 1: Write the failing test**

Create `pipeline/test_chinatalk_writer.py`:

```python
from __future__ import annotations

from unittest.mock import patch

from pipeline.chinatalk_writer import ReportOutput, build_report_prompt


def test_build_prompt_includes_subject_and_body():
    prompt = build_report_prompt(
        body="Speaker A: Hello\nSpeaker B: Hi",
        subject="ChinaTalk: Talking with Foo",
    )
    assert "ChinaTalk: Talking with Foo" in prompt
    assert "Speaker A: Hello" in prompt
    assert "Speaker B: Hi" in prompt


def test_report_output_dataclass_fields():
    r = ReportOutput(script="text", summary="sum")
    assert r.script == "text"
    assert r.summary == "sum"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_chinatalk_writer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.chinatalk_writer'`.

**Step 3: Write minimal implementation**

Create `pipeline/chinatalk_writer.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


PROMPT_TEMPLATE = """\
You are writing a spoken briefing about a podcast conversation that ran
in the ChinaTalk newsletter today. Your listener does NOT want to hear
the transcript read aloud — they want a clear, structured report on
what was said.

Subject line: {subject}

Below is the full transcript. Read it, then produce a 5–10 minute
spoken briefing (roughly 800–1500 words) covering:

- Who participated (host and guests, with affiliations if stated).
- What topics were discussed, in the order that best illuminates the
  conversation (not necessarily the order they appeared).
- The key claims, arguments, and evidence each participant offered,
  with attribution ("Schneider argued...", "the guest pushed back,
  saying...").
- Any notable disagreements or points of tension.
- Concrete details, numbers, and examples that gave the conversation
  weight.

Write for the ear: plain spoken English, no markdown, no bullet
points, no headers. Use natural transitions. You are a smart friend
explaining what a podcast got into, not reading a summary out loud.
Do not editorialize beyond what the participants themselves said,
and do not invent facts.

TRANSCRIPT:

{body}
"""


@dataclass(frozen=True)
class ReportOutput:
    script: str
    summary: str


def build_report_prompt(*, body: str, subject: str) -> str:
    return PROMPT_TEMPLATE.format(subject=subject, body=body)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_chinatalk_writer.py -v`
Expected: PASS (2 tests).

**Step 5: Commit**

```bash
git add pipeline/chinatalk_writer.py pipeline/test_chinatalk_writer.py
git commit -m "feat: scaffold chinatalk report writer prompt"
```

---

## Task 6: Report writer happy path via opencode

**Files:**
- Modify: `pipeline/chinatalk_writer.py`
- Modify: `pipeline/test_chinatalk_writer.py`

**Step 1: Write the failing test**

Append to `pipeline/test_chinatalk_writer.py`:

```python
from unittest.mock import patch

from pipeline.chinatalk_writer import generate_report


@patch("pipeline.chinatalk_writer.delete_session")
@patch("pipeline.chinatalk_writer.get_last_assistant_text")
@patch("pipeline.chinatalk_writer.get_messages")
@patch("pipeline.chinatalk_writer.wait_for_idle")
@patch("pipeline.chinatalk_writer.send_prompt_async")
@patch("pipeline.chinatalk_writer.create_session")
def test_generate_report_happy_path(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_chinatalk"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<summary>Brief on the conversation.</summary>\n\n"
        "<script>Hey, in today's ChinaTalk Jordan sat down with...</script>"
    )

    result = generate_report(body="Speaker: hi", subject="ChinaTalk: X")

    assert result.script.startswith("Hey, in today's ChinaTalk")
    assert result.summary == "Brief on the conversation."
    mock_create.assert_called_once()
    mock_send.assert_called_once()
    mock_wait.assert_called_once_with("ses_chinatalk", timeout=300)
    mock_delete.assert_called_once_with("ses_chinatalk")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_chinatalk_writer.py::test_generate_report_happy_path -v`
Expected: FAIL with `ImportError: cannot import name 'generate_report' from 'pipeline.chinatalk_writer'`.

**Step 3: Update implementation**

Replace `pipeline/chinatalk_writer.py` with:

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
[same template body as Task 5 — keep unchanged]
"""


@dataclass(frozen=True)
class ReportOutput:
    script: str
    summary: str


def build_report_prompt(*, body: str, subject: str) -> str:
    return PROMPT_TEMPLATE.format(subject=subject, body=body)


def _extract_tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def generate_report(*, body: str, subject: str) -> ReportOutput:
    """Generate a spoken-briefing report on a chinatalk transcript."""
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
        if not wait_for_idle(session_id, timeout=300):
            raise RuntimeError(
                "chinatalk report writer did not complete within 300 seconds"
            )
        messages = get_messages(session_id)
        full_text = get_last_assistant_text(messages).strip()
        script = _extract_tag(full_text, "script") or full_text
        summary = _extract_tag(full_text, "summary")
        if not script.strip():
            raise RuntimeError("chinatalk report writer returned empty script")
        return ReportOutput(script=script, summary=summary)
    finally:
        delete_session(session_id)
```

**Important:** keep the actual `PROMPT_TEMPLATE` body from Task 5 verbatim — do not let "[same template body…]" land in the file.

**Step 4: Run all writer tests**

Run: `uv run pytest pipeline/test_chinatalk_writer.py -v`
Expected: PASS (3 tests).

**Step 5: Commit**

```bash
git add pipeline/chinatalk_writer.py pipeline/test_chinatalk_writer.py
git commit -m "feat: implement chinatalk report writer happy path"
```

---

## Task 7: Report writer rejects timeout and empty output

**Files:**
- Modify: `pipeline/test_chinatalk_writer.py`

**Step 1: Write the failing tests**

Append to `pipeline/test_chinatalk_writer.py`:

```python
@patch("pipeline.chinatalk_writer.delete_session")
@patch("pipeline.chinatalk_writer.wait_for_idle")
@patch("pipeline.chinatalk_writer.send_prompt_async")
@patch("pipeline.chinatalk_writer.create_session")
def test_generate_report_timeout_raises_and_deletes_session(
    mock_create, mock_send, mock_wait, mock_delete
):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False

    try:
        generate_report(body="x", subject="y")
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "300 seconds" in str(e)

    mock_delete.assert_called_once_with("ses_timeout")


@patch("pipeline.chinatalk_writer.delete_session")
@patch("pipeline.chinatalk_writer.get_last_assistant_text")
@patch("pipeline.chinatalk_writer.get_messages")
@patch("pipeline.chinatalk_writer.wait_for_idle")
@patch("pipeline.chinatalk_writer.send_prompt_async")
@patch("pipeline.chinatalk_writer.create_session")
def test_generate_report_rejects_empty_script(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_empty"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Anything.</summary>\n\n<script>   </script>"

    try:
        generate_report(body="x", subject="y")
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "empty script" in str(e)

    mock_delete.assert_called_once_with("ses_empty")
```

**Step 2: Run tests to verify they pass already**

Run: `uv run pytest pipeline/test_chinatalk_writer.py -v`
Expected: PASS (5 tests). Implementation from Task 6 already enforces both behaviors.

**Step 3: Commit**

```bash
git add pipeline/test_chinatalk_writer.py
git commit -m "test: cover chinatalk writer timeout and empty-script rejection"
```

---

## Task 8: Pure orchestration helper `maybe_rewrite_chinatalk`

This is the integration point. We isolate it as a pure function so the
processor change in Task 9 is a one-line call site and the logic is
fully unit-testable without touching TTS / R2 / DB.

**Files:**
- Create: `pipeline/chinatalk_report.py`
- Create: `pipeline/test_chinatalk_report.py`

**Step 1: Write the failing tests**

Create `pipeline/test_chinatalk_report.py`:

```python
from __future__ import annotations

from unittest.mock import patch

from pipeline.chinatalk_report import maybe_rewrite_chinatalk
from pipeline.chinatalk_writer import ReportOutput


def test_non_chinatalk_feed_is_passthrough():
    body, title = maybe_rewrite_chinatalk(
        body="essay body",
        title="2026-04-25 - Slow Boring - Foo",
        feed_slug="yglesias",
        subject_raw="Slow Boring: Foo",
    )
    assert body == "essay body"
    assert title == "2026-04-25 - Slow Boring - Foo"


@patch("pipeline.chinatalk_report.is_transcript", return_value=False)
def test_chinatalk_essay_is_passthrough(mock_classify):
    body, title = maybe_rewrite_chinatalk(
        body="An essay on policy.",
        title="2026-04-25 - ChinaTalk - Some Essay",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Some Essay",
    )
    assert body == "An essay on policy."
    assert title == "2026-04-25 - ChinaTalk - Some Essay"
    mock_classify.assert_called_once()


@patch("pipeline.chinatalk_report.generate_report")
@patch("pipeline.chinatalk_report.is_transcript", return_value=True)
def test_chinatalk_transcript_is_rewritten(mock_classify, mock_writer):
    mock_writer.return_value = ReportOutput(
        script="Today on ChinaTalk, Jordan and a guest dug into...",
        summary="Brief.",
    )
    body, title = maybe_rewrite_chinatalk(
        body="Speaker A: Hi\nSpeaker B: Hello",
        title="2026-04-25 - ChinaTalk - Episode 42",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Episode 42",
    )
    assert body.startswith("Today on ChinaTalk")
    assert title == "Report: 2026-04-25 - ChinaTalk - Episode 42"


@patch("pipeline.chinatalk_report.generate_report", side_effect=RuntimeError("boom"))
@patch("pipeline.chinatalk_report.is_transcript", return_value=True)
def test_writer_failure_falls_back_to_reading(mock_classify, mock_writer):
    body, title = maybe_rewrite_chinatalk(
        body="Speaker A: Hi",
        title="2026-04-25 - ChinaTalk - Episode 42",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Episode 42",
    )
    assert body == "Speaker A: Hi"
    assert title == "2026-04-25 - ChinaTalk - Episode 42"


@patch(
    "pipeline.chinatalk_report.is_transcript",
    side_effect=RuntimeError("classifier crashed"),
)
def test_classifier_failure_falls_back_to_reading(mock_classify):
    # The classifier itself is supposed to swallow exceptions, but the
    # orchestrator must also be defensive in case a future change leaks one.
    body, title = maybe_rewrite_chinatalk(
        body="Speaker A: Hi",
        title="2026-04-25 - ChinaTalk - Episode 42",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Episode 42",
    )
    assert body == "Speaker A: Hi"
    assert title == "2026-04-25 - ChinaTalk - Episode 42"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_chinatalk_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.chinatalk_report'`.

**Step 3: Write minimal implementation**

Create `pipeline/chinatalk_report.py`:

```python
from __future__ import annotations

import logging

from pipeline.chinatalk_classifier import is_transcript
from pipeline.chinatalk_writer import generate_report

logger = logging.getLogger(__name__)


def maybe_rewrite_chinatalk(
    *,
    body: str,
    title: str,
    feed_slug: str,
    subject_raw: str,
) -> tuple[str, str]:
    """Rewrite a chinatalk transcript into a spoken briefing if applicable.

    Returns ``(body, title)``. If the feed is not chinatalk, or the body
    is not a transcript, or any step fails, returns the inputs unchanged
    so the caller falls back to a standard reading.
    """
    if feed_slug != "chinatalk":
        return body, title

    try:
        if not is_transcript(body, subject_raw):
            return body, title
        report = generate_report(body=body, subject=subject_raw)
        return report.script, f"Report: {title}"
    except Exception:
        logger.exception(
            "chinatalk report path failed; falling back to standard reading"
        )
        return body, title
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_chinatalk_report.py -v`
Expected: PASS (5 tests).

**Step 5: Commit**

```bash
git add pipeline/chinatalk_report.py pipeline/test_chinatalk_report.py
git commit -m "feat: orchestrator for chinatalk transcript rewrite"
```

---

## Task 9: Wire the orchestrator into `process_email_bytes`

**Files:**
- Modify: `pipeline/processor.py`

**Step 1: Write the failing test**

We don't have an end-to-end test for `process_email_bytes` and adding one
would require fixturing the entire TTS/R2/DB pipeline. Instead, verify
the wiring by inspecting the source. Append to
`pipeline/test_chinatalk_report.py`:

```python
def test_processor_calls_maybe_rewrite_chinatalk():
    """processor.process_email_bytes wires through maybe_rewrite_chinatalk."""
    import inspect

    from pipeline import processor

    source = inspect.getsource(processor.process_email_bytes)
    assert "maybe_rewrite_chinatalk" in source
    assert "preset.feed_slug" in source
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_chinatalk_report.py::test_processor_calls_maybe_rewrite_chinatalk -v`
Expected: FAIL — `process_email_bytes` does not yet reference `maybe_rewrite_chinatalk`.

**Step 3: Modify `pipeline/processor.py`**

a. Add the import near the top, alongside the other pipeline imports:

```python
from pipeline.chinatalk_report import maybe_rewrite_chinatalk
```

b. In `process_email_bytes`, immediately after the line:

```python
        body = adapter.clean_body(raw_email=raw_email, body=body)
```

(currently `pipeline/processor.py:117`), insert:

```python
        body, episode_title = maybe_rewrite_chinatalk(
            body=body,
            title=episode_title,
            feed_slug=preset.feed_slug,
            subject_raw=subject_raw,
        )
```

The new call uses local variables `body`, `episode_title`,
`preset.feed_slug`, and `subject_raw` that all already exist in scope at
that line. No other changes.

**Step 4: Run the wiring test**

Run: `uv run pytest pipeline/test_chinatalk_report.py -v`
Expected: PASS (6 tests).

**Step 5: Run the full pipeline test suite to confirm no regressions**

Run: `uv run pytest pipeline/ -v`
Expected: all tests PASS (existing tests untouched + the new ones).

If anything fails, do not proceed; fix the regression before committing.

**Step 6: Commit**

```bash
git add pipeline/processor.py pipeline/test_chinatalk_report.py
git commit -m "feat: wire chinatalk transcript rewrite into processor"
```

---

## Task 10: Update AGENTS.md docs

**Files:**
- Modify: `AGENTS.md`

**Step 1: Add a short section under "Core Paths"**

In `AGENTS.md`, in the "Core Paths" list, append a bullet:

```markdown
- ChinaTalk transcript report path: `pipeline/chinatalk_classifier.py`,
  `pipeline/chinatalk_writer.py`, `pipeline/chinatalk_report.py`
  (called from `pipeline/processor.py`)
```

**Step 2: Add a short subsection at the end of the doc**

Append below the existing `Blog Polling` section:

```markdown
## ChinaTalk Transcript Report Path

ChinaTalk newsletters are sometimes podcast transcripts rather than essays.
For transcripts, the pipeline replaces the TTS body with an AI-written
spoken briefing about the conversation, and prefixes the episode title
with "Report: ". Essays and articles are read normally.

**Detection:** `pipeline/chinatalk_classifier.py` (Gemini Flash-Lite YES/NO
classifier). Conservative: any failure returns NO and the listener gets
the standard reading.

**Generation:** `pipeline/chinatalk_writer.py` (opencode-serve, mirrors
`rundown_writer.py`, 300-second timeout, rejects empty output).

**Wiring:** `pipeline/chinatalk_report.maybe_rewrite_chinatalk` is called
from `pipeline/processor.process_email_bytes` between body cleaning and
TTS. Any exception in classification or report generation falls back
silently to the standard reading.
```

**Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: document chinatalk transcript report path"
```

---

## Task 11: Verification

**Step 1: Run the full test suite**

Run: `uv run pytest`
Expected: all tests PASS.

**Step 2: Lint / format check**

Run: `uv run ruff check pipeline/ 2>/dev/null || true`
If a linter is configured and complains, fix the issues and amend the
last related commit (per the AGENTS.md amend rules).

**Step 3: Manual sanity check (optional, no commit)**

If a real chinatalk email is available locally, you can dry-run the
classifier in a Python REPL:

```bash
uv run python -c "
from pipeline.chinatalk_classifier import is_transcript
print(is_transcript('Jordan: hello\nGuest: hi back', 'ChinaTalk: Test'))
"
```

This requires `GEMINI_API_KEY` in the environment. Output `True` or `False`
is fine — we are only checking the call path doesn't blow up.

**Step 4: Push**

Per AGENTS.md "Landing the Plane":

```bash
git pull --rebase
bd sync
git push
git status   # must show "up to date with origin"
```

---

## Out-of-scope reminders

These were explicitly cut from v1; **do not implement them in this plan**:

- Storing the original transcript alongside the report.
- A "force report" / "force reading" override.
- Backfilling already-published transcript episodes.
- Multi-speaker or multi-voice TTS.
- Persistent caching of classification decisions.
- A separate `chinatalk-reports` feed.
- Daily-job-style retry / errored-state semantics.
