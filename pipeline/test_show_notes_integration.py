from __future__ import annotations

import json
from xml.etree import ElementTree as ET

from pipeline.db import Episode, StateStore
from pipeline.feed import generate_feed_xml
from pipeline.show_notes import extract_show_notes_articles


def test_show_notes_end_to_end(tmp_path, monkeypatch) -> None:
    """Full flow: extract articles from work dir, store in DB, render in feed."""
    monkeypatch.setenv("PODCAST_BASE_URL", "https://podcast.test")

    # 1. Create work dir with plan and articles
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    plan = {
        "themes": ["Markets", "Tech"],
        "directives": [
            {
                "headline": "Oil Crash",
                "source": "levine",
                "priority": 1,
                "theme": "Markets",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "is_ai": False,
                "ai_query": "",
                "include_in_episode": True,
            },
            {
                "headline": "New Chip Design",
                "source": "semafor",
                "priority": 1,
                "theme": "Tech",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "is_ai": False,
                "ai_query": "",
                "include_in_episode": True,
            },
        ],
    }
    (work_dir / "plan.json").write_text(json.dumps(plan))

    articles_dir = work_dir / "articles"
    articles_dir.mkdir()
    (articles_dir / "00-oil-crash.md").write_text(
        "# Oil Crash\n\nURL: https://bloomberg.com/oil\n\nOil text."
    )
    semafor_dir = articles_dir / "semafor"
    semafor_dir.mkdir()
    (semafor_dir / "new-chip-design.md").write_text(
        "# New Chip Design\n\nURL: https://semafor.com/chip\n\nChip text."
    )

    # 2. Extract articles
    articles = extract_show_notes_articles(work_dir)
    assert len(articles) == 2

    # 3. Store in DB
    store = StateStore(tmp_path / "test.sqlite3")
    episode = Episode(
        id="ep-int",
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
        summary="Markets and tech stories today.",
        articles_json=json.dumps(articles),
    )
    store.insert_episode(episode)

    # 4. Generate feed and verify
    xml_bytes = generate_feed_xml(store, feed_slug="the-rundown")
    root = ET.fromstring(xml_bytes)

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    item = root.find(".//item")
    html = item.find("content:encoded", ns).text

    assert "Markets" in html
    assert "Tech" in html
    assert "https://bloomberg.com/oil" in html
    assert "https://semafor.com/chip" in html
    assert item.find("description").text == "Markets and tech stories today."

    store.close()
