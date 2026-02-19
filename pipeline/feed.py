from __future__ import annotations

import os
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET


if TYPE_CHECKING:
    from pathlib import Path

    from pipeline.db import StateStore
    from pipeline.r2 import R2Client


def _duration_to_hms(seconds: int | None) -> str:
    if seconds is None:
        return "00:00"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    rem_seconds = seconds % 60
    if hours > 0:
        return f"{hours:02}:{minutes:02}:{rem_seconds:02}"
    return f"{minutes:02}:{rem_seconds:02}"


def generate_feed_xml(store: StateStore, feed_slug: str | None = None) -> bytes:
    base_url = os.getenv("PODCAST_BASE_URL", "https://podcast.mohrbacher.dev")
    base_title = os.getenv("PODCAST_TITLE", "My Podcasts")
    description = os.getenv(
        "PODCAST_DESCRIPTION",
        "Automated audio versions of selected email newsletters.",
    )
    language = os.getenv("PODCAST_LANGUAGE", "en-us")
    author = os.getenv("PODCAST_AUTHOR", "Jonathan Mohrbacher")
    image_url = os.getenv("PODCAST_IMAGE_URL", f"{base_url}/cover-general.jpg")
    default_category = os.getenv("PODCAST_DEFAULT_CATEGORY", "News")

    if feed_slug and feed_slug != "general":
        title = f"{base_title} - {feed_slug.title()}"
        image_env_key = f"PODCAST_IMAGE_URL_{feed_slug.upper().replace('-', '_')}"
        image_url = os.getenv(image_env_key, f"{base_url}/cover-{feed_slug}.jpg")
    else:
        title = base_title

    episodes = store.list_episodes(feed_slug=feed_slug)
    category = episodes[0].category if episodes else default_category

    rss = ET.Element(
        "rss",
        attrib={
            "version": "2.0",
            "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
        },
    )
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = base_url
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "language").text = language
    ET.SubElement(channel, "itunes:author").text = author
    ET.SubElement(channel, "itunes:image", href=image_url)
    ET.SubElement(channel, "itunes:category", text=category)

    for episode in episodes:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = episode.title
        episode_url = f"{base_url}/{episode.r2_key}"
        if episode.source_url:
            ET.SubElement(item, "link").text = episode.source_url
            ET.SubElement(item, "description").text = episode.source_url
        ET.SubElement(
            item,
            "enclosure",
            url=episode_url,
            length=str(episode.size_bytes),
            type="audio/mpeg",
        )
        ET.SubElement(item, "guid", isPermaLink="false").text = episode.id
        ET.SubElement(item, "pubDate").text = episode.pub_date
        ET.SubElement(item, "category").text = episode.category
        ET.SubElement(item, "itunes:category", text=episode.category)
        ET.SubElement(item, "itunes:duration").text = _duration_to_hms(
            episode.duration_seconds
        )

    xml_bytes = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    return xml_bytes


def regenerate_and_upload_feed(
    store: StateStore,
    r2_client: R2Client,
    output_file: Path | None = None,
) -> None:
    feed_xml = generate_feed_xml(store)
    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_bytes(feed_xml)
    r2_client.put_object_bytes("feed.xml", feed_xml, content_type="application/rss+xml")

    for feed_slug in store.list_feed_slugs():
        if feed_slug == "general":
            continue
        slug_feed_xml = generate_feed_xml(store, feed_slug=feed_slug)
        feed_key = f"feeds/{feed_slug}.xml"
        r2_client.put_object_bytes(
            feed_key,
            slug_feed_xml,
            content_type="application/rss+xml",
        )
