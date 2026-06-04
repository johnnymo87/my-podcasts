# Yglesias / The Argument Transcript Report Path — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the Yglesias skip filter with a ChinaTalk-style report path that detects *The Argument* podcast transcripts and publishes an AI-written spoken briefing instead of dropping them.

**Architecture:** Mirror the ChinaTalk transcript trio. A deterministic transcript-shape detector (`yglesias_filter.is_argument_transcript`) replaces the marker-based skip. A dedicated opencode-serve writer (`yglesias_writer.generate_report`) produces the briefing. An orchestrator (`yglesias_report.maybe_rewrite_yglesias`) wires detection + writer into `processor.process_email_bytes`, between body cleaning and TTS. The dead skip plumbing (`ProcessResult.skipped`, the `__main__` skip branch) is removed.

**Tech Stack:** Python 3, pytest, `uv`, `pipeline.opencode_client` (opencode-serve HTTP client), `re` + `collections.Counter` for detection.

**Beads issue:** `my-podcasts-adx`

**Design doc:** `docs/plans/2026-06-04-yglesias-transcript-report-design.md`

**Reference implementations to mirror:**
- `pipeline/chinatalk_writer.py` + `pipeline/test_chinatalk_writer.py`
- `pipeline/chinatalk_report.py` + `pipeline/test_chinatalk_report.py`

**Run tests with:** `uv run pytest <path> -v`

---

## Task 1: Transcript-shape detector (`yglesias_filter.py`)

Repurpose `yglesias_filter.py`: delete the marker machinery and `should_skip` /
`is_demsas_podcast`; add a deterministic transcript-shape detector. The regex
and thresholds below are **verified** against the real live-event email
(`/tmp/Can-AI-Cure-Cancer.eml`: `Jerusalem Demsas` ×61, `Kelsey Piper` ×53 →
True) and against essay/one-speaker/below-threshold negatives.

**Files:**
- Rewrite: `pipeline/yglesias_filter.py`
- Rewrite: `pipeline/test_yglesias_filter.py`

**Step 1: Replace the test file with shape-detector tests**

```python
from __future__ import annotations

import inspect

from pipeline import processor
from pipeline.yglesias_filter import is_argument_transcript


# A real Argument transcript: 2+ named speakers each taking many turns.
_TRANSCRIPT_BODY = "\n".join(
    [
        "Jerusalem Demsas: Welcome to the show.",
        "Kelsey Piper: Glad to be here.",
    ]
    + [
        line
        for i in range(6)
        for line in (
            f"Jerusalem Demsas: Point number {i}.",
            f"Kelsey Piper: My response {i}.",
        )
    ]
)


_ESSAY_BODY = """
Today I want to talk about housing policy and why the same problems
keep cropping up. Note: this is just an aside. Update: more later.
Subscribe to Slow Boring for more posts like this one.
"""


def test_transcript_with_two_speakers_is_detected():
    assert is_argument_transcript(_TRANSCRIPT_BODY) is True


def test_normal_essay_is_not_a_transcript():
    assert is_argument_transcript(_ESSAY_BODY) is False


def test_single_speaker_many_turns_is_not_enough():
    body = "\n".join(f"Host: line {i}" for i in range(20))
    assert is_argument_transcript(body) is False


def test_two_speakers_below_turn_threshold_not_detected():
    # Two distinct speakers but only 4 turns each -> below the 5-turn floor.
    body = "\n".join(
        line
        for i in range(4)
        for line in (f"Alice: {i}", f"Bob: {i}")
    )
    assert is_argument_transcript(body) is False


def test_empty_body_is_not_a_transcript():
    assert is_argument_transcript("") is False


def test_timestamp_and_label_lines_do_not_trigger():
    # Digit-led timestamps and one-off labels never reach the threshold.
    body = (
        "Time stamps:\n0:00-intro\n7:07-the point\n"
        "Show Notes:\nCoverage of the topic: an article\n"
    )
    assert is_argument_transcript(body) is False


# --- Wiring check ---


def test_processor_calls_maybe_rewrite_yglesias():
    source = inspect.getsource(processor.process_email_bytes)
    assert "maybe_rewrite_yglesias" in source
    # The old skip machinery is gone.
    assert "should_skip" not in source
```

**Step 2: Run tests, verify they fail**

Run: `uv run pytest pipeline/test_yglesias_filter.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_argument_transcript'`.

**Step 3: Rewrite `pipeline/yglesias_filter.py`**

