from __future__ import annotations

import logging

from pipeline.chinatalk_classifier import is_transcript
from pipeline.chinatalk_writer import generate_report


logger = logging.getLogger(__name__)


def maybe_rewrite_chinatalk(
    *,
    body: str,
    title: str,
    feed_slug: str,
    subject_raw: str,
) -> tuple[str, str]:
    """Rewrite a chinatalk transcript into a spoken briefing if applicable.

    Returns ``(body, title)``. If the feed is not chinatalk, or the body
    is not a transcript, or any step fails, returns the inputs unchanged
    so the caller falls back to a standard reading.
    """
    if feed_slug != "chinatalk":
        return body, title

    try:
        if not is_transcript(body, subject_raw):
            return body, title
        report = generate_report(body=body, subject=subject_raw)
        return report.script, f"Report: {title}"
    except Exception:
        logger.exception(
            "chinatalk report path failed; falling back to standard reading"
        )
        return body, title
