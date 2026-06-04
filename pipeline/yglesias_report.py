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

    Returns ``(body, title)``. If the feed is not yglesias, or the body
    is not a transcript, or any step fails, returns the inputs unchanged
    so the caller falls back to a standard reading.
    """
    if feed_slug != "yglesias":
        return body, title

    try:
        if not is_argument_transcript(body):
            return body, title
        report = generate_report(body=body, subject=subject_raw)
        return report.script, f"Report: {title}"
    except Exception:
        logger.exception(
            "yglesias report path failed; falling back to standard reading"
        )
        return body, title