```python
"""Detect Slow Boring posts that are *The Argument* podcast transcripts.

Matt Yglesias's Slow Boring publishes the newsletter form of *The Argument*,
a debate/conversation podcast hosted by Jerusalem Demsas (regular weekly
episodes with Matt, plus one-off live events with guests). For paying
subscribers the email body carries the full verbatim transcript -- ~80
minutes of TTS. Rather than read that aloud (or drop it, as we used to), we
report on it: see ``pipeline.yglesias_report``.

Detection is deterministic and content-only: a transcript is structurally a
run of named speaker turns. We fire only when at least two distinct speaker
labels each recur at least five times as line-starts. This survives Substack
footer/boilerplate changes and is near-impossible to trigger on a normal
essay. The wiring point in ``pipeline.processor.process_email_bytes`` mirrors
the ChinaTalk transcript hook: it runs after body cleaning, before TTS.
"""

from __future__ import annotations

import re
from collections import Counter


# A transcript turn is a line that begins with a speaker label: one to four
# capitalized words (allowing internal '.', "'", "’", '-') followed by a colon
# and whitespace. Matches "Jerusalem Demsas:", "Kelsey Piper:", "Announcer:",
# "Q:". Timestamp lines like "0:00-..." start with a digit and never match.
_SPEAKER_TURN_RE = re.compile(
    r"^[ \t]*([A-Z][\w.'’\-]*(?:[ \t]+[A-Z][\w.'’\-]*){0,3}):\s",
    re.MULTILINE,
)

# A genuine conversation has at least this many distinct speakers, each taking
# at least this many turns. A normal essay never has two different "Name:"
# labels each repeated five times at line starts.
_MIN_SPEAKERS = 2
_MIN_TURNS_PER_SPEAKER = 5


def is_argument_transcript(body: str) -> bool:
    """Return True if the body looks like a multi-speaker podcast transcript.

    Pure and deterministic. Feed gating (yglesias-only) happens in
    ``pipeline.yglesias_report.maybe_rewrite_yglesias``.
    """
    if not body:
        return False
    counts = Counter(m.group(1) for m in _SPEAKER_TURN_RE.finditer(body))
    speakers = [
        label for label, n in counts.items() if n >= _MIN_TURNS_PER_SPEAKER
    ]
    return len(speakers) >= _MIN_SPEAKERS
```

**Step 4: Run tests, verify they pass (except the wiring test, which depends on Task 4)**

Run: `uv run pytest pipeline/test_yglesias_filter.py -v`
Expected: all detector tests PASS. `test_processor_calls_maybe_rewrite_yglesias`
will FAIL until Task 4 — that is expected; note it and proceed.

**Step 5: Commit**

```bash
git add pipeline/yglesias_filter.py pipeline/test_yglesias_filter.py
git commit -m "feat(yglesias): replace skip-marker filter with transcript-shape detector"
```

---

## Task 2: Report writer (`yglesias_writer.py`)

Mirror `pipeline/chinatalk_writer.py` plumbing exactly; only the prompt differs.

**Files:**
- Create: `pipeline/yglesias_writer.py`
- Create: `pipeline/test_yglesias_writer.py`

**Step 1: Write the test (mirror `test_chinatalk_writer.py`)**

