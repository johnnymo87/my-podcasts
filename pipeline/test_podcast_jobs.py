from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline.db import StateStore
from pipeline.podcast_feed import PodcastItem
from pipeline.podcast_jobs import (
    PODCAST_WORK_ROOT,
    PodcastIdentity,
    admit_item,
    item_from_row,
    podcast_identity,
    work_dir_for,
)
from pipeline.podcast_report import item_metadata
from pipeline.podcast_sources import get_podcast_source


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
SOURCE = get_podcast_source("argument")


def _item(**overrides) -> PodcastItem:
    kwargs = {
        "guid": "audioboom-1",
        "title": "Canceling (and un-canceling) historic heroes",
        "link": "https://audioboom.com/posts/1",
        "pub_date": datetime(2026, 10, 15, 7, 0, tzinfo=UTC),
        "duration_s": 4807,
        "enclosure_url": "https://pscrb.fm/x.mp3?a=1&b=2",
        "description": "desc",
    }
    kwargs.update(overrides)
    return PodcastItem(**kwargs)


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "test.db")
    yield s
    s.close()


def test_display_title_format_and_identity():
    ident = podcast_identity(SOURCE, _item())
    assert ident.display_title == (
        "Report: 2026-10-15 - The Argument - "
        "Canceling (and un-canceling) historic heroes"
    )
    assert ident.date_str == "2026-10-15"
    assert ident.slug == (
        "2026-10-15-report-2026-10-15-the-argument-"
        "canceling-and-un-canceling-historic-heroes"
    )
    assert ident.r2_key == f"episodes/yglesias/{ident.slug}.mp3"
    assert isinstance(ident, PodcastIdentity)


def test_date_is_the_utc_date_of_the_pub_date():
    # 23:30 at UTC-5 on Oct 15 is 04:30 UTC on Oct 16.
    local = datetime(2026, 10, 15, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    ident = podcast_identity(SOURCE, _item(pub_date=local))
    assert ident.date_str == "2026-10-16"
    assert ident.display_title.startswith("Report: 2026-10-16 - ")


def test_work_dir_for_hashes_the_guid():
    digest = hashlib.sha256(b"audioboom-1").hexdigest()[:16]
    assert work_dir_for("argument", "audioboom-1") == (
        PODCAST_WORK_ROOT / "argument" / digest
    )
    assert (
        work_dir_for("argument", "g", Path("/x"))
        == Path("/x/argument") / (hashlib.sha256(b"g").hexdigest()[:16])
    )
    assert PODCAST_WORK_ROOT == Path("/persist/my-podcasts/podcast-work")


def test_admit_item_stores_frozen_identity_and_json(store):
    item = _item()
    assert admit_item(store, SOURCE, item, via="poll", now=NOW) is True
    row = store.get_podcast_episode("argument", "audioboom-1")
    ident = podcast_identity(SOURCE, item)
    assert row["display_title"] == ident.display_title
    assert row["episode_slug"] == ident.slug
    assert row["r2_key"] == ident.r2_key
    assert row["admitted_via"] == "poll"
    assert row["status"] == "pending"
    assert json.loads(row["item_json"]) == item_metadata(SOURCE, item)
    assert datetime.fromisoformat(row["pub_date"]) == item.pub_date


def test_admit_item_second_call_is_false_and_changes_nothing(store):
    assert admit_item(store, SOURCE, _item(), via="poll", now=NOW) is True
    before = store.get_podcast_episode("argument", "audioboom-1")
    renamed = _item(title="A different title")
    assert admit_item(store, SOURCE, renamed, via="enqueue", now=NOW) is False
    assert store.get_podcast_episode("argument", "audioboom-1") == before


def test_admit_item_refuses_future_items(store):
    future = _item(pub_date=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="future"):
        admit_item(store, SOURCE, future, via="poll", now=NOW)
    assert store.list_podcast_episodes() == []


def test_admit_item_accepts_an_item_published_exactly_now(store):
    assert admit_item(store, SOURCE, _item(pub_date=NOW), via="poll", now=NOW)


def test_item_from_row_round_trips(store):
    item = _item()
    admit_item(store, SOURCE, item, via="poll", now=NOW)
    row = store.get_podcast_episode("argument", "audioboom-1")
    assert item_from_row(row) == item
    # and identity recomputed from the row equals the frozen one
    ident = podcast_identity(SOURCE, item_from_row(row))
    assert (ident.display_title, ident.slug, ident.r2_key) == (
        row["display_title"],
        row["episode_slug"],
        row["r2_key"],
    )


def test_item_from_row_round_trips_none_duration(store):
    item = _item(duration_s=None, link="")
    admit_item(store, SOURCE, item, via="poll", now=NOW)
    row = store.get_podcast_episode("argument", "audioboom-1")
    assert item_from_row(row) == item
