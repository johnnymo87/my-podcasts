from __future__ import annotations

import logging

from pipeline.yglesias_filter import is_argument_transcript
from pipeline.yglesias_writer import generate_report


logger = logging.getLogger(__name__)


def maybe_rewrite_yglesias(
    *,
    body: str,
    title: str,
    feed_slug: str,
    subject_raw: str,
) -> tuple[str, str]:
    """Rewrite an Argument transcript into a spoken briefing if applicable.

    Returns ``(body, title)``. If the feed is not yglesias or the body is not
    a transcript, returns the inputs unchanged so the caller falls back to a
    standard reading.

    Detection is deterministic (``is_argument_transcript``). For a *confirmed*
    transcript whose briefing cannot be generated, the generation exception is
    logged and re-raised rather than silently degrading to a literal read of
    the full ~80-minute transcript: this bubbles out of ``process_email_bytes``
    so the consumer leaves the queue message unacked and the email is
    reprocessed on redelivery.
    """
    if feed_slug != "yglesias":
        return body, title
    if not is_argument_transcript(body):
        return body, title
    try:
        report = generate_report(body=body, subject=subject_raw)
    except Exception:
        logger.exception(
            "yglesias report generation failed for a confirmed transcript; "
            "not shipping a literal read"
        )
        raise
    return report.script, f"Report: {title}"
