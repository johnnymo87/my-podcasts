"""Detect Slow Boring posts that are *The Argument* podcast transcripts.

Matt Yglesias's Slow Boring publishes the newsletter form of *The Argument*,
a debate/conversation podcast hosted by Jerusalem Demsas (regular weekly
episodes with Matt, plus one-off live events with guests). For paying
subscribers the email body carries the full verbatim transcript -- ~80
minutes of TTS. Rather than read that aloud (or drop it, as we used to), we
report on it: see ``pipeline.yglesias_report``.

Detection is deterministic and content-only. The shared implementation lives
in ``pipeline.transcript_detect.looks_like_transcript``; this module keeps the
``is_argument_transcript`` name as the yglesias-facing entry point. The wiring
point in ``pipeline.processor.process_email_bytes`` mirrors the ChinaTalk
transcript hook: it runs after body cleaning, before TTS.
"""

from __future__ import annotations

from pipeline.transcript_detect import looks_like_transcript


def is_argument_transcript(body: str) -> bool:
    """Return True if the body looks like a multi-speaker podcast transcript.

    Thin delegator to ``pipeline.transcript_detect.looks_like_transcript``.
    Feed gating (yglesias-only) happens in
    ``pipeline.yglesias_report.maybe_rewrite_yglesias``.
    """
    return looks_like_transcript(body)
