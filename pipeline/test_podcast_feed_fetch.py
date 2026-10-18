"""``podcast_report.fetch_feed_xml`` is bounded: a total wall-clock deadline and a
size cap, on top of the per-read timeout. No network: ``httpx.MockTransport``."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from pipeline import __main__ as main_mod
from pipeline import podcast_report
from pipeline.db import StateStore
from pipeline.podcast_poller import discover
from pipeline.podcast_sources import get_podcast_source


URL = "https://example.test/feed.rss?sig=SECRET"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_fetches_small_feed_with_ua_and_redirects():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        if request.url.path == "/feed.rss":
            return httpx.Response(
                302, headers={"location": "https://example.test/real"}
            )
        return httpx.Response(200, content="<rss>café</rss>".encode())

    text = podcast_report.fetch_feed_xml(
        "https://example.test/feed.rss", transport=_transport(handler)
    )
    assert text == "<rss>café</rss>"
    assert seen["ua"] == "my-podcasts/1.0"


def test_decodes_gzip_and_declared_charset():
    body = "<rss>é</rss>".encode("latin-1")

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/rss+xml; charset=latin-1"},
            content=body,
        )

    assert (
        podcast_report.fetch_feed_xml(URL, transport=_transport(handler))
        == "<rss>é</rss>"
    )

    def gz(request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress(b"<rss/>"),
        )

    assert podcast_report.fetch_feed_xml(URL, transport=_transport(gz)) == "<rss/>"


def test_http_error_status_still_raises_httpx_error():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        podcast_report.fetch_feed_xml(URL, transport=_transport(handler))


def test_trickle_past_the_total_deadline_is_cut_off():
    clock = FakeClock()

    def body():
        # Each chunk is tiny, so no per-read timeout ever fires; the clock
        # advances 20 s per chunk.
        for _ in range(100):
            clock.now += 20.0
            yield b"x"

    def handler(request):
        return httpx.Response(200, content=body())

    with pytest.raises(podcast_report.FeedFetchError, match="deadline") as info:
        podcast_report.fetch_feed_xml(URL, transport=_transport(handler), clock=clock)
    assert isinstance(info.value, httpx.HTTPError)
    # Stopped promptly: ~60 s of fake time, not the whole generator.
    assert clock.now <= 1000.0 + podcast_report.FEED_FETCH_DEADLINE_S + 20.0


def test_deadline_error_does_not_leak_the_signed_url():
    clock = FakeClock()

    def body():
        while True:
            clock.now += 61.0
            yield b"x"

    def handler(request):
        return httpx.Response(200, content=body())

    with pytest.raises(podcast_report.FeedFetchError) as info:
        podcast_report.fetch_feed_xml(URL, transport=_transport(handler), clock=clock)
    assert "SECRET" not in str(info.value)


def test_body_over_the_size_cap_is_refused_while_streaming():
    produced = []

    def body():
        chunk = b"x" * (1 << 20)
        for i in range(100):
            produced.append(i)
            yield chunk

    def handler(request):
        return httpx.Response(200, content=body())

    with pytest.raises(podcast_report.FeedFetchError, match="bytes"):
        podcast_report.fetch_feed_xml(URL, transport=_transport(handler))
    # It stopped reading right after the cap, long before the generator ended.
    assert len(produced) <= podcast_report.FEED_MAX_BYTES // (1 << 20) + 2


def test_declared_content_length_over_the_cap_is_refused_before_reading():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-length": str(podcast_report.FEED_MAX_BYTES + 1)},
            content=b"x",
        )

    with pytest.raises(podcast_report.FeedFetchError, match="bytes"):
        podcast_report.fetch_feed_xml(URL, transport=_transport(handler))


def test_a_body_exactly_at_the_cap_is_accepted():
    payload = b"x" * podcast_report.FEED_MAX_BYTES

    def handler(request):
        return httpx.Response(200, content=payload)

    text = podcast_report.fetch_feed_xml(URL, transport=_transport(handler))
    assert len(text) == podcast_report.FEED_MAX_BYTES


def test_limits_are_the_documented_ones():
    assert podcast_report.FEED_FETCH_DEADLINE_S == 60
    assert podcast_report.FEED_MAX_BYTES == 10 * 1024 * 1024


# --- both call sites handle it -------------------------------------------------


def _trickle(monkeypatch):
    def fetch(url, **kw):
        raise podcast_report.FeedFetchError("feed fetch exceeded the 60s deadline")

    monkeypatch.setattr(podcast_report, "fetch_feed_xml", fetch)


def test_poller_records_a_failed_tick(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "s.sqlite3")
    source = get_podcast_source("argument")
    alerts: list = []
    _trickle(monkeypatch)
    counts = discover(
        store,
        now=datetime(2026, 10, 18, tzinfo=UTC),
        # discover's default binds the real function at import time.
        fetch=lambda url: podcast_report.fetch_feed_xml(url),
        alert=lambda text, severity="info": alerts.append(text) or True,
        sources=[source],
    )
    assert counts == {"argument": 0}
    health = store.get_podcast_health("argument")
    assert health["consecutive_failures"] == 1
    assert "FeedFetchError" in health["last_error"]


def test_enqueue_cli_reports_a_clean_error(monkeypatch):
    _trickle(monkeypatch)
    result = CliRunner().invoke(
        main_mod.cli,
        ["podcast-report", "enqueue", "--source", "argument", "--guid", "g"],
    )
    assert result.exit_code == 1
    assert "FeedFetchError" in result.output
    assert "Traceback" not in result.output


def test_dry_run_cli_reports_a_clean_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.setattr(podcast_report, "check_opencode_serve", lambda: None)
    _trickle(monkeypatch)
    result = CliRunner().invoke(
        main_mod.cli,
        ["podcast-report", "dry-run", "--source", "argument", "--guid", "g"],
    )
    assert result.exit_code == 1
    assert "FeedFetchError" in result.output
    assert "Traceback" not in result.output


def test_fixture_feed_is_far_below_the_cap():
    fixture = Path(__file__).parent / "fixtures" / "argument_feed.xml"
    assert fixture.stat().st_size < podcast_report.FEED_MAX_BYTES // 10
