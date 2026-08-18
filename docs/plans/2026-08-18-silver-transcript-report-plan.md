# Silver Transcript Reports Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give Silver Bulletin conversation transcripts the same spoken-briefing treatment ChinaTalk and Yglesias already get, and collapse the four copies of the opencode-serve report mechanics into one.

**Architecture:** A new leaf module `pipeline/report_engine.py` owns the opencode-serve session + tag-extraction mechanics. A new `pipeline/transcript_report.py` owns a `feed_slug -> prompt template` registry plus the detect-and-rewrite gate, replacing five modules. `pipeline/report_writer.py` delegates its mechanics to the engine. `pipeline/processor.py` calls one gate instead of two.

**Tech Stack:** Python 3, `uv`, `pytest`, `unittest.mock.patch`. No new dependencies.

**Design doc:** `docs/plans/2026-08-18-silver-transcript-report-design.md` — read it first, especially the corpus evidence table and the "Known gaps" section.

**Background you need:**
- The pipeline turns newsletter emails into podcast episodes. `pipeline/processor.py:process_email_bytes` is the whole email path: parse → adapter cleans body → (transcript hook) → TTS via `ttsjoin` → upload to R2 → feed regenerate.
- A "transcript report" replaces the episode body with an AI-written briefing and prefixes the title with `Report: `.
- Detection is deterministic and already written: `pipeline/transcript_detect.looks_like_transcript`. **Do not modify it.** It is validated against the full Silver archive (see design doc).
- opencode-serve is a local HTTP LLM service. `pipeline/opencode_client.py` wraps it. In tests it is always mocked; never let a test make a real call.
- Run tests with `uv run pytest`. Lint with `uv run ruff check pipeline` and `uv run ruff format --check pipeline` (confirm the exact lint invocation from `pyproject.toml` before the first commit).

---

## Task 1: Extract the shared report engine

**Files:**
- Create: `pipeline/report_engine.py`
- Create: `pipeline/test_report_engine.py`

**Step 1: Write the failing tests**

Create `pipeline/test_report_engine.py`:

```python
from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.report_engine import (
    ReportOutput,
    extract_script,
    extract_summary,
    run_report_prompt,
)


# --- extraction ---


def test_extract_script_with_tags():
    raw = "Reasoning here.\n\n<script>The briefing text.</script>"
    assert extract_script(raw) == "The briefing text."


def test_extract_script_no_tags_falls_back_without_summary_prose():
    """Without <script> tags the response passes through, minus the summary.

    This closes my-podcasts-ne0. The old fallback returned the FULL model
    output, which includes the <summary> block and any literal tags --
    strip_markdown_for_tts does not strip HTML, so those were narrated aloud.
    """
    raw = "<summary>Brief.</summary>\n\nThe briefing without script tags."
    assert extract_script(raw) == "The briefing without script tags."


def test_extract_script_unclosed_final_block_is_recovered():
    """my-podcasts-ne0: a mis-closed </script> must not fall back to raw output.

    Observed 2026-06-16 on an arXiv dry run. This case also COMPOSES with the
    longest-block fix: a well-formed placeholder pair followed by the real
    script with a broken closing tag leaves findall seeing only the
    placeholder, so the longest block would be the placeholder. The unclosed
    tail must compete on length.
    """
    real = "The real script. " * 50
    raw = f"<script>...</script>\n\n<script>{real}</scrip>"
    assert extract_script(raw).startswith("The real script.")
    assert "scrip" not in extract_script(raw)


def test_extract_script_picks_longest_block():
    """Regression for the 2026-08-18 incident (commit 39589e3).

    The model sometimes emits a placeholder <script>...</script> while
    planning, before writing the real one. A non-greedy re.search matched the
    placeholder and FP Digest shipped a 2636-byte mp3. The fix landed only in
    rundown_writer.py; the transcript path still had the defect, where a
    placeholder would replace an 80-minute transcript.
    """
    raw = "<script>...</script>\n\n<script>" + "The real script. " * 50 + "</script>"
    assert extract_script(raw).startswith("The real script.")
    assert len(extract_script(raw)) > 500


def test_extract_summary_with_tags():
    raw = "<summary>Brief.</summary>\n\nThe rest."
    assert extract_summary(raw) == "Brief."


def test_extract_summary_no_tags_returns_empty():
    assert extract_summary("No summary tags here.") == ""


# --- session mechanics ---


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_happy_path(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_1"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Brief.</summary>\n<script>Spoken words.</script>"

    result = run_report_prompt("INSTRUCTION", label="chinatalk")

    assert result == ReportOutput(script="Spoken words.", summary="Brief.")
    mock_send.assert_called_once_with("ses_1", "INSTRUCTION")
    mock_wait.assert_called_once_with("ses_1", timeout=900)
    mock_delete.assert_called_once_with("ses_1")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_timeout_raises_and_cleans_up(
    mock_create, mock_send, mock_wait, mock_delete
):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False

    with pytest.raises(RuntimeError, match="silver .*900 seconds"):
        run_report_prompt("INSTRUCTION", label="silver")

    mock_delete.assert_called_once_with("ses_timeout")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_empty_script_raises(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_empty"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Anything.</summary>\n\n<script>   </script>"

    with pytest.raises(RuntimeError, match="empty script"):
        run_report_prompt("INSTRUCTION", label="yglesias")

    mock_delete.assert_called_once_with("ses_empty")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_min_chars_rejects_short_script(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """min_chars is opt-in: emptiness is the wrong test on a publish path."""
    mock_create.return_value = "ses_short"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<script>Too short to be an episode.</script>"

    with pytest.raises(RuntimeError, match="too short to be real"):
        run_report_prompt("INSTRUCTION", label="silver", min_chars=500)

    mock_delete.assert_called_once_with("ses_short")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_default_has_no_length_floor(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """Default min_chars=0 preserves report_writer's existing behavior."""
    mock_create.return_value = "ses_default"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<script>Short but allowed.</script>"

    result = run_report_prompt("INSTRUCTION", label="paper")

    assert result.script == "Short but allowed."
```

