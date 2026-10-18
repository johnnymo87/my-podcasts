from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline import feed as feed_mod
from pipeline.db import StateStore
from pipeline.podcast_feed import PodcastItem
from pipeline.podcast_jobs import admit_item
from pipeline.podcast_publish import publish_podcast_episode
from pipeline.podcast_sources import get_podcast_source


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
SOURCE = get_podcast_source("argument")
ITEM = PodcastItem(
    guid="audioboom-1",
    title="Canceling historic heroes",
    link="https://audioboom.com/posts/1",
    pub_date=datetime(2026, 10, 15, 7, 0, tzinfo=UTC),
    duration_s=4807,
    enclosure_url="https://pscrb.fm/x.mp3",
    description="desc",
)


class FakeR2:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.puts: list[str] = []
        self.fail_put_once = False

    def upload_file(self, path, key, content_type=None):
        assert Path(path).exists()
        self.uploads.append((key, content_type))

    def put_object_bytes(self, key, data, content_type=None):
        if self.fail_put_once:
            self.fail_put_once = False
            raise OSError("feed upload failed")
        self.puts.append(key)


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_ffprobe(monkeypatch):
    monkeypatch.setattr(
        "pipeline.script_processor._parse_duration_seconds", lambda path: 60
    )


def _setup(tmp_path, store, *, summary: str | None = "A tight summary."):
    admit_item(store, SOURCE, ITEM, via="poll", now=NOW)
    row = store.get_podcast_episode("argument", ITEM.guid)
    work = tmp_path / "work"
    work.mkdir()
    (work / "script.txt").write_text("Welcome to the briefing.", encoding="utf-8")
    if summary is not None:
        (work / "summary.txt").write_text(summary, encoding="utf-8")
    return row, work


def test_happy_path_publishes_one_episode(tmp_path, store, fake_tts_render):
    row, work = _setup(tmp_path, store)
    r2 = FakeR2()

    assert publish_podcast_episode(row, work, store=store, r2_client=r2) == "published"

    assert r2.uploads == [(row["r2_key"], "audio/mpeg")]
    [ep] = store.list_episodes(feed_slug="yglesias")
    assert ep.title == row["display_title"]
    assert ep.r2_key == row["r2_key"]
    assert ep.slug == row["episode_slug"]
    assert ep.source_url == ITEM.link
    assert ep.category == "News"
    assert ep.summary == "A tight summary."
    assert "unofficial" in ep.show_notes_html.lower()
    assert "feed.xml" in r2.puts and "feeds/yglesias.xml" in r2.puts
    assert len(fake_tts_render) == 1
    assert fake_tts_render[0]["feed_slug"] == "yglesias"
    assert fake_tts_render[0]["episode_id"] == row["episode_slug"]


def test_show_notes_content(tmp_path, store, fake_tts_render):
    row, work = _setup(tmp_path, store)
    publish_podcast_episode(row, work, store=store, r2_client=FakeR2())
    notes = (work / "show_notes.md").read_text(encoding="utf-8")
    assert notes == (
        "## Episode Summary\n\nA tight summary.\n\n---\n\n"
        "Unofficial AI-generated briefing of this episode of The Argument, "
        "produced from the public audio. "
        f"[Original episode]({ITEM.link})\n"
    )
    assert not list(work.glob("*.tmp"))


def test_show_notes_fallback_without_summary_file(tmp_path, store, fake_tts_render):
    row, work = _setup(tmp_path, store, summary=None)
    publish_podcast_episode(row, work, store=store, r2_client=FakeR2())
    [ep] = store.list_episodes(feed_slug="yglesias")
    assert ep.summary  # the fallback line, not None
    assert "## Episode Summary" in (work / "show_notes.md").read_text()


def test_summary_headings_and_rules_cannot_truncate_it(
    tmp_path, store, fake_tts_render
):
    row, work = _setup(
        tmp_path,
        store,
        summary="# Heading\nFirst point.\n---\n***\n ___ \n## Sub\nSecond point.\n",
    )
    publish_podcast_episode(row, work, store=store, r2_client=FakeR2())
    [ep] = store.list_episodes(feed_slug="yglesias")
    assert ep.summary == "First point.\nSecond point."


def test_summary_of_only_rules_falls_back(tmp_path, store, fake_tts_render):
    row, work = _setup(tmp_path, store, summary="---\n# x\n")
    publish_podcast_episode(row, work, store=store, r2_client=FakeR2())
    [ep] = store.list_episodes(feed_slug="yglesias")
    assert ep.summary == "An AI-generated briefing of this episode."


def test_no_link_publishes_without_source_url(tmp_path, store, fake_tts_render):
    item = PodcastItem(**{**ITEM.__dict__, "guid": "g2", "link": ""})
    admit_item(store, SOURCE, item, via="poll", now=NOW)
    row = store.get_podcast_episode("argument", "g2")
    work = tmp_path / "w2"
    work.mkdir()
    (work / "script.txt").write_text("Body.", encoding="utf-8")
    publish_podcast_episode(row, work, store=store, r2_client=FakeR2())
    [ep] = store.list_episodes(feed_slug="yglesias")
    assert ep.source_url is None
    assert "Original episode:" not in (work / "show_notes.md").read_text()


