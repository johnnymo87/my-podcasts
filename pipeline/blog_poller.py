from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING

from google import genai
from google.genai import types

from pipeline.feed import regenerate_and_upload_feed
from pipeline.script_processor import TTS_MODEL

if TYPE_CHECKING:
    from pipeline.blog_sources import BlogSource
    from pipeline.db import StateStore
    from pipeline.r2 import R2Client

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


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        slug = "untitled"
    return slug[:80]


def process_blog_post(
    post: BlogPost,
    source: BlogSource,
    store: StateStore,
    r2_client: R2Client,
) -> None:
    """Process a single blog post: adapt, TTS, publish."""
    from pipeline.db import Episode

    if store.is_blog_post_processed(post.url):
        logger.info("Already processed: %s", post.url)
        return

    logger.info("Processing blog post: %s", post.title)

    # AI adaptation
    adapted_text = adapt_for_audio(post.html_content, post.title)
    if adapted_text is None:
        logger.error("AI adaptation failed for: %s", post.title)
        return

    # TTS
    date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    title_slug = _slugify(post.title)
    episode_slug = f"{date_str}-{title_slug}"
    episode_r2_key = f"episodes/{source.feed_slug}/{episode_slug}.mp3"

    with tempfile.TemporaryDirectory(prefix="blog-post-") as tmp_dir:
        tmp = Path(tmp_dir)
        input_txt = tmp / f"{episode_slug}.txt"
        output_mp3 = tmp / f"{episode_slug}.mp3"
        input_txt.write_text(adapted_text, encoding="utf-8")

        cmd = [
            "ttsjoin",
            "--input-file",
            str(input_txt),
            "--output-file",
            str(output_mp3),
            "--model",
            TTS_MODEL,
            "--voice",
            source.tts_voice,
        ]
        subprocess.run(cmd, check=True)

        r2_client.upload_file(output_mp3, episode_r2_key, content_type="audio/mpeg")
        size_bytes = output_mp3.stat().st_size

        # Parse duration
        duration_seconds = None
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(output_mp3),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            try:
                duration_seconds = int(round(float(probe.stdout.strip())))
            except ValueError:
                pass

    episode = Episode(
        id=str(uuid.uuid4()),
        title=post.title,
        slug=episode_slug,
        pub_date=format_datetime(datetime.now(tz=UTC)),
        r2_key=episode_r2_key,
        feed_slug=source.feed_slug,
        category=source.category,
        source_tag=None,
        preset_name=source.name,
        source_url=post.url,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        summary=None,
        show_notes_html=None,
    )
    store.insert_episode(episode)
    store.mark_blog_post_processed(post.url, source.feed_slug)
    regenerate_and_upload_feed(store, r2_client)
    logger.info("Published blog episode: %s", post.title)