```python
from __future__ import annotations

from unittest.mock import patch

from pipeline.yglesias_writer import (
    ReportOutput,
    _extract_script,
    _extract_summary,
    build_report_prompt,
    generate_report,
)


def test_build_prompt_includes_subject_and_body():
    prompt = build_report_prompt(
        body="Jerusalem Demsas: Hello\nKelsey Piper: Hi",
        subject="Can AI Cure Cancer?",
    )
    assert "Can AI Cure Cancer?" in prompt
    assert "Jerusalem Demsas: Hello" in prompt
    assert "Kelsey Piper: Hi" in prompt


def test_report_output_dataclass_fields():
    r = ReportOutput(script="text", summary="sum")
    assert r.script == "text"
    assert r.summary == "sum"


@patch("pipeline.yglesias_writer.delete_session")
@patch("pipeline.yglesias_writer.get_last_assistant_text")
@patch("pipeline.yglesias_writer.get_messages")
@patch("pipeline.yglesias_writer.wait_for_idle")
@patch("pipeline.yglesias_writer.send_prompt_async")
@patch("pipeline.yglesias_writer.create_session")
def test_generate_report_happy_path(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_yglesias"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<summary>Brief on the debate.</summary>\n\n"
        "<script>On The Argument this week, Jerusalem and Kelsey...</script>"
    )

    result = generate_report(body="Jerusalem Demsas: hi", subject="X")

    assert result.script.startswith("On The Argument this week")
    assert result.summary == "Brief on the debate."
    mock_create.assert_called_once()
    mock_send.assert_called_once()
    mock_wait.assert_called_once_with("ses_yglesias", timeout=900)
    mock_delete.assert_called_once_with("ses_yglesias")


@patch("pipeline.yglesias_writer.delete_session")
@patch("pipeline.yglesias_writer.wait_for_idle")
@patch("pipeline.yglesias_writer.send_prompt_async")
@patch("pipeline.yglesias_writer.create_session")
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


@patch("pipeline.yglesias_writer.delete_session")
@patch("pipeline.yglesias_writer.get_last_assistant_text")
@patch("pipeline.yglesias_writer.get_messages")
@patch("pipeline.yglesias_writer.wait_for_idle")
@patch("pipeline.yglesias_writer.send_prompt_async")
@patch("pipeline.yglesias_writer.create_session")
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
    raw = "Reasoning here.\n\n<script>The briefing text.</script>"
    assert _extract_script(raw) == "The briefing text."


def test_extract_script_no_tags_returns_full_text():
    raw = "The briefing without any tags."
    assert _extract_script(raw) == raw


def test_extract_summary_with_tags():
    raw = "<summary>Brief.</summary>\n\nThe rest."
    assert _extract_summary(raw) == "Brief."


def test_extract_summary_no_tags_returns_empty():
    raw = "No summary tags here."
    assert _extract_summary(raw) == ""


@patch("pipeline.yglesias_writer.delete_session")
@patch("pipeline.yglesias_writer.get_last_assistant_text")
@patch("pipeline.yglesias_writer.get_messages")
@patch("pipeline.yglesias_writer.wait_for_idle")
@patch("pipeline.yglesias_writer.send_prompt_async")
@patch("pipeline.yglesias_writer.create_session")
def test_generate_report_no_script_tags_uses_full_text(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_no_tags"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "The model just emitted prose without tags."

    result = generate_report(body="x", subject="y")

    assert result.script == "The model just emitted prose without tags."
    assert result.summary == ""
    mock_delete.assert_called_once_with("ses_no_tags")
```

**Step 2: Run tests, verify they fail**

Run: `uv run pytest pipeline/test_yglesias_writer.py -v`
Expected: FAIL — module does not exist.

**Step 3: Create `pipeline/yglesias_writer.py`**

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
You are writing a spoken briefing about an episode of The Argument, a
debate-and-conversation podcast hosted by Jerusalem Demsas. It ran in
the Slow Boring newsletter today. Your listener does NOT want to hear
the transcript read aloud — they want a clear, structured report on
what was said.

The post may open with a short editor's framing before the transcript
begins; use it for context but focus your report on the conversation
itself.

Subject line: {subject}

Below is the full transcript. Read it, then produce a 5–10 minute
spoken briefing (roughly 800–1500 words) covering:

- Who participated (host and guests, with affiliations if stated).
- What topics and questions were debated, in the order that best
  illuminates the conversation (not necessarily the order they appeared).
- The key claims, arguments, and evidence each participant offered,
  with attribution ("Demsas argued...", "Piper pushed back, saying...").
- Where they disagreed and where they found common ground.
- Concrete details, numbers, and examples that gave the conversation
  weight.

Write for the ear: plain spoken English, no markdown, no bullet
points, no headers. Use natural transitions. You are a smart friend
explaining what a debate got into, not reading a summary out loud.
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


