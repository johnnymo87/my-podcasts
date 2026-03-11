from __future__ import annotations

import json

from pipeline.db import Episode, StateStore


def test_episode_with_show_notes_round_trip(tmp_path) -> None:
    """Episodes with summary and articles_json survive insert + list."""
    store = StateStore(tmp_path / "test.sqlite3")

    articles = [
        {
            "title": "Oil's Wild Monday",
            "url": "https://example.com/oil",
            "theme": "Markets",
        },
        {
            "title": "AI Training Workers",
            "url": "https://nymag.com/ai",
            "theme": "AI & Labor",
        },
    ]
    episode = Episode(
        id="ep-1",
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
        summary="Today we cover oil market chaos and the AI training labor story.",
        articles_json=json.dumps(articles),
    )
    store.insert_episode(episode)

    episodes = store.list_episodes(feed_slug="the-rundown")
    assert len(episodes) == 1
    got = episodes[0]
    assert got.summary == episode.summary
    assert json.loads(got.articles_json) == articles

    store.close()


def test_episode_without_show_notes_defaults_to_none(tmp_path) -> None:
    """Episodes without summary/articles_json get None (backward compat)."""
    store = StateStore(tmp_path / "test.sqlite3")

    episode = Episode(
        id="ep-2",
        title="2026-03-10 - Levine",
        slug="2026-03-10-levine",
        pub_date="Mon, 10 Mar 2026 22:00:00 +0000",
        r2_key="episodes/levine/2026-03-10-levine.mp3",
        feed_slug="levine",
        category="News",
        source_tag=None,
        preset_name="General Newsletter",
        source_url="https://bloomberg.com/opinion/...",
        size_bytes=2000,
        duration_seconds=600,
    )
    store.insert_episode(episode)

    episodes = store.list_episodes(feed_slug="levine")
    assert len(episodes) == 1
    assert episodes[0].summary is None
    assert episodes[0].articles_json is None

    store.close()


def test_migration_adds_columns_to_existing_db(tmp_path) -> None:
    """Opening StateStore on an old DB adds the new columns."""
    import sqlite3

    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE episodes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            slug TEXT NOT NULL,
            pub_date TEXT NOT NULL,
            r2_key TEXT NOT NULL,
            feed_slug TEXT NOT NULL DEFAULT 'general',
            category TEXT NOT NULL DEFAULT 'News',
            source_tag TEXT,
            preset_name TEXT NOT NULL DEFAULT 'General Newsletter',
            source_url TEXT,
            size_bytes INTEGER NOT NULL,
            duration_seconds INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE processed_emails (
            r2_key TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE pending_things_happen (
            id TEXT PRIMARY KEY,
            email_r2_key TEXT NOT NULL,
            date_str TEXT NOT NULL,
            links_json TEXT NOT NULL,
            process_after TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE pending_fp_digest (
            id TEXT PRIMARY KEY,
            date_str TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            process_after TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE pending_the_rundown (
            id TEXT PRIMARY KEY,
            date_str TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            process_after TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.close()

    # Opening StateStore should add the new columns via migration
    store = StateStore(db_path)
    cols = {
        row[1] for row in store._conn.execute("PRAGMA table_info(episodes)").fetchall()
    }
    assert "summary" in cols
    assert "articles_json" in cols
    store.close()
