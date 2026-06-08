# Transcript Report Path Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make ChinaTalk transcript detection deterministic (drop the Gemini classifier) and make a briefing-generation failure on a confirmed transcript raise (queue retry) instead of silently shipping a literal read — for both the ChinaTalk and Yglesias feeds.

**Architecture:** Extract the proven Yglesias speaker-label detector into a shared `pipeline/transcript_detect.py:looks_like_transcript`. ChinaTalk's report path calls it directly; Yglesias keeps `is_argument_transcript` as a thin delegator. Both `maybe_rewrite_*` orchestrators stop swallowing generation exceptions for confirmed transcripts and let them propagate out of `process_email_bytes`, where the consumer leaves the queue message unacked for redelivery.

**Tech Stack:** Python 3.14, pytest, uv.

---

### Task 1: Shared deterministic detector

**Files:**
- Create: `pipeline/transcript_detect.py`
- Create: `pipeline/test_transcript_detect.py`
- Modify: `pipeline/yglesias_filter.py`

**Step 1: Write failing tests** in `pipeline/test_transcript_detect.py`:

```python
from pipeline.transcript_detect import looks_like_transcript


def test_two_speakers_five_turns_each_is_transcript():
    body = "".join(f"Alice: {i}\nBob: {i}\n" for i in range(5))
    assert looks_like_transcript(body) is True


def test_essay_is_not_transcript():
    assert looks_like_transcript("An essay on policy. No speaker turns here.") is False


def test_single_speaker_is_not_transcript():
    body = "".join(f"Alice: {i}\n" for i in range(10))
    assert looks_like_transcript(body) is False


def test_two_speakers_under_threshold_is_not_transcript():
    body = "".join(f"Alice: {i}\nBob: {i}\n" for i in range(4))
    assert looks_like_transcript(body) is False


def test_empty_body_is_not_transcript():
    assert looks_like_transcript("") is False


def test_chinatalk_shaped_transcript_is_transcript():
    body = (
        "Jordan Schneider: Welcome back.\n"
        "Rob Lee: Glad to be here.\n"
        "Jordan Schneider: Tell us about the front.\n"
        "Rob Lee: It's underground.\n"
        "Jordan Schneider: And drones?\n"
        "Rob Lee: Everywhere.\n"
        "Jordan Schneider: Logistics?\n"
        "Rob Lee: By UGV.\n"
        "Jordan Schneider: Casualties?\n"
        "Rob Lee: Mostly drones.\n"
    )
    assert looks_like_transcript(body) is True
```

**Step 2:** Run `uv run pytest pipeline/test_transcript_detect.py -v` → FAIL (module missing).

**Step 3: Implement** `pipeline/transcript_detect.py` by moving the logic from `yglesias_filter.py` (regex `_SPEAKER_TURN_RE`, `_APOS`, `_NAME`, `_MIN_SPEAKERS`, `_MIN_TURNS_PER_SPEAKER`, the import-time assert) into `looks_like_transcript(body: str) -> bool`.

**Step 4:** Run the test file → PASS.

**Step 5: Update `yglesias_filter.py`** to delegate (keep public name + docstring):

```python
from pipeline.transcript_detect import looks_like_transcript


def is_argument_transcript(body: str) -> bool:
    return looks_like_transcript(body)
```

**Step 6:** Run `uv run pytest pipeline/test_yglesias_filter.py pipeline/test_transcript_detect.py -v` → PASS.

**Step 7: Commit** `feat: extract shared deterministic transcript detector`.

---

### Task 2: ChinaTalk detection goes deterministic; delete the Gemini classifier

**Files:**
- Modify: `pipeline/chinatalk_report.py`
- Modify: `pipeline/test_chinatalk_report.py`
- Delete: `pipeline/chinatalk_classifier.py`, `pipeline/test_chinatalk_classifier.py`

**Step 1: Rewrite tests** in `pipeline/test_chinatalk_report.py` so they exercise the real detector (no `is_transcript` patch). Keep `test_non_chinatalk_feed_is_passthrough` and `test_processor_calls_maybe_rewrite_chinatalk`. Replace the others:

