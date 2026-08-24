"""Prepend a spoken episode title to TTS input.

Leaf module: imports nothing from ``pipeline``, so every publish path can use
it without an import cycle.
"""

from __future__ import annotations

import re


_ISO_DATE_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2}\s*-\s*")
_WHITESPACE = re.compile(r"\s+")

# Drops non-ASCII alphanumerics, unlike the article-file ``slugify`` family
# documented in AGENTS.md. A title with no ASCII alphanumerics at all
# normalizes to empty and the prelude is skipped -- a safe degradation.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# How much of the body counts as "the opening" for dedupe purposes.
_OPENING_CHARS = 300


def spoken_title(episode_title: str) -> str:
    """Render an ``episode_title`` as something worth hearing aloud.

    Strips the ISO date wherever it appears -- not only at the start, because
    transcript reports are titled ``Report: 2026-08-19 - ChinaTalk - Foo``.
    Remaining ``' - '`` separators become ``': '``, which reads as a subtitle.
    """
    without_date = _ISO_DATE_PREFIX.sub("", episode_title)
    with_colons = without_date.replace(" - ", ": ")
    return _WHITESPACE.sub(" ", with_colons).strip()


def _normalize(text: str) -> str:
    return _NON_ALNUM.sub(" ", text.casefold()).strip()


def _already_states(spoken: str, body: str) -> bool:
    """Does ``body`` open by stating ``spoken``?

    Compares token lists rather than using ``startswith``, which matches a
    partial final token: the real title "Better than gold" would otherwise be
    suppressed by a body opening "Better than golden retrievers...".
    """
    # ``spoken`` is not truncated to _OPENING_CHARS -- a title longer than
    # that can never dedupe. Deliberate: it degrades safely (prelude is
    # added anyway) rather than risking a false match on a truncated title.
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
