# Spoken Title Prelude Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Newsletter-derived episodes open their audio by speaking the episode title, with the date prefix stripped.

**Architecture:** One leaf module, `pipeline/title_prelude.py`, importing only `re` and nothing from `pipeline` (same discipline as `pipeline/article_resolver.py`, so any caller can use it without an import cycle). Four call sites prepend the title to the **TTS input string only**, immediately before it is written to the tempdir file handed to `ttsjoin`. Archived and published script artifacts are never modified, which is also what makes the change idempotent across retries.

**Tech Stack:** Python 3.12, `uv`, `pytest`. Tests are colocated in `pipeline/test_*.py`, not a separate `tests/` tree.

**Design doc:** `docs/plans/2026-08-23-tts-title-prelude-design.md` — read it first. It records why The Rundown and FP Digest are deliberately excluded, and why the Levine "already speaks its title" premise turned out to be false.

---

## Task 1: `spoken_title`

**Files:**
- Create: `pipeline/title_prelude.py`
- Test: `pipeline/test_title_prelude.py`

**Step 1: Write the failing tests**

```python
import pytest

from pipeline.title_prelude import spoken_title


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "2026-08-17 - Money Stuff - Bilateral OTC Goat Hedge",
            "Money Stuff: Bilateral OTC Goat Hedge",
        ),
        (
            "Report: 2026-08-19 - ChinaTalk - North Korean Messiah",
            "Report: ChinaTalk: North Korean Messiah",
        ),
        (
            "2026-08-11 - Slow Boring - Why does everyone hate data centers?",
            "Slow Boring: Why does everyone hate data centers?",
        ),
        # No date at all (one-off episode titles).
        ("Anthropic's LLM watermarking", "Anthropic's LLM watermarking"),
        # Date with no human part left over.
        ("2026-08-17 - The Rundown", "The Rundown"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_spoken_title(raw: str, expected: str) -> None:
    assert spoken_title(raw) == expected
```

**Step 2: Run to verify it fails**

Run: `uv run pytest pipeline/test_title_prelude.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'pipeline.title_prelude'`

**Step 3: Write the minimal implementation**

Create `pipeline/title_prelude.py`:

```python
"""Prepend a spoken episode title to TTS input.

Leaf module: imports nothing from ``pipeline``, so every publish path can use
it without an import cycle.
"""

import re

_ISO_DATE_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2}\s*-\s*")
_WHITESPACE = re.compile(r"\s+")


def spoken_title(episode_title: str) -> str:
    """Render an ``episode_title`` as something worth hearing aloud.

    Strips the ISO date wherever it appears -- not only at the start, because
    transcript reports are titled ``Report: 2026-08-19 - ChinaTalk - Foo``.
    Remaining ``' - '`` separators become ``': '``, which reads as a subtitle.
    """
    without_date = _ISO_DATE_PREFIX.sub("", episode_title)
    with_colons = without_date.replace(" - ", ": ")
    return _WHITESPACE.sub(" ", with_colons).strip()
```

**Step 4: Run to verify it passes**

Run: `uv run pytest pipeline/test_title_prelude.py -v`
Expected: 7 passed

**Step 5: Commit**

```bash
git add pipeline/title_prelude.py pipeline/test_title_prelude.py
git commit -m "feat: add spoken_title for rendering episode titles as audio"
```

---

## Task 2: `prepend_title`

**Files:**
- Modify: `pipeline/title_prelude.py`
- Test: `pipeline/test_title_prelude.py`

**Step 1: Write the failing tests**

Append to `pipeline/test_title_prelude.py`:

```python
from pipeline.title_prelude import prepend_title

# Real cleaned-body opening from an archived Levine email. The headline does
# not appear anywhere in the body, so dedupe must NOT fire here.
LEVINE_BODY = (
    "Money Stuff\n\n View in browser Subscribe to Bloomberg.com for "
    "unlimited access to all our coverage.\n\nProgramming note: this "
    "column will be off tomorrow.\n"
)


def test_prepends_when_body_does_not_state_title() -> None:
    result = prepend_title("2026-08-17 - Money Stuff - Goat Hedge", LEVINE_BODY)
    assert result.startswith("Money Stuff: Goat Hedge.\n\n")
    assert result.endswith(LEVINE_BODY)


def test_skips_when_body_already_opens_with_title() -> None:
    body = "Bilateral OTC Goat Hedge\n\nSome article text.\n"
    assert prepend_title("2026-08-17 - Bilateral OTC Goat Hedge", body) == body


def test_dedupe_ignores_case_and_punctuation() -> None:
    body = "money stuff -- goat hedge!\n\nSome article text.\n"
    assert prepend_title("2026-08-17 - Money Stuff - Goat Hedge", body) == body


def test_dedupe_only_looks_at_the_opening() -> None:
    """A title mentioned deep in the body is not an opening statement."""
    body = "Unrelated lede.\n\n" + ("filler. " * 60) + "Goat Hedge\n"
    result = prepend_title("2026-08-17 - Goat Hedge", body)
    assert result.startswith("Goat Hedge.\n\n")


@pytest.mark.parametrize(
    "title",
    [
        "2026-08-11 - Slow Boring - Why does everyone hate data centers?",
        "2026-08-11 - Slow Boring - Woke 1 is dead. We've learned nothing.",
        "2026-08-11 - Slow Boring - Stop!",
    ],
)
def test_no_double_terminator(title: str) -> None:
    result = prepend_title(title, "Body text.\n")
    first_line = result.split("\n", 1)[0]
    assert first_line[-2:] not in {"?.", "..", "!."}
    assert first_line[-1] in ".?!"


def test_empty_title_returns_body_unchanged() -> None:
    assert prepend_title("", "Body text.\n") == "Body text.\n"


def test_punctuation_only_title_returns_body_unchanged() -> None:
    """Normalizes to empty, so the guard must not emit a bare '.' prelude."""
    assert prepend_title("---", "Body text.\n") == "Body text.\n"
```

**Step 2: Run to verify it fails**

Run: `uv run pytest pipeline/test_title_prelude.py -v`
Expected: `ImportError: cannot import name 'prepend_title'`

**Step 3: Write the minimal implementation**

Append to `pipeline/title_prelude.py`:

```python
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# How much of the body counts as "the opening" for dedupe purposes.
_OPENING_CHARS = 300


def _normalize(text: str) -> str:
    return _NON_ALNUM.sub(" ", text.casefold()).strip()


def _already_states(spoken: str, body: str) -> bool:
    normalized_title = _normalize(spoken)
    if not normalized_title:
        return True
    return _normalize(body[:_OPENING_CHARS]).startswith(normalized_title)


def prepend_title(episode_title: str, body: str) -> str:
    """Return ``body`` with its spoken title prepended, or unchanged.

    Unchanged when the title is empty or the body already opens by stating it.
    The terminating period is required, not cosmetic: ``ttsjoin`` tokenizes
    with ``nltk.sent_tokenize`` and treats blank lines as nothing, so an
    unterminated title merges into the body's first sentence.
    """
    spoken = spoken_title(episode_title)
    if not spoken or _already_states(spoken, body):
        return body
    terminator = "" if spoken[-1] in ".?!" else "."
    return f"{spoken}{terminator}\n\n{body}"
```

**Step 4: Run to verify it passes**

Run: `uv run pytest pipeline/test_title_prelude.py -v`
Expected: all passed

**Step 5: Commit**

```bash
git add pipeline/title_prelude.py pipeline/test_title_prelude.py
git commit -m "feat: add prepend_title with opening-dedupe and terminator guard"
```

---

## Task 3: Wire the email path

Covers levine, silver, chinatalk, yglesias, general.

**Files:**
- Modify: `pipeline/processor.py` (import; new line before `input_txt.write_text` at `:134`)
- Test: `pipeline/test_processor_titles.py`

**Step 1: Write the failing test**

Read `pipeline/test_processor_titles.py` first and match its existing fixture
style for invoking `process_email_bytes` with `ttsjoin` and R2 stubbed out. Add:

```python
def test_tts_input_opens_with_episode_title(...):
    """The audio states the title; the DB row and artifacts are unaffected."""
    # Drive process_email_bytes with a Levine-shaped body whose headline is
    # absent from the text, capturing the file handed to ttsjoin.
    assert captured_tts_input.startswith("Money Stuff: ")
```

**Step 2: Run to verify it fails**

Run: `uv run pytest pipeline/test_processor_titles.py -v`
Expected: FAIL — TTS input starts with the body, not the title

**Step 3: Implement**

In `pipeline/processor.py`, add to the imports:

```python
from pipeline.title_prelude import prepend_title
```

and insert immediately before `input_txt.write_text(body, encoding="utf-8")`
(currently `:134`), inside the `with tempfile.TemporaryDirectory(...)` block:

```python
        # TTS input only -- never the archived body, so a retry cannot
        # prepend twice.
        body = prepend_title(episode_title, body)
```

Placement matters: it must sit **after** `maybe_rewrite_transcript` (`:123`), so
a transcript report speaks its final `Report: ` title rather than the
pre-rewrite one.