**Step 2: Run to verify failure**

Run: `uv run pytest pipeline/test_report_engine.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'pipeline.report_engine'`

**Step 3: Write the implementation**

Create `pipeline/report_engine.py`:

```python
"""Shared opencode-serve mechanics for every spoken-briefing writer.

These mechanics -- create a session, send one instruction, wait for idle,
read the last assistant message, pull <summary>/<script> out of it, always
delete the session -- existed in four near-identical copies (chinatalk,
yglesias, report_writer, rundown). This is their single home.

Leaf module: imports only ``pipeline.opencode_client``, so any writer can use
it without an import cycle.
"""

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


WRITER_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class ReportOutput:
    script: str
    summary: str


def extract_script(text: str) -> str:
    """Extract the spoken script from ``<script>...</script>`` tags.

    Three defects converge here, which is why this lives in one place.

    Picks the **longest** block, not the first. The model sometimes emits a
    placeholder ``<script>...</script>`` while planning, before writing the
    real one -- on 2026-08-18 an FP Digest episode shipped as a 2636-byte mp3
    because a non-greedy ``re.search`` matched a 3-character placeholder at
    offset 647 instead of the 11814-character script at offset 2523. That fix
    (commit 39589e3) landed only in ``rundown_writer``.

    Recovers an **unclosed final block** (my-podcasts-ne0): on 2026-06-16 the
    model closed the script with a mangled tag, ``findall`` matched nothing,
    and the old fallback returned the entire model output for narration. Note
    this composes with the case above -- a well-formed placeholder followed by
    a mis-closed real script leaves ``findall`` seeing only the placeholder --
    so the tail must compete on length rather than being a last resort.

    When there is no script tag at all, the fallback strips the ``<summary>``
    block and any stray literal tags. ``strip_markdown_for_tts`` does not
    strip HTML, so without this the summary prose and the tags themselves are
    read aloud.

    Residual known ambiguity: an unclosed ``<summary>`` with no script tags.
    The emptiness and ``min_chars`` guards in ``run_report_prompt`` catch it
    loudly rather than shipping it.
    """
    candidates = re.findall(r"<script>\s*(.*?)\s*</script>", text, re.DOTALL)
    last_open = text.rfind("<script>")
    if last_open != -1 and last_open > text.rfind("</script>"):
        tail = text[last_open + len("<script>") :].strip()
        tail = re.sub(r"</?scr[a-z]*[^>]*>?\s*$", "", tail).strip()
        candidates.append(tail)
    if candidates:
        return max(candidates, key=len).strip()
    fallback = re.sub(r"<summary>.*?</summary>", "", text, flags=re.DOTALL)
    return fallback.replace("<script>", "").replace("</script>", "").strip()


def extract_summary(text: str) -> str:
    """Extract the ``<summary>`` block, returning an empty string if absent."""
    m = re.search(r"<summary>\s*(.*?)\s*</summary>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def run_report_prompt(
    instruction: str,
    *,
    label: str,
    min_chars: int = 0,
) -> ReportOutput:
    """Run one instruction through opencode-serve and parse the result.

    ``label`` names the caller in error messages (a feed slug or a style).
    ``min_chars`` is an opt-in plausibility floor for callers whose output goes
    straight to TTS: emptiness is the wrong test on a publish path, because a
    3-character placeholder is not empty. It defaults to 0 so callers that
    review output before publishing are unaffected.
    """
    session_id = create_session()
    try:
        send_prompt_async(session_id, instruction)
        if not wait_for_idle(session_id, timeout=WRITER_TIMEOUT_SECONDS):
            raise RuntimeError(
                f"{label} report writer did not complete within "
                f"{WRITER_TIMEOUT_SECONDS} seconds"
            )
        messages = get_messages(session_id)
        full_text = get_last_assistant_text(messages).strip()
        script = extract_script(full_text)
        summary = extract_summary(full_text)
        if not script.strip():
            raise RuntimeError(f"{label} report writer returned empty script")
        if min_chars and len(script.strip()) < min_chars:
            raise RuntimeError(
                f"{label} report writer returned a script too short to be real: "
                f"{len(script.strip())} chars (minimum {min_chars})"
            )
        return ReportOutput(script=script, summary=summary)
    finally:
        delete_session(session_id)
```

**Step 4: Run to verify pass**

Run: `uv run pytest pipeline/test_report_engine.py -q`
Expected: all pass.

**Step 5: Commit**

```bash
git add pipeline/report_engine.py pipeline/test_report_engine.py
git commit -m "feat(writers): add a shared report engine, closing my-podcasts-ne0

Four copies of the opencode-serve report mechanics get one home, and the
three defects that had been fixed unevenly across them get fixed once:
longest-block selection (39589e3, previously rundown-only), unclosed
final-block recovery (ne0, never fixed), and a no-tag fallback that no
longer narrates the summary prose and the literal tags."
```

---

## Task 2: Delegate `report_writer` to the engine

**Files:**
- Modify: `pipeline/report_writer.py:80-140`
- Check: `pipeline/test_report_writer.py` (must stay green unmodified except for patch targets)

**Step 1: Confirm the current tests pass**

Run: `uv run pytest pipeline/test_report_writer.py -q`
Expected: PASS. This is the baseline you must preserve.

**Step 2: Rewrite the tail of `report_writer.py`**

Delete `ReportOutput`, `_extract_script`, `_extract_summary`, the
`opencode_client` imports, and the body of `generate_report`. Keep
`_INTERVIEW_TEMPLATE`, `_PAPER_TEMPLATE`, `_TEMPLATES`, and
`build_report_prompt` exactly as they are. Replace with:

```python
from pipeline.report_engine import ReportOutput, run_report_prompt


__all__ = ["ReportOutput", "build_report_prompt", "generate_report"]


def generate_report(
    *, body: str, subject: str, style: str = "interview", byline: str = ""
) -> ReportOutput:
    """Generate a spoken-briefing report on a source document."""
    prompt = build_report_prompt(body=body, subject=subject, style=style, byline=byline)
    instruction = (
        "Read the following source text and produce the spoken briefing. "
        "First write a 2-3 sentence summary wrapped in <summary>...</summary> "
        "tags. Then write the full spoken script wrapped in "
        "<script>...</script> tags. Output nothing outside these tags.\n\n" + prompt
    )
    return run_report_prompt(instruction, label="report")
```

Keep `ReportOutput` importable from `pipeline.report_writer` (the re-export
above) so `pipeline/__main__.py` and existing tests do not need to change.

**Step 3: Fix the test patch targets**

`pipeline/test_report_writer.py` patches `pipeline.report_writer.create_session`
and friends. Those names now live in the engine. Update every such decorator to
`pipeline.report_engine.<name>`. Do **not** change any assertion — the point of
this task is that behavior is unchanged.

Two classes of assertion legitimately change; everything else must stay:

1. Any assertion pinning the timeout or empty-script error string, which now
   carries the `report` label.
2. **The no-tags fallback assertion.** `test_report_writer.py` (and the
   chinatalk/yglesias equivalents retired in Task 6) asserts the full model
   output passes through when `<script>` tags are absent. That behavior is
   `my-podcasts-ne0` and Task 1 deliberately changed it: the fallback now
   strips the `<summary>` block and stray literal tags. Update the expectation
   and say so in the commit message.

If a *behavioral* assertion outside those two classes fails, stop and
investigate — that means the refactor was not behavior-preserving.

**Step 4: Run the tests**

Run: `uv run pytest pipeline/test_report_writer.py pipeline/test_report_engine.py -q`
Expected: all pass.

**Step 5: Commit**

```bash
git add pipeline/report_writer.py pipeline/test_report_writer.py
git commit -m "refactor(report_writer): delegate session mechanics to report_engine"
```

---

## Task 3: Create `transcript_report` with the two existing feeds, proven byte-identical

This task deliberately runs **while the old modules still exist**, so the
golden test can compare new output against the real old implementation. Do not
delete anything yet.

**Files:**
- Create: `pipeline/transcript_report.py`
- Create: `pipeline/test_transcript_report.py`

**Step 1: Copy the two templates verbatim**

Do not retype them. Copy the exact `PROMPT_TEMPLATE` string literal out of
`pipeline/chinatalk_writer.py:16-46` and `pipeline/yglesias_writer.py:16-50`
into the new module as `_CHINATALK_TEMPLATE` and `_YGLESIAS_TEMPLATE`. Any
whitespace drift will be caught by Step 3.

**Step 2: Write the golden equivalence test first**

Add to `pipeline/test_transcript_report.py`:

```python
from __future__ import annotations

from pipeline import chinatalk_writer, yglesias_writer
from pipeline.transcript_report import build_report_prompt


_BODY = "Alice: hello\nBob: hi\n"
_SUBJECT = "Some Subject"


def test_chinatalk_prompt_is_byte_identical_to_the_retired_module():
    assert build_report_prompt(
        body=_BODY, subject=_SUBJECT, feed_slug="chinatalk"
    ) == chinatalk_writer.build_report_prompt(body=_BODY, subject=_SUBJECT)


def test_yglesias_prompt_is_byte_identical_to_the_retired_module():
    assert build_report_prompt(
        body=_BODY, subject=_SUBJECT, feed_slug="yglesias"
    ) == yglesias_writer.build_report_prompt(body=_BODY, subject=_SUBJECT)
```

Run: `uv run pytest pipeline/test_transcript_report.py -q`
Expected: FAIL — module does not exist.

**Step 3: Write `transcript_report.py` with only the two existing feeds**

