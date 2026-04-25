from __future__ import annotations

import logging
import os

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are classifying a newsletter post. Decide whether it consists primarily
of a podcast transcript with multiple named speakers and verbatim dialogue
(YES) or whether it is an essay, article, or analysis (NO).

A transcript looks like alternating turns labeled with speaker names or
roles ("Jordan:", "Guest:", "Q:", "A:", etc.) and reads like a recorded
conversation. An essay reads like one author's prose, even if it contains
quoted excerpts.

Reply with exactly one token: YES or NO.
"""


def is_transcript(body: str, subject: str) -> bool:
    """Return True iff the chinatalk newsletter body is a podcast transcript.

    Conservative: any failure (missing API key, network error, ambiguous
    response) returns False, so callers fall back to the standard reading.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set; skipping chinatalk classification")
        return False

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=f"Subject: {subject}\n\n{body}",
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                temperature=0.0,
            ),
        )
        text = (response.text or "").strip().upper()
        return text == "YES"
    except Exception:
        logger.exception("chinatalk classifier failed; defaulting to NO")
        return False
