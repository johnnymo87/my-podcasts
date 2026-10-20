"""``podcast_report.fetch_feed_xml`` is bounded: a total wall-clock deadline and a
size cap, on top of the per-read timeout. No network: ``httpx.MockTransport``."""

from __future__ import annotations

import gzip
import threading
import time
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


@pytest.fixture(autouse=True)
def _no_leaked_fetch_state():
    """The single-flight reference is module state: never let one test's
    abandoned fetch thread refuse the next test's fetch."""
    podcast_report._active_fetch = None
    yield
    podcast_report._active_fetch = None


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


# --- redirects are handled by us, and never read ---------------------------------


class Endless:
    """A body that would feed a reader forever; counts what was pulled."""

    def __init__(self) -> None:
        self.pulled = 0

    def __iter__(self):
        while True:
            self.pulled += 1
            yield b"x" * 65536


def test_redirect_body_is_never_read():
    body = Endless()

    def handler(request):
        if request.url.path == "/feed.rss":
            return httpx.Response(
                302, headers={"location": "https://example.test/real"}, content=body
            )
        return httpx.Response(200, content=b"<rss/>")

    text = podcast_report.fetch_feed_xml(
        "https://example.test/feed.rss", transport=_transport(handler)
    )
    assert text == "<rss/>"
    assert body.pulled == 0


def test_a_redirect_loop_with_endless_bodies_is_bounded_and_unread():
    bodies: list[Endless] = []

    def handler(request):
        body = Endless()
        bodies.append(body)
        return httpx.Response(
            302, headers={"location": "https://example.test/next"}, content=body
        )

    with pytest.raises(podcast_report.FeedFetchError, match="redirect"):
        podcast_report.fetch_feed_xml(
            "https://example.test/feed.rss", transport=_transport(handler)
        )
    assert len(bodies) == podcast_report.FEED_MAX_REDIRECTS + 1
    assert all(b.pulled == 0 for b in bodies)


def test_exactly_the_maximum_number_of_redirects_is_followed():
    hops: list[str] = []

    def handler(request):
        hops.append(request.url.path)
        n = len(hops)
        if n <= podcast_report.FEED_MAX_REDIRECTS:
            return httpx.Response(302, headers={"location": f"/hop{n}"})
        return httpx.Response(200, content=b"<rss/>")

    assert (
        podcast_report.fetch_feed_xml(
            "https://example.test/feed.rss", transport=_transport(handler)
        )
        == "<rss/>"
    )
    assert len(hops) == podcast_report.FEED_MAX_REDIRECTS + 1


def test_relative_and_absolute_locations_are_joined_against_the_current_url():
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/a/feed.rss":
            return httpx.Response(301, headers={"location": "../b/feed2.rss?x=1"})
        if request.url.path == "/b/feed2.rss":
            return httpx.Response(307, headers={"location": "https://other.test/z"})
        return httpx.Response(200, content=b"<rss/>")

    podcast_report.fetch_feed_xml(
        "https://example.test/a/feed.rss", transport=_transport(handler)
    )
    assert seen == [
        "https://example.test/a/feed.rss",
        "https://example.test/b/feed2.rss?x=1",
        "https://other.test/z",
    ]


def test_redirect_without_location_is_an_error():
    def handler(request):
        return httpx.Response(302, content=Endless())

    with pytest.raises(podcast_report.FeedFetchError, match="Location"):
        podcast_report.fetch_feed_xml(URL, transport=_transport(handler))


def test_redirect_to_a_non_http_scheme_is_refused():
    def handler(request):
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    with pytest.raises(podcast_report.FeedFetchError, match="scheme"):
        podcast_report.fetch_feed_xml(URL, transport=_transport(handler))


def test_one_deadline_spans_all_hops():
    clock = FakeClock()

    def handler(request):
        clock.now += 25.0  # each hop costs 25 fake seconds before headers
        if request.url.path.count("h") < 5:
            return httpx.Response(302, headers={"location": request.url.path + "h"})
        return httpx.Response(200, content=b"<rss/>")

    with pytest.raises(podcast_report.FeedFetchError, match="deadline"):
        podcast_report.fetch_feed_xml(
            "https://example.test/", transport=_transport(handler), clock=clock
        )
    assert clock.now <= 1000.0 + podcast_report.FEED_FETCH_DEADLINE_S + 25.0


def test_redirect_error_does_not_leak_the_signed_url():
    def handler(request):
        return httpx.Response(
            302, headers={"location": request.url.path + "?sig=SECRET"}
        )

    with pytest.raises(podcast_report.FeedFetchError) as info:
        podcast_report.fetch_feed_xml(URL, transport=_transport(handler))
    assert "SECRET" not in str(info.value)


# --- the whole exchange is bounded: blocked before headers -----------------------