```python
"""Rewrite podcast-transcript newsletter bodies into spoken briefings.

Several newsletters occasionally ship the verbatim transcript of a podcast
conversation as the email body. Reading that aloud is 60-80 minutes of
narration nobody asked for, so for a *confirmed* transcript we generate a
spoken briefing about the conversation instead and prefix the episode title
with ``Report: ``.

One registry, one gate, one writer. Feeds differ only by prompt template, and
those templates are separate constants so an edit aimed at one feed cannot
reach another.

Detection is deterministic and content-only
(``pipeline.transcript_detect.looks_like_transcript``) -- no LLM, no network,
so it cannot silently fail during a remote outage the way an earlier Gemini
classifier did on 2026-05-26.
"""

from __future__ import annotations

import logging

from pipeline.report_engine import ReportOutput, run_report_prompt
from pipeline.transcript_detect import looks_like_transcript


logger = logging.getLogger(__name__)

# A briefing that replaces a whole episode body must be long enough to *be* an
# episode. See report_engine.run_report_prompt's min_chars.
_MIN_SCRIPT_CHARS = 500

_CHINATALK_TEMPLATE = """\
<copied verbatim from chinatalk_writer.PROMPT_TEMPLATE>
"""

_YGLESIAS_TEMPLATE = """\
<copied verbatim from yglesias_writer.PROMPT_TEMPLATE>
"""

TRANSCRIPT_FEEDS: dict[str, str] = {
    "chinatalk": _CHINATALK_TEMPLATE,
    "yglesias": _YGLESIAS_TEMPLATE,
}


def build_report_prompt(*, body: str, subject: str, feed_slug: str) -> str:
    try:
        template = TRANSCRIPT_FEEDS[feed_slug]
    except KeyError:
        raise ValueError(f"No transcript prompt for feed: {feed_slug!r}") from None
    return template.format(subject=subject, body=body)


def generate_report(*, body: str, subject: str, feed_slug: str) -> ReportOutput:
    """Generate a spoken-briefing report on a transcript body."""
    prompt = build_report_prompt(body=body, subject=subject, feed_slug=feed_slug)
    instruction = (
        "Read the following transcript and produce the spoken briefing. "
        "First write a 2-3 sentence summary wrapped in <summary>...</summary> "
        "tags. Then write the full spoken script wrapped in "
        "<script>...</script> tags. Output nothing outside these tags.\n\n" + prompt
    )
    return run_report_prompt(
        instruction, label=feed_slug, min_chars=_MIN_SCRIPT_CHARS
    )


def maybe_rewrite_transcript(
    *,
    body: str,
    title: str,
    feed_slug: str,
    subject_raw: str,
) -> tuple[str, str]:
    """Rewrite a transcript body into a spoken briefing if applicable.

    Returns ``(body, title)`` unchanged when the feed has no transcript prompt
    or the body is not a transcript, so the caller falls back to a standard
    reading.

    For a *confirmed* transcript whose briefing cannot be generated, the
    exception is logged and re-raised rather than silently degrading to a
    literal read: it bubbles out of ``process_email_bytes``, the consumer
    leaves the queue message unacked, and the email is reprocessed on
    redelivery.
    """
    if feed_slug not in TRANSCRIPT_FEEDS:
        return body, title
    if not looks_like_transcript(body):
        return body, title
    try:
        report = generate_report(body=body, subject=subject_raw, feed_slug=feed_slug)
    except Exception:
        logger.exception(
            "%s report generation failed for a confirmed transcript; "
            "not shipping a literal read",
            feed_slug,
        )
        raise
    return report.script, f"Report: {title}"
```

**Step 4: Run the golden tests**

Run: `uv run pytest pipeline/test_transcript_report.py -q`
Expected: both pass. **If either fails, the templates were not copied
verbatim — fix the copy, do not adjust the test.**

**Step 5: Port the gate and writer tests, parametrized**

Add to `pipeline/test_transcript_report.py` — port every case from
`test_chinatalk_report.py`, `test_yglesias_report.py`, and
`test_chinatalk_writer.py`, parametrized over the registered slugs:

```python
import pytest
from unittest.mock import patch

from pipeline.report_engine import ReportOutput
from pipeline.transcript_report import (
    TRANSCRIPT_FEEDS,
    generate_report,
    maybe_rewrite_transcript,
)


_TRANSCRIPT = "".join(f"Alice: line {i}\nBob: line {i}\n" for i in range(5))
_LONG_SCRIPT = "A spoken briefing sentence. " * 40  # > _MIN_SCRIPT_CHARS


def test_unregistered_feed_is_passthrough():
    body, title = maybe_rewrite_transcript(
        body=_TRANSCRIPT,
        title="2026-04-25 - Money Stuff - Foo",
        feed_slug="levine",
        subject_raw="Money Stuff: Foo",
    )
    assert body == _TRANSCRIPT
    assert title == "2026-04-25 - Money Stuff - Foo"


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
def test_essay_is_passthrough(slug):
    body, title = maybe_rewrite_transcript(
        body="An essay on policy. No speaker turns here.",
        title="T",
        feed_slug=slug,
        subject_raw="S",
    )
    assert body == "An essay on policy. No speaker turns here."
    assert title == "T"


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
@patch("pipeline.transcript_report.generate_report")
def test_transcript_is_rewritten(mock_writer, slug):
    mock_writer.return_value = ReportOutput(script=_LONG_SCRIPT, summary="Brief.")
    body, title = maybe_rewrite_transcript(
        body=_TRANSCRIPT, title="Ep 42", feed_slug=slug, subject_raw="Subject 42"
    )
    assert body == _LONG_SCRIPT
    assert title == "Report: Ep 42"
    mock_writer.assert_called_once_with(
        body=_TRANSCRIPT, subject="Subject 42", feed_slug=slug
    )


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
@patch(
    "pipeline.transcript_report.generate_report", side_effect=RuntimeError("boom")
)
def test_writer_failure_propagates(mock_writer, slug):
    """A confirmed transcript must never silently degrade to a literal read."""
    with pytest.raises(RuntimeError):
        maybe_rewrite_transcript(
            body=_TRANSCRIPT, title="Ep 42", feed_slug=slug, subject_raw="Subject 42"
        )


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
def test_prompt_contains_subject_and_body(slug):
    from pipeline.transcript_report import build_report_prompt

    prompt = build_report_prompt(body="BODYMARKER", subject="SUBJMARKER", feed_slug=slug)
    assert "BODYMARKER" in prompt
    assert "SUBJMARKER" in prompt


def test_build_report_prompt_rejects_unknown_feed():
    from pipeline.transcript_report import build_report_prompt

    with pytest.raises(ValueError, match="levine"):
        build_report_prompt(body="b", subject="s", feed_slug="levine")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_report_enforces_the_length_floor(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """A placeholder script must not replace a real transcript body."""
    mock_create.return_value = "ses_x"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<script>...</script>"

    with pytest.raises(RuntimeError, match="too short to be real"):
        generate_report(body="b", subject="s", feed_slug="chinatalk")

    mock_delete.assert_called_once_with("ses_x")
```

**Step 6: Run and commit**

Run: `uv run pytest pipeline/test_transcript_report.py -q`
Expected: all pass.

```bash
git add pipeline/transcript_report.py pipeline/test_transcript_report.py
git commit -m "feat(transcript): one registry-driven transcript report path

Golden tests assert the chinatalk and yglesias prompts are byte-identical to
the modules they replace, so the migration is provably behavior-preserving
for two live feeds."
```

