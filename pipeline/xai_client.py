from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import xai_sdk
from xai_sdk.chat import user
from xai_sdk.tools import x_search

logger = logging.getLogger(__name__)

XAI_MODEL = "grok-4-1-fast-non-reasoning"


@dataclass(frozen=True)
class XaiResult:
    summary: str


def search_twitter(
    headline: str,
    *,
    lookback_days: int = 3,
) -> XaiResult | None:
    """Search Twitter/X via xAI Grok for discussion about a headline.

    Returns None if XAI_API_KEY is not set or on any error.
    """
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        return None

    try:
        now = datetime.now(tz=UTC)
        from_date = now - timedelta(days=lookback_days)

        client = xai_sdk.Client(api_key=api_key)
        try:
            chat = client.chat.create(
                model=XAI_MODEL,
                tools=[x_search(from_date=from_date, to_date=now)],
            )
            chat.append(
                user(
                    f"Search X/Twitter for discussion about this news headline:\n"
                    f'"{headline}"\n\n'
                    f"Summarize the key points of what people are saying. "
                    f"Include any notable takes from journalists or experts. "
                    f"Be concise — 2-3 short paragraphs max."
                )
            )

            response = chat.sample()
            summary = (response.content or "").strip()

            if not summary:
                return None
            return XaiResult(summary=summary)
        finally:
            client.close()

    except Exception:
        logger.exception("xAI search_twitter failed for: %s", headline)
        return None