```python
import pytest
from pipeline.chinatalk_report import maybe_rewrite_chinatalk
from pipeline.chinatalk_writer import ReportOutput

_TRANSCRIPT = "".join(f"Alice: {i}\nBob: {i}\n" for i in range(5))


def test_chinatalk_essay_is_passthrough():
    body, title = maybe_rewrite_chinatalk(
        body="An essay on policy.",
        title="2026-04-25 - ChinaTalk - Some Essay",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Some Essay",
    )
    assert body == "An essay on policy."
    assert title == "2026-04-25 - ChinaTalk - Some Essay"


@patch("pipeline.chinatalk_report.generate_report")
def test_chinatalk_transcript_is_rewritten(mock_writer):
    mock_writer.return_value = ReportOutput(script="A briefing.", summary="Brief.")
    body, title = maybe_rewrite_chinatalk(
        body=_TRANSCRIPT,
        title="2026-04-25 - ChinaTalk - Episode 42",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Episode 42",
    )
    assert body == "A briefing."
    assert title == "Report: 2026-04-25 - ChinaTalk - Episode 42"
    mock_writer.assert_called_once_with(body=_TRANSCRIPT, subject="ChinaTalk: Episode 42")


@patch("pipeline.chinatalk_report.generate_report", side_effect=RuntimeError("boom"))
def test_chinatalk_writer_failure_propagates(mock_writer):
    with pytest.raises(RuntimeError):
        maybe_rewrite_chinatalk(
            body=_TRANSCRIPT,
            title="2026-04-25 - ChinaTalk - Episode 42",
            feed_slug="chinatalk",
            subject_raw="ChinaTalk: Episode 42",
        )
```

(Delete `test_classifier_failure_falls_back_to_reading`.)

**Step 2:** Run `uv run pytest pipeline/test_chinatalk_report.py -v` → FAIL.

**Step 3: Rewrite `pipeline/chinatalk_report.py`:**

```python
from __future__ import annotations

import logging

from pipeline.chinatalk_writer import generate_report
from pipeline.transcript_detect import looks_like_transcript

logger = logging.getLogger(__name__)


def maybe_rewrite_chinatalk(*, body, title, feed_slug, subject_raw):
    if feed_slug != "chinatalk":
        return body, title
    if not looks_like_transcript(body):
        return body, title
    try:
        report = generate_report(body=body, subject=subject_raw)
    except Exception:
        logger.exception(
            "chinatalk report generation failed for a confirmed transcript; "
            "not shipping a literal read"
        )
        raise
    return report.script, f"Report: {title}"
```

**Step 4:** `git rm pipeline/chinatalk_classifier.py pipeline/test_chinatalk_classifier.py`.

**Step 5:** Run `uv run pytest pipeline/test_chinatalk_report.py -v` → PASS.

**Step 6: Commit** `feat: deterministic chinatalk transcript detection; drop gemini classifier`.

---

### Task 3: Raise-on-generation-failure for Yglesias

**Files:**
- Modify: `pipeline/yglesias_report.py`
- Modify: `pipeline/test_yglesias_report.py`

**Step 1: Update tests** — replace `test_writer_failure_falls_back_to_reading` and `test_detector_failure_falls_back_to_reading` with propagation tests:

```python
@patch("pipeline.yglesias_report.generate_report", side_effect=RuntimeError("boom"))
@patch("pipeline.yglesias_report.is_argument_transcript", return_value=True)
def test_yglesias_writer_failure_propagates(mock_detect, mock_writer):
    with pytest.raises(RuntimeError):
        maybe_rewrite_yglesias(
            body="Jerusalem Demsas: Hi",
            title="2026-06-04 - Slow Boring - Episode",
            feed_slug="yglesias",
            subject_raw="Episode",
        )
```

(Keep the passthrough and rewrite tests. Add `import pytest`.)

**Step 2:** Run `uv run pytest pipeline/test_yglesias_report.py -v` → FAIL.

**Step 3: Update `pipeline/yglesias_report.py`** to the same structure as `chinatalk_report` (call `is_argument_transcript`; on detection True, try `generate_report`, log + re-raise on exception; no broad swallow).

**Step 4:** Run `uv run pytest pipeline/test_yglesias_report.py -v` → PASS.

**Step 5: Commit** `feat: yglesias report generation failure propagates for confirmed transcripts`.

---

### Task 4: Real-data validation guard

**Files:**
- Modify: `pipeline/test_transcript_detect.py`

**Step 1:** Add a test that builds an essay-shaped body (prose paragraphs, no repeated `Name:` line-starts) and asserts `looks_like_transcript(...) is False`, plus a larger multi-speaker block asserting `True`, to lock the false-positive/false-negative boundary. (The live WarTalk `.eml` was already verified True manually; no network in tests.)

**Step 2:** Run `uv run pytest pipeline/test_transcript_detect.py -v` → PASS.

**Step 3: Commit** `test: lock transcript detector false-positive boundary`.

---

### Task 5: Docs + full suite

**Files:**
- Modify: `AGENTS.md`

**Step 1:** Update the ChinaTalk references in `AGENTS.md`:
- "Core Paths" line: replace `pipeline/chinatalk_classifier.py` with `pipeline/transcript_detect.py`.
- "ChinaTalk Transcript Report Path → Detection" paragraph: detection is now deterministic via `pipeline/transcript_detect.py` (shared with Yglesias, no Gemini dependency); a generation failure on a confirmed transcript raises (queue retry) instead of falling back to a standard reading.

**Step 2:** Run the full suite: `uv run pytest` → all green.

**Step 3: Commit** `docs: chinatalk detection is deterministic; note raise-on-failure`.