---

## Task 4: Add the `silver` feed with real-corpus fixtures

**Files:**
- Modify: `pipeline/transcript_report.py` (add `_SILVER_TEMPLATE` + registry entry)
- Create: `pipeline/fixtures/silver_transcript.txt`
- Create: `pipeline/fixtures/silver_essay.txt`
- Create: `pipeline/fixtures/silver_mailbag.txt`
- Modify: `pipeline/test_transcript_detect.py`

**Step 1: Produce the fixtures from real email-cleaned bodies**

The fixtures must come from the **email** path, not the web fetcher — see the
design doc. If `/tmp/opencode/replay-bodies/` still exists from the design
session, use it; otherwise re-run the replay script in the Appendix first.

```bash
mkdir -p pipeline/fixtures
# transcript: keep the essay preamble plus enough turns to clear the 5-turn floor
head -c 8000 "/tmp/opencode/replay-bodies/2026-08-17-T-Why-does-everyone-hate-data-cente.txt" \
  > pipeline/fixtures/silver_transcript.txt
head -c 8000 "/tmp/opencode/replay-bodies/2026-08-15-F-A-tour-of-the-7-key-Senate-races.txt" \
  > pipeline/fixtures/silver_essay.txt
head -c 8000 "/tmp/opencode/replay-bodies/2026-04-21-F-SBSQ-31-Trump-is-super-unpopular-So.txt" \
  > pipeline/fixtures/silver_mailbag.txt
```

Verify the truncation did not destroy what each fixture is meant to prove:

```bash
uv run python -c "
from pathlib import Path
from pipeline.transcript_detect import looks_like_transcript
for n in ['silver_transcript','silver_essay','silver_mailbag']:
    t = Path(f'pipeline/fixtures/{n}.txt').read_text()
    print(n, looks_like_transcript(t))
"
```

Expected: `True`, `False`, `False`. If the transcript fixture prints `False`,
8000 chars did not reach five turns for two speakers — raise the cut until it
does, and record the size you used.

**Step 2: Write the failing detector regression tests**

Append to `pipeline/test_transcript_detect.py`:

```python
from pathlib import Path

_FIX = Path(__file__).parent / "fixtures"


def test_real_silver_transcript_is_detected():
    """Real email-cleaned body from the 2026-08-17 Silver Bulletin post.

    Measured against the full 57-email Silver archive: 4 transcripts, 53
    essays, zero false positives. See
    docs/plans/2026-08-18-silver-transcript-report-design.md.
    """
    assert looks_like_transcript((_FIX / "silver_transcript.txt").read_text())


def test_real_silver_essay_is_not_detected():
    """Poll/ranking tables produce name-shaped labels that occur once each."""
    assert not looks_like_transcript((_FIX / "silver_essay.txt").read_text())


def test_real_silver_mailbag_is_not_detected():
    """SBSQ subscriber-question posts were the leading false-positive suspect.

    The speaker-turn regex does match a bare ``Q:``; this series does not
    label its answers, so only one label can recur and the two-speaker floor
    holds.
    """
    assert not looks_like_transcript((_FIX / "silver_mailbag.txt").read_text())
```

Run: `uv run pytest pipeline/test_transcript_detect.py -q`
Expected: PASS immediately (the detector is unchanged; these tests pin
externally-validated behavior rather than drive new code).

**Step 3: Add the Silver prompt**

Add to `pipeline/transcript_report.py`, modeled on the other two but with the
two Silver-specific facts from the design doc: the post opens with original
prose (4–11% of the body), and the conversation is not always Nate's — the
2026-04-29 post is two staff writers with no Nate Silver present.

```python
_SILVER_TEMPLATE = """\
You are writing a spoken briefing about a Silver Bulletin post by Nate
Silver's team that contains a recorded conversation. Your listener does
NOT want to hear the transcript read aloud — they want a clear,
structured report on what was said.

The post usually opens with several hundred words of the author's own
essay before the conversation begins. Cover that opening argument first,
in proportion to its length, then report the conversation itself.

Do not assume Nate Silver is one of the speakers; some conversations are
between other Silver Bulletin writers. Take the participants from the
transcript.

Subject line: {subject}

Below is the full post. Read it, then produce a 5–10 minute spoken
briefing (roughly 800–1500 words) covering:

- The argument or setup in the opening essay, if there is one.
- Who participated in the conversation (with affiliations if stated).
- What topics were discussed, in the order that best illuminates the
  conversation (not necessarily the order they appeared).
- The key claims, arguments, and evidence each participant offered,
  with attribution ("Silver argued...", "the guest pushed back,
  saying...").
- Any notable disagreements or points of tension.
- Concrete numbers, forecasts, and examples that gave the conversation
  weight. Silver Bulletin traffics in specific figures; keep them exact
  and do not round or invent them.

Write for the ear: plain spoken English, no markdown, no bullet
points, no headers. Use natural transitions. You are a smart friend
explaining what a conversation got into, not reading a summary out loud.
Do not editorialize beyond what the participants themselves said,
and do not invent facts.

POST:

{body}
"""

TRANSCRIPT_FEEDS: dict[str, str] = {
    "chinatalk": _CHINATALK_TEMPLATE,
    "yglesias": _YGLESIAS_TEMPLATE,
    "silver": _SILVER_TEMPLATE,
}
```

**Step 4: Run the whole transcript suite**

Run: `uv run pytest pipeline/test_transcript_report.py pipeline/test_transcript_detect.py -q`
Expected: all pass. The parametrized gate/prompt tests now cover `silver` for
free because they iterate `TRANSCRIPT_FEEDS`; confirm the test count rose.

**Step 5: Commit**

