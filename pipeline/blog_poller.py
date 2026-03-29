from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"


@dataclass(frozen=True)
class BlogPost:
    title: str
    url: str
    pub_date: str
    html_content: str
    guid: str


_ADAPT_SYSTEM_PROMPT = """\
You are preparing a blog post for text-to-speech conversion.
Convert the HTML to clean spoken prose. Preserve ALL content
and the author's voice faithfully. Do not summarize or editorialize.

Rules:
- Remove images entirely, no references to them
- Verbalize math notation naturally (x squared, 2 to the n, etc.)
- Keep hyperlink text, drop URLs
- Introduce blockquotes with attribution where available
- Convert horizontal rules to paragraph breaks
- Output plain text, no markdown formatting
"""


def parse_blog_feed(rss_xml: str) -> list[BlogPost]:
    """Parse RSS XML and return blog posts with content."""
    root = ET.fromstring(rss_xml)
    posts: list[BlogPost] = []

    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pub_date_el = item.find("pubDate")
        content_el = item.find(f"{{{_CONTENT_NS}}}encoded")
        guid_el = item.find("guid")

        if title_el is None or link_el is None or pub_date_el is None:
            continue
        if content_el is None or not content_el.text:
            continue

        posts.append(
            BlogPost(
                title=title_el.text or "",
                url=link_el.text or "",
                pub_date=pub_date_el.text or "",
                html_content=content_el.text.strip(),
                guid=guid_el.text
                if guid_el is not None and guid_el.text
                else link_el.text or "",
            )
        )

    return posts


def adapt_for_audio(html_content: str, title: str) -> str | None:
    """Adapt HTML blog post content for TTS using Gemini Flash.

    Returns adapted text, or None if GEMINI_API_KEY is not set.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set, skipping audio adaptation")
        return None

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite-preview",
        contents=f"Blog post title: {title}\n\n{html_content}",
        config=types.GenerateContentConfig(
            system_instruction=_ADAPT_SYSTEM_PROMPT,
            temperature=0.3,
        ),
    )
    return response.text
