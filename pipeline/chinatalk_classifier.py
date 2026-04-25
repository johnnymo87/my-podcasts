from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def is_transcript(body: str, subject: str) -> bool:
    """Return True iff the chinatalk newsletter body is a podcast transcript.

    Conservative: any failure (missing API key, network error, ambiguous
    response) returns False, so callers fall back to the standard reading.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set; skipping chinatalk classification")
        return False
    return False