def _extract_script(text: str) -> str:
    """Extract the spoken script from ``<script>...</script>`` tags.

    If the tag is absent (model didn't follow the format), the full
    response is returned as-is. The empty-script guard in
    ``generate_report`` will reject the result if it is whitespace only.
    """
    m = re.search(r"<script>\s*(.*?)\s*</script>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _extract_summary(text: str) -> str:
    """Extract the ``<summary>`` block, returning an empty string if absent."""
    m = re.search(r"<summary>\s*(.*?)\s*</summary>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def generate_report(*, body: str, subject: str) -> ReportOutput:
    """Generate a spoken-briefing report on an Argument transcript."""
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
                "yglesias report writer did not complete within 900 seconds"
            )
        messages = get_messages(session_id)
        full_text = get_last_assistant_text(messages).strip()
        script = _extract_script(full_text)
        summary = _extract_summary(full_text)
        if not script.strip():
            raise RuntimeError("yglesias report writer returned empty script")
        return ReportOutput(script=script, summary=summary)
    finally:
        delete_session(session_id)
```

**Step 4: Run tests, verify they pass**

Run: `uv run pytest pipeline/test_yglesias_writer.py -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add pipeline/yglesias_writer.py pipeline/test_yglesias_writer.py
git commit -m "feat(yglesias): add opencode-serve transcript report writer"
```

---

## Task 3: Orchestrator (`yglesias_report.py`)

Mirror `pipeline/chinatalk_report.py`.

**Files:**
- Create: `pipeline/yglesias_report.py`
- Create: `pipeline/test_yglesias_report.py`

**Step 1: Write the test (mirror `test_chinatalk_report.py`)**

```python
from __future__ import annotations

import inspect
from unittest.mock import patch

from pipeline import processor
from pipeline.yglesias_report import maybe_rewrite_yglesias
from pipeline.yglesias_writer import ReportOutput


def test_non_yglesias_feed_is_passthrough():
    body, title = maybe_rewrite_yglesias(
        body="essay body",
        title="2026-06-04 - ChinaTalk - Foo",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Foo",
    )
    assert body == "essay body"
    assert title == "2026-06-04 - ChinaTalk - Foo"


@patch("pipeline.yglesias_report.is_argument_transcript", return_value=False)
def test_yglesias_essay_is_passthrough(mock_detect):
    body, title = maybe_rewrite_yglesias(
        body="An essay on housing.",
        title="2026-06-04 - Slow Boring - Some Essay",
        feed_slug="yglesias",
        subject_raw="Slow Boring: Some Essay",
    )
    assert body == "An essay on housing."
    assert title == "2026-06-04 - Slow Boring - Some Essay"
    mock_detect.assert_called_once()


@patch("pipeline.yglesias_report.generate_report")
@patch("pipeline.yglesias_report.is_argument_transcript", return_value=True)
def test_yglesias_transcript_is_rewritten(mock_detect, mock_writer):
    mock_writer.return_value = ReportOutput(
        script="On The Argument, Jerusalem and Kelsey debated...",
        summary="Brief.",
    )
    body, title = maybe_rewrite_yglesias(
        body="Jerusalem Demsas: Hi\nKelsey Piper: Hello",
        title="2026-06-04 - Slow Boring - Can AI Cure Cancer?",
        feed_slug="yglesias",
        subject_raw="Can AI Cure Cancer?",
    )
    assert body.startswith("On The Argument")
    assert title == "Report: 2026-06-04 - Slow Boring - Can AI Cure Cancer?"
    mock_writer.assert_called_once_with(
        body="Jerusalem Demsas: Hi\nKelsey Piper: Hello",
        subject="Can AI Cure Cancer?",
    )


@patch(
    "pipeline.yglesias_report.generate_report",
    side_effect=RuntimeError("boom"),
)
@patch("pipeline.yglesias_report.is_argument_transcript", return_value=True)
def test_writer_failure_falls_back_to_reading(mock_detect, mock_writer):
    body, title = maybe_rewrite_yglesias(
        body="Jerusalem Demsas: Hi",
        title="2026-06-04 - Slow Boring - Episode",
        feed_slug="yglesias",
        subject_raw="Episode",
    )
    assert body == "Jerusalem Demsas: Hi"
    assert title == "2026-06-04 - Slow Boring - Episode"


@patch(
    "pipeline.yglesias_report.is_argument_transcript",
    side_effect=RuntimeError("detector crashed"),
)
def test_detector_failure_falls_back_to_reading(mock_detect):
    body, title = maybe_rewrite_yglesias(
        body="Jerusalem Demsas: Hi",
        title="2026-06-04 - Slow Boring - Episode",
        feed_slug="yglesias",
        subject_raw="Episode",
    )
    assert body == "Jerusalem Demsas: Hi"
    assert title == "2026-06-04 - Slow Boring - Episode"


def test_processor_calls_maybe_rewrite_yglesias():
    source = inspect.getsource(processor.process_email_bytes)
    assert "maybe_rewrite_yglesias" in source
    assert "preset.feed_slug" in source
```

**Step 2: Run tests, verify they fail**

Run: `uv run pytest pipeline/test_yglesias_report.py -v`
Expected: FAIL — module does not exist.

**Step 3: Create `pipeline/yglesias_report.py`**

```python
from __future__ import annotations

import logging

from pipeline.yglesias_filter import is_argument_transcript
from pipeline.yglesias_writer import generate_report


logger = logging.getLogger(__name__)


def maybe_rewrite_yglesias(
    *,
    body: str,
    title: str,
    feed_slug: str,
    subject_raw: str,
) -> tuple[str, str]:
    """Rewrite an Argument transcript into a spoken briefing if applicable.

    Returns ``(body, title)``. If the feed is not yglesias, or the body
    is not a transcript, or any step fails, returns the inputs unchanged
    so the caller falls back to a standard reading.
    """
    if feed_slug != "yglesias":
        return body, title

    try:
        if not is_argument_transcript(body):
            return body, title
        report = generate_report(body=body, subject=subject_raw)
        return report.script, f"Report: {title}"
    except Exception:
        logger.exception(
            "yglesias report path failed; falling back to standard reading"
        )
        return body, title
```

**Step 4: Run tests, verify they pass (except the wiring test — Task 4)**

Run: `uv run pytest pipeline/test_yglesias_report.py -v`
Expected: all PASS except `test_processor_calls_maybe_rewrite_yglesias`
(depends on Task 4). Note and proceed.

**Step 5: Commit**

```bash
git add pipeline/yglesias_report.py pipeline/test_yglesias_report.py
git commit -m "feat(yglesias): add report orchestrator (detect + rewrite)"
```

---

## Task 4: Wire into `processor.py` and drop the skip path

**Files:**
- Modify: `pipeline/processor.py`

**Step 1: Swap the import**

Replace:
```python
from pipeline.yglesias_filter import should_skip
```
with:
```python
from pipeline.yglesias_report import maybe_rewrite_yglesias
```

**Step 2: Remove the `skipped` field from `ProcessResult`**

Delete these lines from the dataclass (currently ~lines 39-42):
```python
    # True when the email was intentionally dropped (e.g. a Yglesias podcast
    # show-notes post). All other fields hold placeholder/blank values in
    # that case; callers should branch on this rather than reading them.
    skipped: bool = False
```

**Step 3: Replace the skip block with the rewrite call**

Delete the entire skip early-return block (currently ~lines 125-143):
```python
    # Some Yglesias newsletters are really the show notes + transcript for
    # his Argument podcast with Jerusalem Demsas. We can't usefully turn
    # those into audio (they're already audio), so drop them entirely.
    # Mark the source email as processed so the queue does not retry it.
    if should_skip(body=body, feed_slug=preset.feed_slug):
        print(f"Skipping Yglesias podcast post: {episode_title}")
        store.mark_processed(source_r2_key)
        return ProcessResult(
            r2_key="",
            title=episode_title,
            route_tag=route_tag,
            feed_slug=preset.feed_slug,
            category=preset.category,
            preset_name=preset.name,
            source_url=None,
            size_bytes=0,
            duration_seconds=None,
            skipped=True,
        )

```

The following `maybe_rewrite_chinatalk(...)` block stays. Add the yglesias
rewrite immediately before it so the two transcript hooks sit together:
```python
    # Some Yglesias newsletters are the full transcript of his Argument
    # podcast with Jerusalem Demsas. Rather than read 80 minutes of
    # transcript aloud, rewrite it into a spoken briefing (a "Report:").
    body, episode_title = maybe_rewrite_yglesias(
        body=body,
        title=episode_title,
        feed_slug=preset.feed_slug,
        subject_raw=subject_raw,
    )

    body, episode_title = maybe_rewrite_chinatalk(
        body=body,
        title=episode_title,
        feed_slug=preset.feed_slug,
        subject_raw=subject_raw,
    )
```

**Step 4: Run the wiring tests + the new module tests**

Run: `uv run pytest pipeline/test_yglesias_filter.py pipeline/test_yglesias_report.py -v`
Expected: all PASS now (including both wiring tests).

**Step 5: Commit**

```bash
git add pipeline/processor.py
git commit -m "feat(yglesias): wire report path into processor, remove skip early-return"
```

---

## Task 5: Remove the dead skip branch in `__main__.py`

**Files:**
- Modify: `pipeline/__main__.py`

**Step 1: Delete the `result.skipped` branch in `process_command`**

Remove (currently ~lines 270-273):
```python
        if result.skipped:
            click.echo(f"Skipped (filter): {result.title}")
            click.echo(f"Feed: {result.feed_slug}")
            return
```
so the function proceeds directly to `click.echo(f"Uploaded episode: ...")`.

**Step 2: Verify no remaining `.skipped` references**

Run: `uv run rg -n "\.skipped|should_skip|is_demsas_podcast|_PODCAST_MARKERS" pipeline/`
Expected: no matches.

**Step 3: Commit**

```bash
git add pipeline/__main__.py
git commit -m "chore(yglesias): remove dead skip branch from process CLI"
```

---

## Task 6: Update docs (`AGENTS.md`)

**Files:**
- Modify: `AGENTS.md` (root) — the "## Yglesias Podcast Filter" section.

**Step 1: Rewrite the section**

Replace the existing "Yglesias Podcast Filter" section with a description of
the report path. Suggested content:

```markdown
## Yglesias Argument Transcript Reports

Slow Boring publishes the newsletter form of Matt Yglesias's *The Argument*
podcast with Jerusalem Demsas (regular weekly episodes and one-off live
events). For paying subscribers the body carries the full transcript (~80
minutes of TTS). Rather than read that verbatim, the pipeline rewrites it
into a spoken briefing — mirroring the ChinaTalk transcript report path — and
publishes it with a "Report: " title prefix.

**Detection:** `pipeline/yglesias_filter.is_argument_transcript` is a
deterministic transcript-shape detector. It counts line-start speaker labels
and fires only when at least two distinct speakers each take five or more
turns. No LLM call. Survives Substack footer changes; near-impossible to
trigger on a normal essay.

**Generation:** `pipeline/yglesias_writer.generate_report` (opencode-serve,
mirrors `chinatalk_writer.py`, 900-second timeout, rejects empty output) with
a prompt tuned for *The Argument*.

**Wiring:** `pipeline/yglesias_report.maybe_rewrite_yglesias` is called from
`pipeline/processor.process_email_bytes` after body cleaning, before TTS
(alongside the ChinaTalk hook). It is yglesias-only and fails safe: any
exception in detection or generation falls back to the standard reading, so
the listener always gets an episode (a long reading rather than a dropped
essay).
```

Also update the `## Content Routing` / module-path references elsewhere in
`AGENTS.md` only if they mention `yglesias_filter.should_skip` (they currently
do not — verify with rg).

**Step 2: Verify**

Run: `uv run rg -n "yglesias_filter|should_skip|is_demsas" AGENTS.md pipeline/AGENTS.md`
Expected: only the new `is_argument_transcript` reference (if any).

**Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs(yglesias): document transcript report path"
```

---

## Task 7: Full suite + real-email smoke check

**Step 1: Run the full test suite**

Run: `uv run pytest -q`
Expected: all green (no regressions in processor/chinatalk/consumer tests).

**Step 2: Smoke-test detection against the real email**

Run:
```bash
uv run python -c "
from pathlib import Path
from email_processor.api import EmailProcessor
from pipeline.source_adapters import get_source_adapter
from pipeline.yglesias_filter import is_argument_transcript
raw = Path('/tmp/Can-AI-Cure-Cancer.eml').read_bytes()
body = EmailProcessor(raw).parse()['body']
cleaned = get_source_adapter('yglesias').clean_body(raw_email=raw, body=body)
print('is_argument_transcript:', is_argument_transcript(cleaned))
"
```
Expected: `is_argument_transcript: True`.

**Step 3 (optional, requires opencode-serve + network): end-to-end report**

Only if a local opencode-serve is available. Run the `process` CLI against the
sample eml with the yglesias route tag and confirm the resulting title starts
with `Report:` and the uploaded body is the briefing, not the transcript. Skip
in CI / offline.

**Step 4: Close the beads issue and land the plane**

```bash
bd close my-podcasts-adx --reason="Yglesias transcript report path shipped"
git pull --rebase
git push
git status   # MUST show up to date with origin
```

---

## Notes for the implementer

- `ReportOutput.summary` is parsed but unused (no show-notes storage for
  yglesias today), mirroring ChinaTalk. Keep it for prompt-structure parity.
- The yglesias and chinatalk rewrites are mutually exclusive by `feed_slug`;
  order between them does not matter.
- Conservative fallback semantics changed only in *consequence*: a missed
  transcript now becomes a long reading instead of a silent drop. That is the
  intended behavior.
```
