from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline import podcast_poller
from pipeline.db import PODCAST_HEALTH_ALERT_AFTER, StateStore
from pipeline.podcast_poller import discover
from pipeline.podcast_sources import PODCAST_SOURCES


FIXTURE = Path(__file__).parent / "fixtures" / "argument_feed.xml"
FEED_XML = FIXTURE.read_text(encoding="utf-8")
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
# Between the fixture's Oct 8 and Oct 15 items (Jan 1 is older still).
SINCE = datetime(2026, 10, 10, tzinfo=UTC)
NEWEST = "audioboom-01a13c3d-ae49-7326-a550-f82c9eff9be1"
MIDDLE = "audioboom-0000aaaa-bbbb-cccc-dddd-eeeeffff0000"

SOURCE = dataclasses.replace(PODCAST_SOURCES[0], since=SINCE)


class Alerts:
    def __init__(self, result: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result = result

    def __call__(self, text: str, severity: str = "info") -> bool:
        self.calls.append((text, severity))
        return self.result


@pytest.fixture(autouse=True)
def _fresh_collision_memory():
    podcast_poller._ALERTED_COLLISIONS.clear()
    yield
    podcast_poller._ALERTED_COLLISIONS.clear()


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "test.db")
    yield s
    s.close()


def _fetch_ok(url: str) -> str:
    return FEED_XML


def _guids(store) -> list[str]:
    return sorted(r["guid"] for r in store.list_podcast_episodes())


def test_admits_only_items_in_window(store):
    counts = discover(store, now=NOW, fetch=_fetch_ok, alert=Alerts(), sources=[SOURCE])
    assert counts == {"argument": 1}
    assert _guids(store) == [NEWEST]
    assert store.get_podcast_episode("argument", NEWEST)["admitted_via"] == "poll"


def test_since_before_both_admits_both_but_not_the_old_one(store):
    source = dataclasses.replace(SOURCE, since=datetime(2026, 10, 1, tzinfo=UTC))
    counts = discover(store, now=NOW, fetch=_fetch_ok, alert=Alerts(), sources=[source])
    assert counts == {"argument": 2}
    assert _guids(store) == sorted([NEWEST, MIDDLE])


def test_items_after_now_are_not_admitted(store):
    early = datetime(2026, 10, 12, tzinfo=UTC)
    source = dataclasses.replace(SOURCE, since=datetime(2026, 10, 1, tzinfo=UTC))
    discover(store, now=early, fetch=_fetch_ok, alert=Alerts(), sources=[source])
    assert _guids(store) == [MIDDLE]


def test_idempotent_across_ticks(store):
    kwargs = {"fetch": _fetch_ok, "alert": Alerts(), "sources": [SOURCE]}
    assert discover(store, now=NOW, **kwargs) == {"argument": 1}
    assert discover(store, now=NOW, **kwargs) == {"argument": 0}
    assert len(store.list_podcast_episodes()) == 1


def test_success_records_healthy_tick(store):
    discover(store, now=NOW, fetch=_fetch_ok, alert=Alerts(), sources=[SOURCE])
    health = store.get_podcast_health("argument")
    assert health["consecutive_failures"] == 0
    assert health["last_success_at"] is not None


def _raising(url: str) -> str:
    raise ConnectionError("boom")


def test_fetch_failing_four_times_alerts_exactly_once(store):
    alerts = Alerts()
    for _ in range(PODCAST_HEALTH_ALERT_AFTER + 2):
        counts = discover(
            store, now=NOW, fetch=_raising, alert=alerts, sources=[SOURCE]
        )
        assert counts == {"argument": 0}
    assert len(alerts.calls) == 1
    text, severity = alerts.calls[0]
    assert severity == "error"
    assert "The Argument" in text
    assert f"failed {PODCAST_HEALTH_ALERT_AFTER} times in a row" in text
    assert "boom" in text
    assert store.get_podcast_health("argument")["consecutive_failures"] == 6


def test_failure_before_threshold_does_not_alert(store):
    alerts = Alerts()
    for _ in range(PODCAST_HEALTH_ALERT_AFTER - 1):
        discover(store, now=NOW, fetch=_raising, alert=alerts, sources=[SOURCE])
    assert alerts.calls == []


def test_empty_feed_counts_as_failure(store):
    empty = '<rss version="2.0"><channel><title>x</title></channel></rss>'
    alerts = Alerts()
    for _ in range(PODCAST_HEALTH_ALERT_AFTER):
        discover(store, now=NOW, fetch=lambda u: empty, alert=alerts, sources=[SOURCE])
    assert store.get_podcast_health("argument")["consecutive_failures"] == (
        PODCAST_HEALTH_ALERT_AFTER
    )
    assert len(alerts.calls) == 1
    assert "zero items" in alerts.calls[0][0]


