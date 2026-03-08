from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from pipeline.db import StateStore


def test_days_since_last_episode_with_episodes(tmp_path):
    """Returns correct days since last episode."""
    store = StateStore(tmp_path / "test.db")
    # Insert a fake episode 3 days ago
    three_days_ago = format_datetime(datetime.now(tz=UTC) - timedelta(days=3))
    store._conn.execute(
        "INSERT INTO episodes (id, title, slug, pub_date, r2_key, feed_slug, category, preset_name, size_bytes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ep1",
            "Test Episode",
            "test",
            three_days_ago,
            "key",
            "things-happen",
            "News",
            "General",
            1000,
        ),
    )
    store._conn.commit()

    result = store.days_since_last_episode("things-happen")
    assert result == 3


def test_days_since_last_episode_no_episodes(tmp_path):
    """Returns None when no episodes exist for feed."""
    store = StateStore(tmp_path / "test.db")
    result = store.days_since_last_episode("things-happen")
    assert result is None


def test_days_since_last_episode_different_feed(tmp_path):
    """Only considers episodes for the requested feed_slug."""
    store = StateStore(tmp_path / "test.db")
    yesterday = format_datetime(datetime.now(tz=UTC) - timedelta(days=1))
    store._conn.execute(
        "INSERT INTO episodes (id, title, slug, pub_date, r2_key, feed_slug, category, preset_name, size_bytes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ep1",
            "FP Episode",
            "fp",
            yesterday,
            "key",
            "fp-digest",
            "News",
            "General",
            1000,
        ),
    )
    store._conn.commit()

    # things-happen has no episodes
    assert store.days_since_last_episode("things-happen") is None
    # fp-digest has one from yesterday
    assert store.days_since_last_episode("fp-digest") == 1


def test_days_since_last_episode_unparseable_dates(tmp_path):
    """Returns None when all pub_dates are malformed."""
    store = StateStore(tmp_path / "test.db")
    store._conn.execute(
        "INSERT INTO episodes (id, title, slug, pub_date, r2_key, feed_slug, category, preset_name, size_bytes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ep1",
            "Bad Episode",
            "bad",
            "not-a-date",
            "key",
            "things-happen",
            "News",
            "General",
            1000,
        ),
    )
    store._conn.commit()

    result = store.days_since_last_episode("things-happen")
    assert result is None