```bash
git add pipeline/transcript_report.py pipeline/fixtures/silver_*.txt \
        pipeline/test_transcript_detect.py
git commit -m "feat(silver): give Silver Bulletin transcripts the report treatment

Detector safety measured, not assumed: all 57 archived Silver emails replayed
through the real email path gave 4 transcripts, 53 essays, zero false
positives. Fixtures are email-cleaned bodies, including the SBSQ mailbag --
the nearest miss in the archive."
```

---

## Task 5: Wire the processor to the single gate

**Files:**
- Modify: `pipeline/processor.py:15,21,121-136`
- Create: `pipeline/test_transcript_wiring.py` (or add to `test_transcript_report.py`)

**Step 1: Write the failing wiring test**

```python
import inspect

from pipeline import processor


def test_processor_calls_maybe_rewrite_transcript():
    source = inspect.getsource(processor.process_email_bytes)
    assert "maybe_rewrite_transcript" in source
    assert "preset.feed_slug" in source


def test_processor_no_longer_calls_the_retired_gates():
    source = inspect.getsource(processor.process_email_bytes)
    assert "maybe_rewrite_chinatalk" not in source
    assert "maybe_rewrite_yglesias" not in source
```

Run it. Expected: the second test fails.

**Step 2: Edit `processor.py`**

Replace the two imports (lines 15 and 21) with:

```python
from pipeline.transcript_report import maybe_rewrite_transcript
```

Replace lines 121–136 with:

```python
    # Some newsletters ship the verbatim transcript of a podcast conversation
    # as the email body -- 60-80 minutes of TTS. For a confirmed transcript on
    # a registered feed, rewrite it into a spoken briefing (a "Report:").
    body, episode_title = maybe_rewrite_transcript(
        body=body,
        title=episode_title,
        feed_slug=preset.feed_slug,
        subject_raw=subject_raw,
    )
```

**Step 3: Run**

Run: `uv run pytest pipeline/test_transcript_wiring.py pipeline/test_processor*.py -q`
Expected: all pass.

**Step 4: Commit**

```bash
git add pipeline/processor.py pipeline/test_transcript_wiring.py
git commit -m "refactor(processor): one transcript gate instead of two"
```

---

## Task 6: Delete the retired modules

Do this only after Tasks 3–5 are green. The golden tests in Task 3 depend on
the old modules existing, so they must be removed in the same commit.

**Files:**
- Delete: `pipeline/chinatalk_writer.py`, `pipeline/chinatalk_report.py`,
  `pipeline/yglesias_writer.py`, `pipeline/yglesias_report.py`,
  `pipeline/yglesias_filter.py`
- Delete: `pipeline/test_chinatalk_writer.py`, `pipeline/test_chinatalk_report.py`,
  `pipeline/test_yglesias_writer.py`, `pipeline/test_yglesias_report.py`,
  `pipeline/test_yglesias_filter.py`
- Modify: `pipeline/test_transcript_report.py` (drop the two golden tests)

**Step 1: Confirm nothing else imports them**

```bash
rg -n "chinatalk_writer|chinatalk_report|yglesias_writer|yglesias_report|yglesias_filter" \
  --glob '!docs/**' .
```

Expected after Task 5: only the five modules, their five test files, the two
golden tests, a comment in `pipeline/report_writer.py`, and `AGENTS.md`.
Anything else is a missed caller — stop and handle it.

**Step 2: Preserve the detector's non-obvious cases**

`test_yglesias_filter.py` contains detector edge cases (e.g. the `Note:` /
`Update:` case proving repeated non-speaker labels do not fire). Move those
into `pipeline/test_transcript_detect.py` before deleting the file. Read it
line by line; do not assume `test_transcript_detect.py` already covers them.

**Step 3: Delete and drop the golden tests**

```bash
git rm pipeline/chinatalk_writer.py pipeline/chinatalk_report.py \
       pipeline/yglesias_writer.py pipeline/yglesias_report.py \
       pipeline/yglesias_filter.py \
       pipeline/test_chinatalk_writer.py pipeline/test_chinatalk_report.py \
       pipeline/test_yglesias_writer.py pipeline/test_yglesias_report.py \
       pipeline/test_yglesias_filter.py
```

Remove the two `*_is_byte_identical_to_the_retired_module` tests and their
`from pipeline import chinatalk_writer, yglesias_writer` import. They have
served their purpose: they proved equivalence at migration time and are
recorded in the commit history. Also fix the stale comment at
`pipeline/report_writer.py:83` referencing `chinatalk_writer`.

**Step 4: Full suite**

Run: `uv run pytest -q`
Expected: all pass, no import errors, no skipped-because-missing modules.

Also run the linter (`uv run ruff check pipeline`) — deleting modules often
leaves unused imports behind.

**Step 5: Commit**

```bash
git commit -am "refactor: retire the per-feed transcript modules"
```

---

## Task 7: Documentation and follow-up issues

**Files:**
- Modify: `AGENTS.md` (merge the two transcript sections; fix the stale claim)
- Modify: `pipeline/AGENTS.md` only if it references the retired modules

**Step 1: Merge the AGENTS.md sections**

Replace the two sections "ChinaTalk Transcript Report Path" and "Yglesias
Argument Transcript Reports" with one "Transcript Report Path" section
covering all three feeds. It must state:

- Which feeds are registered, and that registration is a one-line addition to
  `TRANSCRIPT_FEEDS` — a new feed needs a prompt constant and nothing else.
- Detection is `pipeline/transcript_detect.looks_like_transcript`, deterministic
  and content-only, with the 2026-05-26 Gemini-outage history that motivated it.
- **The failure mode is re-raise, not fall back.** The current Yglesias section
  says "fails safe: any exception in detection or generation falls back to the
  standard reading." That is false — the code re-raises so the email is
  redelivered. Do not carry that sentence forward.
