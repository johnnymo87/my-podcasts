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
# capitalized words (allowing internal '.', '-', and a straight or
# typographic apostrophe) followed by a colon and whitespace. Matches
# "Jerusalem Demsas:", "Kelsey Piper:", "Announcer:", "O'Brien:", "Q:".
# Timestamp lines like "0:00-..." start with a digit and never match.
#
# The apostrophe chars are injected via an escape (not a literal curly
# character) so the source survives editor/copy-paste normalization.
_APOS = "'" + "\u2019"  # straight (U+0027) + typographic (U+2019)
_NAME = r"[A-Z][\w." + _APOS + r"\-]*"
_SPEAKER_TURN_RE = re.compile(
    rf"^[ \t]*({_NAME}(?:[ \t]+{_NAME}){{0,3}}):\s",
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


# ---------------------------------------------------------------------------
# Backward-compatibility shim — to be removed when processor.py is rewired
# in a later task to call ``maybe_rewrite_yglesias`` instead.
# ---------------------------------------------------------------------------

def should_skip(*, body: str, feed_slug: str) -> bool:  # noqa: ARG001
    """Deprecated stub kept only so processor.py can import without error.

    The processor wiring task will replace this call site with
    ``maybe_rewrite_yglesias``; at that point this function is deleted.
    """
    return False