class Blocked:
    """A transport that blocks until released (a server that never answers)."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        self.entered.set()
        self.release.wait(30)
        return httpx.Response(200, content=b"<rss/>")


@pytest.fixture
def blocked():
    b = Blocked()
    try:
        yield b
    finally:
        b.release.set()
        leaked = podcast_report._active_fetch
        if leaked is not None:
            leaked.join(5)


def test_blocked_before_headers_is_cut_off_at_the_deadline(blocked):
    t0 = time.monotonic()
    with pytest.raises(podcast_report.FeedFetchError, match="deadline") as info:
        podcast_report.fetch_feed_xml(
            URL, transport=_transport(blocked), deadline_s=0.3
        )
    assert time.monotonic() - t0 < 5.0
    assert blocked.entered.is_set()
    assert "SECRET" not in str(info.value)
    assert podcast_report._active_fetch is not None


def test_a_second_fetch_while_one_is_still_abandoned_fails_at_once(blocked):
    with pytest.raises(podcast_report.FeedFetchError, match="deadline"):
        podcast_report.fetch_feed_xml(
            URL, transport=_transport(blocked), deadline_s=0.3
        )
    t0 = time.monotonic()
    with pytest.raises(podcast_report.FeedFetchError, match="still running"):
        podcast_report.fetch_feed_xml(
            URL, transport=_transport(blocked), deadline_s=0.3
        )
    assert time.monotonic() - t0 < 0.2  # immediate: no wait, no new thread
    assert blocked.calls == 1  # the second call never reached the transport
    assert sum(1 for t in threading.enumerate() if t.name == "podcast-feed-fetch") == 1


def test_fetching_works_again_once_the_abandoned_thread_has_finished(blocked):
    with pytest.raises(podcast_report.FeedFetchError):
        podcast_report.fetch_feed_xml(
            URL, transport=_transport(blocked), deadline_s=0.3
        )
    blocked.release.set()
    podcast_report._active_fetch.join(5)

    def ok(request):
        return httpx.Response(200, content=b"<rss/>")

    assert podcast_report.fetch_feed_xml(URL, transport=_transport(ok)) == "<rss/>"
    assert podcast_report._active_fetch is None


def test_a_cancelled_fetch_stops_without_reading_the_body():
    """The cancel flag, on its own: no clock is involved (long inner deadline)."""
    pulled = Endless()
    cancelled = threading.Event()

    def handler(request):
        cancelled.set()  # the caller gave up while we were waiting for headers
        return httpx.Response(200, content=pulled)

    with pytest.raises(podcast_report.FeedFetchError, match="abandoned"):
        podcast_report._fetch_feed_xml(
            URL,
            clock=time.monotonic,
            transport=_transport(handler),
            deadline_s=3600,
            cancelled=cancelled,
        )
    assert pulled.pulled == 0


def test_the_abandoned_thread_stops_when_it_wakes(blocked):
    """End to end: timeout abandons the thread; waking it must not download."""
    pulled = Endless()

    def handler(request):
        blocked(request)
        return httpx.Response(200, content=pulled)

    real_fetch = podcast_report._fetch_feed_xml

    def long_inner_deadline(url, **kw):
        # Only the join times out: the inner deadline cannot be what stops it.
        return real_fetch(url, **{**kw, "deadline_s": 3600})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(podcast_report, "_fetch_feed_xml", long_inner_deadline)
        with pytest.raises(podcast_report.FeedFetchError, match="deadline"):
            podcast_report.fetch_feed_xml(
                URL, transport=_transport(handler), deadline_s=0.3
            )
        blocked.release.set()
        podcast_report._active_fetch.join(5)
    assert not podcast_report._active_fetch.is_alive()
    assert pulled.pulled == 0


def test_two_concurrent_callers_cannot_both_start_a_fetch(blocked):
    outcome: list = []

    def first():
        try:
            outcome.append(
                podcast_report.fetch_feed_xml(
                    URL, transport=_transport(blocked), deadline_s=30
                )
            )
        except Exception as exc:  # noqa: BLE001
            outcome.append(exc)

    caller = threading.Thread(target=first, daemon=True)
    caller.start()
    assert blocked.entered.wait(5)  # the first fetch is in flight, not abandoned
    with pytest.raises(podcast_report.FeedFetchError, match="still running"):
        podcast_report.fetch_feed_xml(
            URL, transport=_transport(blocked), deadline_s=0.3
        )
    assert blocked.calls == 1
    assert sum(1 for t in threading.enumerate() if t.name == "podcast-feed-fetch") == 1
    blocked.release.set()
    caller.join(5)
    assert outcome == ["<rss/>"]
    assert podcast_report._active_fetch is None


def test_errors_from_the_worker_keep_their_type():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        podcast_report.fetch_feed_xml(URL, transport=_transport(handler))
    assert podcast_report._active_fetch is None
