"""Deterministic, content-only detection of podcast-transcript bodies.

A transcript is structurally a run of named speaker turns. We fire only when
at least two distinct speaker labels each recur at least five times as
line-starts. This survives newsletter footer/boilerplate changes and is
near-impossible to trigger on a normal essay, so it needs no LLM and no
network. Shared by the ChinaTalk and Yglesias report paths.
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
# Import-time guard: the regex is assembled from parts (_NAME/_APOS), so a
# sanity check catches construction errors immediately if those are edited.
assert _SPEAKER_TURN_RE.match("Jerusalem Demsas: hi"), "speaker-turn regex is broken"

# A genuine conversation has at least this many distinct speakers, each taking
# at least this many turns. A normal essay never has two different "Name:"
# labels each repeated five times at line starts.
_MIN_SPEAKERS = 2
_MIN_TURNS_PER_SPEAKER = 5


def looks_like_transcript(body: str) -> bool:
    """Return True if the body looks like a multi-speaker podcast transcript.

    Pure and deterministic. Feed gating lives in the report orchestrators.
    """
    if not body:
        return False
    counts = Counter(m.group(1) for m in _SPEAKER_TURN_RE.finditer(body))
    speakers = [label for label, n in counts.items() if n >= _MIN_TURNS_PER_SPEAKER]
    return len(speakers) >= _MIN_SPEAKERS
