from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"


@dataclass(frozen=True)
class BlogPost:
    title: str
    url: str
    pub_date: str
    html_content: str
    guid: str


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