- The Silver evidence: 57 emails replayed through the real email path, 4
  transcripts / 53 essays / zero false positives, and that the essay preamble
  is 4–11% of the body so one whole-post report is intentional.
- That prompts are per-feed constants specifically so an edit to one cannot
  reach another.
- Point at `docs/plans/2026-08-18-silver-transcript-report-design.md`.

Also add `pipeline/report_engine.py` to the "Core Paths" list as the single
home of the opencode-serve report mechanics.

**Step 2: Close and file beads**

```bash
bd close my-podcasts-ne0 --reason "Fixed in report_engine.extract_script: unclosed final block now competes on length, and the no-tag fallback strips <summary> and stray literal tags. The sibling writers the bead named (chinatalk_writer, yglesias_writer) were deleted in the same change, so there is now exactly one implementation."
```

Then file the known gaps:

```bash
bd create "Email path has no failure counter: a deterministic transcript-report failure redelivers forever" \
  -d "See docs/plans/2026-08-18-silver-transcript-report-design.md 'Known gaps'. Unlike daily jobs, the email queue path has no failure_count/backoff/errored state (pipeline/consumer.py ~448). A body that fails generation every time redelivers indefinitely at up to 900s per attempt, delaying every feed behind it. Queue max_retries/DLQ is Cloudflare dashboard-side and not visible in workers/email-ingest/wrangler.toml. Either verify that config or add an alert keyed on source_r2_key after N failures."

bd create "report_writer one-off path has no script-length floor" \
  -d "report_engine.run_report_prompt gained an opt-in min_chars floor (commit 39589e3's lesson: emptiness is the wrong test, a 3-char placeholder is not empty). transcript_report passes 500; report_writer passes nothing, so a one-off episode could still publish a placeholder. Lower risk because one-offs are operator-run, but decide deliberately."
```

**Step 3: Commit and push**

```bash
git add AGENTS.md
git commit -m "docs: one transcript report section covering three feeds"
git pull --rebase
bd dolt push
git push
git status   # MUST show up to date with origin
```

---

## Task 8: Backfill the four historical Silver transcripts

The four transcripts already shipped as literal reads (75, 60, 50, and ~40
minutes). The user wants reports for them in the feed. Do this **only after
Tasks 1–7 are merged and deployed**, so the backfill uses the same prompt the
automated path will use — it doubles as real-corpus QA of the Silver prompt.

Two facts corrected an earlier draft of this task; do not re-derive them wrong:

- **`publish_script` sets `pub_date` to *now*, unconditionally**
  (`pipeline/script_processor.py:216`). `date_str` shapes only the slug and
  r2_key (`:184-186`), and the feed orders by `created_at DESC`
  (`pipeline/db.py:311`). So the four reports land at the top of the feed as
  fresh unplayed items. Do **not** hand-UPDATE `pub_date` to the original
  dates — clients would bury them.
- **`python -m pipeline episode --script-file` will not work here.**
  `pipeline/__main__.py:977` resolves the source document *before* the
  `--script-file` branch at `:988`, and `substack.resolve_post` raises on
  paywalled posts (`pipeline/substack.py:76-82`). All four posts are
  paid-subscriber-only. Use the bare `publish-script` command
  (`pipeline/__main__.py:796`) instead. Cost: no `source_url` and no
  auto-generated show notes.

**Step 1: Generate the four scripts offline, writing nothing to the DB**

Pull the four raw emails from R2, run them through the real cleaning path, and
generate with the shipped code. Adapt the Appendix replay script; keep it
read-only against R2 and the state DB.

```python
# for each of the four dates, having found its key in processed_emails:
raw = r2.get_object_bytes(key)
parsed = EmailProcessor(raw).parse()
body = get_source_adapter("silver").clean_body(raw_email=raw, body=parsed["body"])
report = generate_report(
    body=body, subject=parsed.get("subject_raw", ""), feed_slug="silver"
)
Path(f"/tmp/opencode/silver-backfill/{parsed['date']}.txt").write_text(report.script)
```

Each generation takes minutes and costs money. Do them one at a time and stop
on the first failure rather than burning four runs on a broken prompt.

**Step 2: Review all four scripts before publishing anything**

Read them. This is the first real output of the Silver prompt. Check
specifically: does it cover the opening essay proportionately (4–11% of the
body), does it attribute claims to the right speaker, and — for the 2026-04-29
post — does it correctly report Eli McKown-Dawson and Nathaniel Rakich rather
than assuming Nate Silver is present? A bad script here means fixing the
prompt in `transcript_report.py` and regenerating, not publishing anyway.

**Step 3: Back up the state DB with the online backup API**

The consumer is a live writer; `cp` can copy a torn page.

```bash
sqlite3 /persist/my-podcasts/state.sqlite3 \
  ".backup '/persist/my-podcasts/state.sqlite3.bak-$(date +%Y%m%d-%H%M%S)'"
```

Do the whole task outside 04:00–05:30 ET (the daily timers) and not while a
Silver email is landing.

**Step 4: Publish the first one and verify before doing the rest**

```bash
uv run python -m pipeline publish-script \
  --script-file /tmp/opencode/silver-backfill/2026-08-17.txt \
  --title "Report: 2026-08-17 - Silver Bulletin - Why does everyone hate data centers?" \
  --feed-slug silver \
  --category News \
  --date 2026-08-17
```

**`--category News` is mandatory.** The channel-level `<itunes:category>` is
taken from the newest episode's category (`pipeline/feed.py:99`) and
`publish-script` defaults to `Technology`. Omitting it flips the whole Silver
Bulletin channel category.

Then verify before continuing:

```bash
curl -s https://podcast.mohrbacher.dev/feeds/silver.xml | head -40
curl -sI "$(curl -s https://podcast.mohrbacher.dev/feeds/silver.xml \
  | grep -o 'https://[^"]*\.mp3' | head -1)" | head -3
```