def test_unparseable_feed_counts_as_failure(store):
    alerts = Alerts()
    discover(
        store, now=NOW, fetch=lambda u: "<html>nope", alert=alerts, sources=[SOURCE]
    )
    assert store.get_podcast_health("argument")["consecutive_failures"] == 1
    assert store.list_podcast_episodes() == []


def test_feed_with_items_all_outside_window_is_healthy(store):
    source = dataclasses.replace(SOURCE, since=datetime(2026, 10, 17, tzinfo=UTC))
    counts = discover(store, now=NOW, fetch=_fetch_ok, alert=Alerts(), sources=[source])
    assert counts == {"argument": 0}
    assert store.get_podcast_health("argument")["consecutive_failures"] == 0


def test_success_resets_streak_and_rearms_alert(store):
    alerts = Alerts()
    for _ in range(PODCAST_HEALTH_ALERT_AFTER):
        discover(store, now=NOW, fetch=_raising, alert=alerts, sources=[SOURCE])
    discover(store, now=NOW, fetch=_fetch_ok, alert=alerts, sources=[SOURCE])
    assert store.get_podcast_health("argument")["consecutive_failures"] == 0
    for _ in range(PODCAST_HEALTH_ALERT_AFTER):
        discover(store, now=NOW, fetch=_raising, alert=alerts, sources=[SOURCE])
    assert len(alerts.calls) == 2


def test_one_source_failing_does_not_stop_another(store):
    bad = dataclasses.replace(SOURCE, key="bad", rss_url="https://bad.example/rss")
    good = dataclasses.replace(
        SOURCE, key="good", rss_url="https://good.example/rss", feed_slug="other"
    )

    def fetch(url: str) -> str:
        if "bad" in url:
            raise ConnectionError("down")
        return FEED_XML

    counts = discover(store, now=NOW, fetch=fetch, alert=Alerts(), sources=[bad, good])
    assert counts == {"bad": 0, "good": 1}
    assert [r["source"] for r in store.list_podcast_episodes()] == ["good"]
    assert store.get_podcast_health("bad")["consecutive_failures"] == 1
    assert store.get_podcast_health("good")["consecutive_failures"] == 0


def test_alert_sender_raising_does_not_abort_the_tick(store):
    def exploding(text: str, severity: str = "info") -> bool:
        raise RuntimeError("pigeon down")

    bad = dataclasses.replace(SOURCE, key="bad", rss_url="https://bad.example/rss")
    good = dataclasses.replace(
        SOURCE, key="good", rss_url="https://good.example/rss", feed_slug="other"
    )

    def fetch(url: str) -> str:
        if "bad" in url:
            raise ConnectionError("down")
        return FEED_XML

    for _ in range(PODCAST_HEALTH_ALERT_AFTER - 1):
        discover(store, now=NOW, fetch=fetch, alert=exploding, sources=[bad, good])
    # The threshold tick: the alert sender blows up, the next source still runs.
    counts = discover(store, now=NOW, fetch=fetch, alert=exploding, sources=[bad, good])
    assert counts == {"bad": 0, "good": 0}  # good was admitted on tick one
    assert len(store.list_podcast_episodes()) == 1


def test_failure_message_has_no_url_query(store):
    alerts = Alerts()

    def leaky(url: str) -> str:
        raise RuntimeError("GET https://cdn.example/a.mp3?sig=SECRET failed")

    for _ in range(PODCAST_HEALTH_ALERT_AFTER):
        discover(store, now=NOW, fetch=leaky, alert=alerts, sources=[SOURCE])
    assert "SECRET" not in alerts.calls[0][0]
    assert "SECRET" not in (store.get_podcast_health("argument")["last_error"] or "")


def test_r2_key_collision_alerts_once_and_does_not_stop_others(store, monkeypatch):
    source = dataclasses.replace(SOURCE, since=datetime(2026, 10, 1, tzinfo=UTC))
    real_admit = podcast_poller.admit_item

    def admit(store_, source_, item, **kw):
        if item.guid == MIDDLE:
            raise sqlite3.IntegrityError("UNIQUE constraint failed: r2_key")
        return real_admit(store_, source_, item, **kw)

    monkeypatch.setattr(podcast_poller, "admit_item", admit)
    alerts = Alerts()
    counts = discover(store, now=NOW, fetch=_fetch_ok, alert=alerts, sources=[source])
    assert counts == {"argument": 1}
    assert _guids(store) == [NEWEST]
    assert len(alerts.calls) == 1
    assert MIDDLE in alerts.calls[0][0]
    assert alerts.calls[0][1] == "error"
    # The fetch itself worked: the source is healthy.
    assert store.get_podcast_health("argument")["consecutive_failures"] == 0


def test_collisions_across_sources_share_one_alert(store, monkeypatch):
    a = dataclasses.replace(SOURCE, key="a", feed_slug="fa")
    b = dataclasses.replace(SOURCE, key="b", feed_slug="fb")

    def admit(store_, source_, item, **kw):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: r2_key")

    monkeypatch.setattr(podcast_poller, "admit_item", admit)
    alerts = Alerts()
    discover(store, now=NOW, fetch=_fetch_ok, alert=alerts, sources=[a, b])
    assert len(alerts.calls) == 1


