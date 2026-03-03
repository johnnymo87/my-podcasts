from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from openai import OpenAI


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
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1",
        )

        now = datetime.now(tz=UTC)
        from_date = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        to_date = now.strftime("%Y-%m-%d")

        response = client.responses.create(
            model="grok-3-fast",
            input=[
                {
                    "role": "user",
                    "content": (
                        f"Search X/Twitter for discussion about this news headline:\n"
                        f'"{headline}"\n\n'
                        f"Summarize the key points of what people are saying. "
                        f"Include any notable takes from journalists or experts. "
                        f"Be concise — 2-3 short paragraphs max."
                    ),
                }
            ],
            tools=[
                {
                    "type": "x_search",
                    "x_search": {
                        "from_date": from_date,
                        "to_date": to_date,
                    },
                },
            ],
        )

        text_parts = []
        for item in response.output:
            if item.type == "message":
                for c in item.content:
                    if c.type == "output_text":
                        text_parts.append(c.text)

        summary = "\n".join(text_parts).strip()
        if not summary:
            return None

        return XaiResult(summary=summary)
    except Exception:
        return None
