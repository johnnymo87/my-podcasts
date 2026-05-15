from __future__ import annotations

import inspect

from pipeline import processor
from pipeline.yglesias_filter import is_demsas_podcast, should_skip


# --- Body samples ---


# Mirrors the marker block we see in real Slow Boring podcast posts
# (verified against the 2026-05-14 "What the Spirit debate is really about"
# email pulled from R2). All five markers are present.
_DEMSAS_PODCAST_BODY = """
Spirit Airlines is dead, and the right is blaming Biden.

In the latest episode, Jerusalem and I talk through the specific
circumstances of Spirit's failure.

The transcript will be after the paywall in this post for paying
subscribers.

WATCH THE EPISODE HERE

New episodes post every Thursday.

For an ad-free version and full transcript, subscribe at TheArgumentMag.com.

Subscribe: Apple Podcasts | Spotify | YouTube | Overcast | Pocket Casts

Time stamps:
0:00-Spirit Airlines is dead
7:07-The blocked JetBlue merger
14:54-1960s airline nostalgia

Show Notes:
Coverage of the Strait of Hormuz: Brookings article, CBS News article
"""


# A normal Slow Boring essay body has none of the podcast markers.
_NORMAL_YGLESIAS_BODY = """
Today I want to talk about housing policy and why the same problems
keep cropping up no matter which city tries to tackle them. The core
issue is that local control over land use creates strong incentives
for incumbents to block change, even when the welfare math is lopsided.

Subscribe to Slow Boring for more posts like this one.
"""


# --- is_demsas_podcast ---


def test_demsas_podcast_body_detected():
    assert is_demsas_podcast(_DEMSAS_PODCAST_BODY) is True


def test_normal_essay_body_not_detected():
    assert is_demsas_podcast(_NORMAL_YGLESIAS_BODY) is False


def test_single_marker_is_not_enough():
    # Only one marker present -> below the 2-of-N threshold -> no match.
    # Defends against false positives from incidental phrases.
    body = (
        "Subscribe: Apple Podcasts | Spotify | YouTube | Overcast | Pocket Casts"
        "\n\nThe rest of this is a totally normal essay about zoning.\n"
    )
    assert is_demsas_podcast(body) is False


def test_two_markers_is_enough():
    body = (
        "WATCH THE EPISODE HERE\n\n"
        "Time stamps:\n0:00-intro\n1:23-the point\n"
    )
    assert is_demsas_podcast(body) is True


def test_empty_body_is_not_a_podcast():
    assert is_demsas_podcast("") is False


# --- should_skip ---


def test_should_skip_only_fires_on_yglesias_feed():
    # Same body, different feed -> never skip. Defensive: a future
    # source could legitimately quote the same phrases.
    assert (
        should_skip(body=_DEMSAS_PODCAST_BODY, feed_slug="chinatalk") is False
    )
    assert should_skip(body=_DEMSAS_PODCAST_BODY, feed_slug="levine") is False
    assert (
        should_skip(body=_DEMSAS_PODCAST_BODY, feed_slug="silver") is False
    )


def test_should_skip_yglesias_demsas_podcast():
    assert should_skip(body=_DEMSAS_PODCAST_BODY, feed_slug="yglesias") is True


def test_should_skip_yglesias_normal_essay():
    assert (
        should_skip(body=_NORMAL_YGLESIAS_BODY, feed_slug="yglesias") is False
    )


def test_should_skip_swallows_matcher_exceptions(monkeypatch):
    # If the underlying matcher blows up for any reason, the safe
    # default is "do not skip" -- one over-long episode beats
    # silently dropping a normal essay.
    def boom(_body: str) -> bool:
        raise RuntimeError("matcher crashed")

    monkeypatch.setattr("pipeline.yglesias_filter.is_demsas_podcast", boom)
    assert (
        should_skip(body=_DEMSAS_PODCAST_BODY, feed_slug="yglesias") is False
    )


# --- Wiring check ---


def test_processor_calls_should_skip():
    """processor.process_email_bytes wires through should_skip."""
    source = inspect.getsource(processor.process_email_bytes)
    assert "should_skip" in source
    assert "preset.feed_slug" in source