def test_permanent_collision_alerts_once_across_ticks(store, monkeypatch):
    def admit(store_, source_, item, **kw):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: r2_key")

    monkeypatch.setattr(podcast_poller, "admit_item", admit)
    alerts = Alerts()
    kwargs = {"fetch": _fetch_ok, "alert": alerts, "sources": [SOURCE]}
    discover(store, now=NOW, **kwargs)
    discover(store, now=NOW, **kwargs)
    discover(store, now=NOW, **kwargs)
    assert len(alerts.calls) == 1


def test_a_new_collision_alerts_again_naming_only_the_new_one(store, monkeypatch):
    source = dataclasses.replace(SOURCE, since=datetime(2026, 10, 1, tzinfo=UTC))
    colliding = {MIDDLE}

    def admit(store_, source_, item, **kw):
        if item.guid in colliding:
            raise sqlite3.IntegrityError("UNIQUE constraint failed: r2_key")
        return False

    monkeypatch.setattr(podcast_poller, "admit_item", admit)
    alerts = Alerts()
    kwargs = {"fetch": _fetch_ok, "alert": alerts, "sources": [source]}
    discover(store, now=NOW, **kwargs)
    colliding.add(NEWEST)
    discover(store, now=NOW, **kwargs)
    assert len(alerts.calls) == 2
    assert NEWEST in alerts.calls[1][0]
    assert MIDDLE not in alerts.calls[1][0]


class _HealthRaises:
    """A store whose health bookkeeping fails; everything else is real."""

    def __init__(self, real, fail_ok=True, fail_failure=True):
        self._real = real
        self.fail_ok = fail_ok
        self.fail_failure = fail_failure
        self.record_calls = 0

    def record_podcast_discovery(self, source, *, ok, error, now):
        self.record_calls += 1
        if (ok and self.fail_ok) or (not ok and self.fail_failure):
            raise sqlite3.OperationalError("database is locked")
        return self._real.record_podcast_discovery(source, ok=ok, error=error, now=now)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_health_recording_failing_on_failure_path_does_not_stop_other_sources(store):
    bad = dataclasses.replace(SOURCE, key="bad", rss_url="https://bad.example/rss")
    good = dataclasses.replace(
        SOURCE, key="good", rss_url="https://good.example/rss", feed_slug="other"
    )

    def fetch(url: str) -> str:
        if "bad" in url:
            raise ConnectionError("down")
        return FEED_XML

    wrapped = _HealthRaises(store, fail_ok=False, fail_failure=True)
    counts = discover(
        wrapped, now=NOW, fetch=fetch, alert=Alerts(), sources=[bad, good]
    )
    assert counts == {"bad": 0, "good": 1}
    assert [r["source"] for r in store.list_podcast_episodes()] == ["good"]
    assert wrapped.record_calls == 2


def test_health_recording_failing_on_success_path_is_contained(store):
    other = dataclasses.replace(SOURCE, key="other", feed_slug="other")
    wrapped = _HealthRaises(store, fail_ok=True, fail_failure=True)
    counts = discover(
        wrapped, now=NOW, fetch=_fetch_ok, alert=Alerts(), sources=[SOURCE, other]
    )
    # Admission happened; the bookkeeping failure is logged, not raised.
    assert counts == {"argument": 1, "other": 1}
    assert wrapped.record_calls == 2


def test_discover_never_raises_for_a_source(store):
    wrapped = _HealthRaises(store)
    discover(wrapped, now=NOW, fetch=_raising, alert=Alerts(), sources=[SOURCE])


def test_a_hanging_alert_cannot_stall_discovery(store, monkeypatch):
    import threading
    import time

    from pipeline import alerts as alerts_mod

    monkeypatch.setattr(alerts_mod, "BOUNDED_ALERT_TIMEOUT_S", 0.2)
    release = threading.Event()

    def hung(text, severity="info"):
        release.wait(30)
        return True

    good = dataclasses.replace(
        SOURCE, key="good", rss_url="https://good.example/rss", feed_slug="other"
    )

    def fetch(url: str) -> str:
        if "good" in url:
            return FEED_XML
        raise ConnectionError("down")

    try:
        t0 = time.monotonic()
        for _ in range(PODCAST_HEALTH_ALERT_AFTER):
            counts = discover(
                store, now=NOW, fetch=fetch, alert=hung, sources=[SOURCE, good]
            )
        assert time.monotonic() - t0 < 5.0
    finally:
        release.set()
    assert counts["good"] == 0  # already admitted on the first tick
    assert store.get_podcast_health("argument")["consecutive_failures"] == (
        PODCAST_HEALTH_ALERT_AFTER
    )
