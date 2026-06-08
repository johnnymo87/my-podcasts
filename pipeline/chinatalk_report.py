from __future__ import annotations

import logging

from pipeline.chinatalk_writer import generate_report
from pipeline.transcript_detect import looks_like_transcript


logger = logging.getLogger(__name__)


def maybe_rewrite_chinatalk(
    *,
    body: str,
    title: str,
    feed_slug: str,
    subject_raw: str,
) -> tuple[str, str]:
    """Rewrite a chinatalk transcript into a spoken briefing if applicable.

    Returns ``(body, title)``. If the feed is not chinatalk or the body is not
    a transcript, returns the inputs unchanged so the caller falls back to a
    standard reading.

    Detection is deterministic (``looks_like_transcript``), so it never depends
    on a remote API. For a *confirmed* transcript whose briefing cannot be
    generated, the generation exception is logged and re-raised rather than
    silently degrading to a literal read of the full transcript: this bubbles
    out of ``process_email_bytes`` so the consumer leaves the queue message
    unacked and the email is reprocessed on redelivery.
    """
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
