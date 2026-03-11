from __future__ import annotations

import json
from xml.etree import ElementTree as ET

from pipeline.db import Episode, StateStore
from pipeline.feed import generate_feed_xml


def test_feed_includes_show_notes(tmp_path, monkeypatch) -> None:
    """Episodes with summary and articles_json produce description and content:encoded."""
    monkeypatch.setenv("PODCAST_BASE_URL", "https://podcast.test")
    store = StateStore(tmp_path / "test.sqlite3")

    articles = [
        {
            "title": "Oil's Wild Monday",
            "url": "https://example.com/oil",
            "theme": "Markets",
        },
        {"title": "AI Training", "url": "https://nymag.com/ai", "theme": "AI & Labor"},
        {"title": "No Link Story", "url": None, "theme": "AI & Labor"},
    ]
    episode = Episode(
        id="ep-show",
        title="2026-03-10 - The Rundown",
        slug="2026-03-10-the-rundown",
        pub_date="Mon, 10 Mar 2026 22:00:00 +0000",
        r2_key="episodes/the-rundown/2026-03-10-the-rundown.mp3",
        feed_slug="the-rundown",
        category="News",
        source_tag="the-rundown",
        preset_name="The Rundown",
        source_url=None,
        size_bytes=1000,
        duration_seconds=300,
        summary="Today we cover oil chaos and AI labor.",
        articles_json=json.dumps(articles),
    )
    store.insert_episode(episode)

    xml_bytes = generate_feed_xml(store, feed_slug="the-rundown")
    root = ET.fromstring(xml_bytes)

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    item = root.find(".//item")

    # description should be plain-text summary
    desc = item.find("description")
    assert desc is not None
    assert desc.text == "Today we cover oil chaos and AI labor."

    # content:encoded should have HTML with links
    encoded = item.find("content:encoded", ns)
    assert encoded is not None
    html = encoded.text
    assert "<p>Today we cover oil chaos and AI labor.</p>" in html
    assert 'href="https://example.com/oil"' in html
    assert "Oil's Wild Monday" in html or "Oil&#x27;s Wild Monday" in html
    assert 'href="https://nymag.com/ai"' in html
    assert "No Link Story" in html
    assert "Markets" in html

    store.close()


def test_feed_without_show_notes_unchanged(tmp_path, monkeypatch) -> None:
    """Episodes without show notes behave as before."""
    monkeypatch.setenv("PODCAST_BASE_URL", "https://podcast.test")
    store = StateStore(tmp_path / "test.sqlite3")

    episode = Episode(
        id="ep-no-sn",
        title="2026-03-10 - Levine",
        slug="2026-03-10-levine",
        pub_date="Mon, 10 Mar 2026 22:00:00 +0000",
        r2_key="episodes/levine/2026-03-10-levine.mp3",
        feed_slug="levine",
        category="News",
        source_tag=None,
        preset_name="General Newsletter",
        source_url="https://bloomberg.com/opinion/xyz",
        size_bytes=2000,
        duration_seconds=600,
    )
    store.insert_episode(episode)

    xml_bytes = generate_feed_xml(store, feed_slug="levine")
    root = ET.fromstring(xml_bytes)

    item = root.find(".//item")
    desc = item.find("description")
    assert desc is not None
    assert desc.text == "https://bloomberg.com/opinion/xyz"

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    encoded = item.find("content:encoded", ns)
    assert encoded is None

    store.close()
