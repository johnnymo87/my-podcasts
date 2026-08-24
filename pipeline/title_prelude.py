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
