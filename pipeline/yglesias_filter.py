"""Detect and skip Slow Boring posts that are really podcast show notes.

Matt Yglesias's Slow Boring publishes a weekly podcast with Jerusalem Demsas
(for The Argument). The newsletter form of those episodes is the show notes
+ timestamps + paywalled transcript -- ~80 minutes of TTS that the user can
just listen to as a podcast directly. We detect those posts via deterministic
body markers and skip episode generation entirely.

The detector is conservative on two axes:

* It only runs for ``feed_slug == "yglesias"``. Same phrases appearing in any
  other feed are passed through.
* It requires at least two distinct podcast-boilerplate markers in the cleaned
  body. One marker can show up incidentally in a normal essay (e.g. an essay
  that quotes Spotify). Two markers from this set together is a very strong
  signal -- and we'd rather let one ~80-minute episode through than silently
  drop a real essay.

The wiring point in ``pipeline.processor.process_email_bytes`` mirrors the
ChinaTalk transcript hook: it runs after body cleaning, before TTS / R2 /
DB insert.
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


# Each of these strings is boilerplate that appears verbatim in Slow Boring
# Conversations posts (the Yglesias + Demsas Argument podcast). They were
# verified against the 2026-05-14 "What the Spirit debate is really about"
# email body fetched from R2. None of them appear in a normal Slow Boring
# essay.
_PODCAST_MARKERS: tuple[str, ...] = (
    "WATCH THE EPISODE HERE",
    "TheArgumentMag.com",
    "Subscribe: Apple Podcasts | Spotify | YouTube | Overcast | Pocket Casts",
    "Time stamps:",
    "New episodes post every Thursday.",
)


# Require this many marker hits before we treat the body as a podcast post.
# Two is enough to be near-zero false positives while staying robust to
# Substack tweaking any one boilerplate string in the future.
_MIN_MARKER_HITS: int = 2


def is_demsas_podcast(body: str) -> bool:
    """Return True if the cleaned body looks like a Slow Boring podcast post."""
    if not body:
        return False
    hits = sum(1 for marker in _PODCAST_MARKERS if marker in body)
    return hits >= _MIN_MARKER_HITS


def should_skip(*, body: str, feed_slug: str) -> bool:
    """Return True if this email should be dropped without producing an episode.

    Safe-by-default: any unexpected exception in the matcher falls back to
    ``False`` so we never silently drop a normal essay.
    """
    if feed_slug != "yglesias":
        return False
    try:
        return is_demsas_podcast(body)
    except Exception:
        logger.exception(
            "yglesias filter matcher crashed; falling back to no-skip"
        )
        return False
