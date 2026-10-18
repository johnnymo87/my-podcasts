from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.podcast_feed import PodcastItem, eligible_items, parse_podcast_feed
from pipeline.podcast_sources import PODCAST_SOURCES, get_podcast_source


FIXTURE = Path(__file__).parent / "fixtures" / "argument_feed.xml"


def _items() -> list[PodcastItem]:
    return parse_podcast_feed(FIXTURE.read_text(encoding="utf-8"))


def test_parses_identity_and_metadata() -> None:
    first = _items()[0]
    assert first.guid == "audioboom-01a13c3d-ae49-7326-a550-f82c9eff9be1"
    assert first.title == "Canceling (and un-canceling) historic heroes"
    assert first.link == "https://audioboom.com/posts/8964415"
    assert first.pub_date == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
    assert first.duration_s == 4807
    assert first.enclosure_url.startswith("https://pscrb.fm/")
    assert "&amp;" not in first.enclosure_url


def test_description_is_plain_text() -> None:
    desc = _items()[0].description
    assert "<" not in desc
    assert "Jerusalem Demsas and Matt Yglesias" in desc


def test_hms_duration() -> None:
    assert _items()[2].duration_s == 3665


@pytest.mark.parametrize(
    "removed",
    [
        '<guid isPermaLink="false">Buzzsprout-12345</guid>',
        (
            '<enclosure url="https://audioboom.com/posts/1.mp3"'
            ' length="12345" type="audio/mpeg" />'
        ),
        "<pubDate>Thu, 01 Jan 2026 07:00:00 +0000</pubDate>",
    ],
    ids=["guid", "enclosure", "pubDate"],
)
def test_item_missing_required_field_is_skipped(removed: str, caplog) -> None:
    xml = FIXTURE.read_text(encoding="utf-8")
    assert removed in xml
    with caplog.at_level("WARNING", logger="pipeline.podcast_feed"):
        got = parse_podcast_feed(xml.replace(removed, ""))
    assert len(got) == 2
    assert "An older episode" in caplog.text


def test_unparseable_pub_date_is_skipped(caplog) -> None:
    xml = FIXTURE.read_text(encoding="utf-8").replace(
        "Thu, 01 Jan 2026 07:00:00 +0000", "not a date"
    )
    with caplog.at_level("WARNING", logger="pipeline.podcast_feed"):
        got = parse_podcast_feed(xml)
    assert len(got) == 2
    assert "An older episode" in caplog.text


def test_non_ascii_digit_duration_does_not_abort_parse() -> None:
    xml = FIXTURE.read_text(encoding="utf-8").replace(
        "<itunes:duration>01:01:05</itunes:duration>",
        "<itunes:duration>\u00b2</itunes:duration>",
    )
    got = parse_podcast_feed(xml)
    assert len(got) == 3
    assert got[2].duration_s is None


def test_naive_pub_date_is_treated_as_utc() -> None:
    # RFC 2822 "-0000" means "UTC, local zone unknown"; Python parses it naive.
    xml = FIXTURE.read_text(encoding="utf-8").replace(
        "Thu, 15 Oct 2026 07:00:00 +0000", "Thu, 15 Oct 2026 07:00:00 -0000"
    )
    assert parse_podcast_feed(xml)[0].pub_date == datetime(
        2026, 10, 15, 7, 0, tzinfo=UTC
    )


def test_eligible_items_cutoff_future_and_oldest_first() -> None:
    now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    since = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    got = eligible_items(_items(), since=since, now=now)
    assert [i.guid[:14] for i in got] == ["audioboom-0000", "audioboom-01a1"]
    # A future-dated item is never eligible.
    early = eligible_items(
        _items(), since=since, now=datetime(2026, 10, 10, tzinfo=UTC)
    )
    assert early[-1].pub_date.day == 8


def test_source_registry() -> None:
    src = get_podcast_source("argument")
    assert src.feed_slug == "yglesias"
    assert src.rss_url == "https://audioboom.com/channels/5173020.rss"
    assert src.since == datetime(2026, 10, 16, tzinfo=UTC)
    assert [s.key for s in PODCAST_SOURCES] == ["argument"]
    with pytest.raises(KeyError):
        get_podcast_source("nope")