Check the enclosure length is a plausible episode (megabytes, not kilobytes —
see the 2636-byte incident), the channel category still reads News, and the
item appears in a podcast client. Only then publish the remaining three.

**Step 5: Delete the four literal-read episodes**

Do this only after all four reports are verified. There is no
`delete_episode` helper, so it is manual SQL — this is the same shape as the
`my-podcasts-78b` cleanup.

```bash
uv run python -c "
import sqlite3
c = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
c.row_factory = sqlite3.Row
rows = list(c.execute(
    \"SELECT id, title, r2_key, duration_seconds FROM episodes \"
    \"WHERE feed_slug='silver' AND title NOT LIKE 'Report:%' \"
    \"AND slug LIKE '2026-04-29%' OR ...\"))
for r in rows: print(dict(r))
"
```

Select the exact four by `id` after printing and eyeballing them — do not
delete by a `LIKE` pattern you have not first run as a `SELECT`. Then delete
those four ids, and run the authoritative regeneration last:

```bash
uv run python -m pipeline feed
```

That full rebuild from the DB also closes the only residual race here: the
consumer may regenerate feeds concurrently, and last-write-wins is
self-healing because every regeneration is a complete rebuild.

Deleting the four old mp3s from R2 is optional hygiene; orphaned objects are
harmless once the feed no longer references them.

**Step 6: Record what happened**

```bash
bd create "Backfilled four historical Silver Bulletin transcripts as reports" \
  --status closed \
  -d "2026-04-29, 2026-07-25, 2026-08-03, 2026-08-17 shipped as literal reads (up to 75 min) before the silver transcript path existed. Regenerated as reports with the shipped prompt, published via publish-script --category News, and the four literal-read rows deleted. First real-corpus output of the silver prompt; note any prompt weaknesses observed during review."
```

Note in that bead anything the review in Step 2 revealed about the prompt —
that is the only feedback signal this feature gets until the next Silver
transcript lands, roughly a month out.

---

## Verification before claiming done

Run all of these and read the output — do not infer:

```bash
uv run pytest -q                       # whole suite green
uv run ruff check pipeline             # no lint regressions
rg -n "chinatalk_writer|yglesias_filter" --glob '!docs/**' .   # only AGENTS.md history, if anything
uv run python -c "
from pipeline.transcript_report import TRANSCRIPT_FEEDS
print(sorted(TRANSCRIPT_FEEDS))"      # ['chinatalk', 'silver', 'yglesias']
```

Then confirm the deploy: the consumer runs the installed code, so restart it
and watch it come up clean.

```bash
sudo systemctl restart my-podcasts-consumer
sudo systemctl status my-podcasts-consumer --no-pager
journalctl -u my-podcasts-consumer --since "2 minutes ago" --no-pager
```

The real end-to-end proof only arrives with the next Silver transcript post
(roughly monthly — 4 in the last 5 months). When it lands, check that the
episode title carries the `Report: ` prefix and that the duration dropped from
~75 minutes to ~8.

---

## Appendix: the corpus replay script

Regenerate the fixtures or re-verify the detector against the whole archive.
Read-only against R2 and the state DB.

```python
"""Replay archived raw emails through the REAL email path + transcript detector."""

from __future__ import annotations

import email
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from email_processor.api import EmailProcessor
from pipeline.r2 import R2Client
from pipeline.source_adapters import get_source_adapter
from pipeline.transcript_detect import _SPEAKER_TURN_RE, looks_like_transcript

TARGET = sys.argv[1] if len(sys.argv) > 1 else "natesilver"
TARGET_SLUG = sys.argv[2] if len(sys.argv) > 2 else "silver"
OUT = Path("/tmp/opencode/replay-bodies")
OUT.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect("/persist/my-podcasts/state.sqlite3")
keys = [r[0] for r in conn.execute("SELECT r2_key FROM processed_emails")]
r2 = R2Client()

rows = []
for key in keys:
    try:
        raw = r2.get_object_bytes(key)
    except Exception as exc:  # noqa: BLE001
        print(f"MISS {key} {exc}")
        continue
    msg = email.message_from_bytes(raw)
    sender = (msg.get("From") or "") + " " + (msg.get("Sender") or "")
    if TARGET not in sender.lower():
        continue
    parsed = EmailProcessor(raw).parse()
    adapter = get_source_adapter(TARGET_SLUG)
    body = adapter.clean_body(raw_email=raw, body=parsed["body"])
    counts = Counter(m.group(1) for m in _SPEAKER_TURN_RE.finditer(body))
    hit = looks_like_transcript(body)
    subject = (parsed.get("subject_raw") or "").strip().replace("\n", " ")[:70]
    rows.append((parsed["date"], hit, len(body), subject, counts.most_common(4)))
    name = f"{parsed['date']}-{'T' if hit else 'F'}-{parsed['subject'][:40]}.txt"
    (OUT / name).write_text(body, encoding="utf-8")

rows.sort()
print(f"\n{len(rows)} {TARGET} emails replayed\n")
for date, hit, n, subject, top in rows:
    print(f"{date} {'TRANSCRIPT' if hit else 'essay     '} {n:>7} {subject}")
    if top:
        print(f"           labels: {top}")
```

Run it with R2 credentials:

```bash
R2_ACCOUNT_ID="$(sudo cat /run/secrets/r2_account_id)" \
R2_ACCESS_KEY_ID="$(sudo cat /run/secrets/r2_access_key_id)" \
R2_SECRET_ACCESS_KEY="$(sudo cat /run/secrets/r2_secret_access_key)" \
uv run python /tmp/opencode/silver_replay.py natesilver silver
```

Two keys in `processed_emails` are historical `local/*.eml` test entries that
no longer exist in R2; the `MISS` lines for them are expected.