**Step 4: Run to verify it passes**

Run: `uv run pytest pipeline/test_processor_titles.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add pipeline/processor.py pipeline/test_processor_titles.py
git commit -m "feat: speak the episode title at the start of email-feed audio"
```

---

## Task 4: Wire the one-off / publish-script path

Covers `episode` (substack, arxiv, papers) and `publish-script`.

**Files:**
- Modify: `pipeline/script_processor.py` (import; new line after `:173`)
- Modify: `pipeline/__main__.py:850` (dry-run TTS reimplementation)
- Test: `pipeline/test_script_processor.py` (create if absent)

**Step 1: Write the failing test**

```python
def test_publish_script_tts_input_opens_with_title(...):
    # Capture the file handed to ttsjoin; assert it opens with the title and
    # that the script file on disk is untouched.
```

**Step 2: Run to verify it fails**

Run: `uv run pytest pipeline/test_script_processor.py -v`
Expected: FAIL

**Step 3: Implement**

In `pipeline/script_processor.py`, import `prepend_title` and insert directly
after `tts_text = strip_markdown_for_tts(raw_script)` (`:173`):

```python
    tts_text = prepend_title(title, tts_text)
```

Apply the identical line in the `publish-script --dry-run` branch of
`pipeline/__main__.py` (after its own `strip_markdown_for_tts` call, `:850`).
That branch is a second implementation of the same TTS step; leaving it behind
is how `my-podcasts-78b` happened.

**Step 4: Run to verify it passes**

Run: `uv run pytest pipeline/test_script_processor.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add pipeline/script_processor.py pipeline/__main__.py pipeline/test_script_processor.py
git commit -m "feat: speak the title in one-off episode and publish-script audio"
```

---

## Task 5: Wire the blog poller

Covers aaronson.

**Files:**
- Modify: `pipeline/blog_poller.py` (import; new line before `:151`)
- Test: `pipeline/test_blog_poller.py` (create if absent)

**Step 1: Write the failing test**

```python
def test_blog_tts_input_opens_with_post_title_not_the_dated_title(...):
    # post.title == "Anthropic's LLM watermarking"
    # episode_title  == "Aug 22 - Anthropic's LLM watermarking"
    assert captured_tts_input.startswith("Anthropic's LLM watermarking.")
    assert "Aug 22" not in captured_tts_input.split("\n", 1)[0]
```

**Step 2: Run to verify it fails**

Run: `uv run pytest pipeline/test_blog_poller.py -v`
Expected: FAIL

**Step 3: Implement**

In `pipeline/blog_poller.py`, import `prepend_title` and insert before
`input_txt.write_text(adapted_text, encoding="utf-8")` (`:151`):

```python
        # post.title, not episode_title: the latter is "Aug 22 - <title>",
        # whose date shape the ISO strip does not match.
        adapted_text = prepend_title(post.title, adapted_text)
```

Do **not** hoist the `parsed_pub_date` block from `:193-201`. Using `post.title`
makes the hoist unnecessary.

**Step 4: Run to verify it passes**

Run: `uv run pytest pipeline/test_blog_poller.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add pipeline/blog_poller.py pipeline/test_blog_poller.py
git commit -m "feat: speak the post title at the start of blog episode audio"
```

---

## Task 6: Full suite, docs, deploy

**Step 1: Run everything**

Run: `uv run pytest`
Expected: all pass, no regressions in `test_transcript_report.py`,
`test_dry_run_assembly.py`, `test_feed_show_notes.py`

**Step 2: Update `AGENTS.md`**

Add a short subsection recording that the per-article feeds prepend a spoken
title to the TTS input only, that The Rundown and FP Digest are deliberately
excluded, and that the blog path passes `post.title` rather than
`episode_title`. Point at the design doc.

**Step 3: Commit and deploy**

```bash
git add AGENTS.md
git commit -m "docs: record the spoken title prelude in AGENTS.md"
git pull --rebase && git push
sudo systemctl restart my-podcasts-consumer
sudo systemctl status my-podcasts-consumer --no-pager
```

**Step 4: Verify on real audio**

The next Levine or ChinaTalk episode should open with its title. Confirm by
listening, not by reading logs — the whole point is audible.

---

## Explicitly out of scope

- The Rundown and FP Digest (`things_happen_processor.py`, `fp_processor.py`). Their prompts already write a self-announcing opening.
- Stripping Levine's `View in browser / Subscribe to Bloomberg.com` boilerplate. File a bead; the prelude ships fine without it.
