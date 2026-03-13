from __future__ import annotations

from xml.etree import ElementTree as ET

from pipeline.db import Episode, StateStore
from pipeline.feed import generate_feed_xml


def test_episode_show_notes_html_stored_and_retrieved(tmp_path) -> None:
    """Episodes with show_notes_html can be stored and retrieved."""
    store = StateStore(tmp_path / "test.sqlite3")

    episode = Episode(
        id="ep-html",
        title="Test Episode",
        slug="test-episode",
        pub_date="Thu, 13 Mar 2026 12:00:00 +0000",
        r2_key="episodes/deep-dives/test-episode.mp3",
        feed_slug="deep-dives",
        category="Technology",
        source_tag=None,
        preset_name="Script",
        source_url=None,
        size_bytes=5000,
        duration_seconds=120,
        summary="A test summary.",
        show_notes_html="<h2>Show Notes</h2><p>Details here.</p>",
    )
    store.insert_episode(episode)

    episodes = store.list_episodes(feed_slug="deep-dives")
    assert len(episodes) == 1
    assert episodes[0].show_notes_html == "<h2>Show Notes</h2><p>Details here.</p>"
    assert episodes[0].summary == "A test summary."

    store.close()


def test_feed_uses_show_notes_html_when_set(tmp_path, monkeypatch) -> None:
    """Episodes with show_notes_html use it for content:encoded instead of articles_json."""
    monkeypatch.setenv("PODCAST_BASE_URL", "https://podcast.test")
    store = StateStore(tmp_path / "test.sqlite3")

    episode = Episode(
        id="ep-rich",
        title="Deep Dive Episode",
        slug="deep-dive-episode",
        pub_date="Thu, 13 Mar 2026 12:00:00 +0000",
        r2_key="episodes/deep-dives/deep-dive-episode.mp3",
        feed_slug="deep-dives",
        category="Technology",
        source_tag=None,
        preset_name="Script",
        source_url=None,
        size_bytes=5000,
        duration_seconds=120,
        summary="A summary for description tag.",
        show_notes_html="<h2>Full Notes</h2><p>Rich content.</p>",
    )
    store.insert_episode(episode)

    xml_bytes = generate_feed_xml(store, feed_slug="deep-dives")
    root = ET.fromstring(xml_bytes)

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    item = root.find(".//item")

    desc = item.find("description")
    assert desc is not None
    assert desc.text == "A summary for description tag."

    encoded = item.find("content:encoded", ns)
    assert encoded is not None
    assert "<h2>Full Notes</h2>" in encoded.text
    assert "<p>Rich content.</p>" in encoded.text

    store.close()
