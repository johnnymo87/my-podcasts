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


def test_dedupe_requires_whole_token_match() -> None:
    """"Better than gold" is not stated by "Better than golden retrievers"."""
    body = "Better than golden retrievers, honestly.\n\nText.\n"
    result = prepend_title("Better than gold", body)
    assert result.startswith("Better than gold.\n\n")


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
# Drops non-ASCII alphanumerics, unlike the article-file ``slugify`` family
# documented in AGENTS.md. A title with no ASCII alphanumerics at all
# normalizes to empty and the prelude is skipped -- a safe degradation.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# How much of the body counts as "the opening" for dedupe purposes.
_OPENING_CHARS = 300


def _normalize(text: str) -> str:
    return _NON_ALNUM.sub(" ", text.casefold()).strip()


def _already_states(spoken: str, body: str) -> bool:
    """Does ``body`` open by stating ``spoken``?

    Compares token lists rather than using ``startswith``, which matches a
    partial final token: the real title "Better than gold" would otherwise be
    suppressed by a body opening "Better than golden retrievers...".
    """
    title_tokens = _normalize(spoken).split()
    if not title_tokens:
        return True
    body_tokens = _normalize(body[:_OPENING_CHARS]).split()
    return body_tokens[: len(title_tokens)] == title_tokens


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

**Budget real time for this step — the harness does not exist.** No test in the
suite drives `process_email_bytes`. `pipeline/test_processor_titles.py` is 33
lines of pure `format_title` unit tests, and `pipeline/test_transcript_wiring.py`
only asserts against `inspect.getsource`. You are building the first integration
harness for this function, which means stubbing all of:

- raw multipart email bytes that `EmailProcessor(raw_email).parse()` accepts
- `StateStore` and the `R2Client`
- `subprocess.run` for both `ttsjoin` and `ffprobe` — this is where you capture
  the input file, by reading `input_txt` inside the fake before it is deleted
  with the tempdir
- the feed regeneration call
- `maybe_rewrite_transcript`, or a body that does not trip the detector

Resist the temptation to write another `inspect.getsource` test. A source-string
assertion would pass whether or not the prelude reaches the audio, which is the
only thing that matters here.

```python
def test_tts_input_opens_with_episode_title(...):
    """The audio states the title; the DB row and artifacts are unaffected."""
    # Body is Levine-shaped: opens with boilerplate, headline absent.
    assert captured_tts_input.startswith("Money Stuff: ")
    assert episode_row.title == "2026-08-17 - Money Stuff - Goat Hedge"
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
- Test: `pipeline/test_script_processor.py` — **exists, 486 lines. Append, do not
  create.** Its existing `test_publish_script_tts_receives_stripped_text`
  (`:301-348`) has been audited against this change and still passes; do not
  rewrite it.

**Step 1: Write the failing tests**

```python
def test_publish_script_tts_input_opens_with_title(...):
    # Capture the file handed to ttsjoin; assert it opens with the title and
    # that the script file on disk is untouched.


def test_publish_script_skips_prelude_for_daily_digests(...):
    # feed_slug="the-rundown", title="2026-08-21 - The Rundown"
    # TTS input must equal the stripped script, unprefixed.
```

**Step 2: Run to verify it fails**

Run: `uv run pytest pipeline/test_script_processor.py -v`
Expected: FAIL

**Step 3: Implement**

In `pipeline/script_processor.py`, import `prepend_title` and insert directly
after `tts_text = strip_markdown_for_tts(raw_script)` (`:173`):

```python
    # The daily digests' writer prompts already produce a self-announcing
    # opening, so a prelude would double it. Their automated processors bypass
    # publish_script entirely -- this guard is for the documented
    # consumer-down recovery, which publishes those scripts through here.
    if feed_slug not in {"the-rundown", "fp-digest"}:
        tts_text = prepend_title(title, tts_text)
```

Prepend **after** the markdown strip, not before: the strip's `*`-regexes then
never see the title, and dedupe compares against the same text TTS will read.

Apply the identical guarded block in the `publish-script --dry-run` branch of
`pipeline/__main__.py` (after its own `strip_markdown_for_tts` call, `:850`;
`title` and `feed_slug` are both in scope there, `title` at `:829`). That branch
is a second implementation of the same TTS step; leaving it behind is how
`my-podcasts-78b` happened.

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
- Test: `pipeline/test_blog_poller.py` — **exists, 235 lines. Append, do not
  create.** Its assertions are on DB rows, not TTS input; none break.

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

**Step 3: File the follow-up bead**

```bash
bd create "Strip Levine 'View in browser' boilerplate from TTS body" \
  -d "The title prelude makes the existing Bloomberg boilerplate more audible. Strip it in LevineAdapter.clean_body (pipeline/source_adapters.py). See docs/plans/2026-08-23-tts-title-prelude-design.md."
```

**Step 4: Commit and deploy**

```bash
git add AGENTS.md
git commit -m "docs: record the spoken title prelude in AGENTS.md"
git pull --rebase
bd dolt push
git push
sudo systemctl restart my-podcasts-consumer
sudo systemctl status my-podcasts-consumer --no-pager
```

The restart is what deploys the **email and blog** paths, which run inside the
consumer. The `episode` / `publish-script` CLI paths need no restart — they run
in your shell from the working tree.

**Step 5: Verify on real audio**

The next Levine or ChinaTalk episode should open with its title. Confirm by
listening, not by reading logs — the whole point is audible.

---

## Explicitly out of scope

- The Rundown and FP Digest (`things_happen_processor.py`, `fp_processor.py`). Their prompts already write a self-announcing opening. Task 4's `feed_slug` guard extends that exclusion to manual publishes of those feeds.
- Stripping Levine's `View in browser / Subscribe to Bloomberg.com` boilerplate — Task 6 Step 3 files it as a bead. The prelude ships fine without it.
- The mild self-statement redundancy in one-off report mode (see the design doc's "Accepted redundancy" note). Accepted, not fixed.