def test_failure_after_audio_upload_retry_reinserts_exactly_once(
    tmp_path, store, fake_tts_render, monkeypatch
):
    row, work = _setup(tmp_path, store)
    r2 = FakeR2()
    real_insert = store.insert_episode
    calls = {"n": 0}

    def flaky_insert(episode):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db write failed")
        real_insert(episode)

    monkeypatch.setattr(store, "insert_episode", flaky_insert)

    with pytest.raises(RuntimeError, match="db write failed"):
        publish_podcast_episode(row, work, store=store, r2_client=r2)
    assert store.list_episodes(feed_slug="yglesias") == []
    assert [k for k, _ in r2.uploads] == [row["r2_key"]]

    assert publish_podcast_episode(row, work, store=store, r2_client=r2) == "published"

    assert [k for k, _ in r2.uploads] == [row["r2_key"]] * 2
    assert len(store.list_episodes(feed_slug="yglesias")) == 1
    assert len(fake_tts_render) == 2


def test_failure_after_db_commit_retry_reconciles_without_tts(
    tmp_path, store, fake_tts_render, monkeypatch
):
    row, work = _setup(tmp_path, store)
    r2 = FakeR2()
    real = feed_mod.regenerate_and_upload_feed
    calls = {"n": 0}

    def flaky_regen(s, r):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("feed regen failed")
        real(s, r)

    monkeypatch.setattr(
        "pipeline.script_processor.regenerate_and_upload_feed", flaky_regen
    )
    monkeypatch.setattr(
        "pipeline.podcast_publish.regenerate_and_upload_feed", flaky_regen
    )

    with pytest.raises(RuntimeError, match="feed regen failed"):
        publish_podcast_episode(row, work, store=store, r2_client=r2)
    assert len(store.list_episodes(feed_slug="yglesias")) == 1

    assert publish_podcast_episode(row, work, store=store, r2_client=r2) == "reconciled"

    assert len(store.list_episodes(feed_slug="yglesias")) == 1
    assert len(fake_tts_render) == 1
    assert [k for k, _ in r2.uploads] == [row["r2_key"]]
    assert "feeds/yglesias.xml" in r2.puts


def test_failure_during_feed_upload_retry_reconciles(tmp_path, store, fake_tts_render):
    row, work = _setup(tmp_path, store)
    r2 = FakeR2()
    r2.fail_put_once = True

    with pytest.raises(OSError, match="feed upload failed"):
        publish_podcast_episode(row, work, store=store, r2_client=r2)
    assert len(store.list_episodes(feed_slug="yglesias")) == 1

    assert publish_podcast_episode(row, work, store=store, r2_client=r2) == "reconciled"

    assert len(store.list_episodes(feed_slug="yglesias")) == 1
    assert len(fake_tts_render) == 1
    assert "feed.xml" in r2.puts and "feeds/yglesias.xml" in r2.puts


@pytest.mark.parametrize(
    "field", ["display_title", "episode_slug", "r2_key", "pub_date"]
)
def test_identity_drift_raises_before_any_tts(tmp_path, store, fake_tts_render, field):
    row, work = _setup(tmp_path, store)
    r2 = FakeR2()
    drifted = dict(row)
    drifted[field] = (
        "2026-10-16T07:00:00.000000+00:00" if field == "pub_date" else "drifted"
    )

    with pytest.raises(RuntimeError, match="identity drift"):
        publish_podcast_episode(drifted, work, store=store, r2_client=r2)

    assert fake_tts_render == []
    assert r2.uploads == []
    assert store.list_episodes() == []


def test_identity_drift_when_item_json_title_changes(tmp_path, store, fake_tts_render):
    row, work = _setup(tmp_path, store)
    meta = json.loads(row["item_json"])
    meta["title"] = "Something else"
    drifted = {**row, "item_json": json.dumps(meta)}
    with pytest.raises(RuntimeError, match="identity drift"):
        publish_podcast_episode(drifted, work, store=store, r2_client=FakeR2())
    assert fake_tts_render == []


def test_drift_is_checked_even_when_already_published(tmp_path, store, fake_tts_render):
    row, work = _setup(tmp_path, store)
    publish_podcast_episode(row, work, store=store, r2_client=FakeR2())
    with pytest.raises(RuntimeError, match="identity drift"):
        publish_podcast_episode(
            {**row, "episode_slug": "drifted"},
            work,
            store=store,
            r2_client=FakeR2(),
        )


def test_asserts_publish_returned_the_frozen_key(
    tmp_path, store, fake_tts_render, monkeypatch
):
    row, work = _setup(tmp_path, store)
    from pipeline.script_processor import PublishResult

    monkeypatch.setattr(
        "pipeline.podcast_publish.publish_script",
        lambda **kw: PublishResult(
            title=kw["title"],
            r2_key="episodes/yglesias/other.mp3",
            feed_slug="yglesias",
            size_bytes=1,
            duration_seconds=1,
        ),
    )
    with pytest.raises(RuntimeError, match="r2_key"):
        publish_podcast_episode(row, work, store=store, r2_client=FakeR2())
