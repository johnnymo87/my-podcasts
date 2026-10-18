"""Consumer wiring for podcast reports: recovery at startup and the tick."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from pipeline import consumer, podcast_executor, podcast_poller
from pipeline.consumer import (
    _PODCAST_POLL_INTERVAL,
    _podcast_tick,
    _recover_podcast_episodes,
    consume_forever,
)
from pipeline.db import PODCAST_MAX_FAILURES, StateStore


class _Done(BaseException):
    """Breaks consume_forever's infinite loop (bypasses ``except Exception``)."""


@pytest.fixture(autouse=True)
def _fresh_clock(monkeypatch):
    monkeypatch.setattr(consumer, "_last_podcast_poll", None)
    monkeypatch.setattr("pipeline.blog_poller.poll_all_blogs", lambda store, r2: None)
    monkeypatch.setattr(consumer, "_last_blog_poll", 0.0)


@pytest.fixture
def calls(monkeypatch):
    rec = {"discover": [], "execute": []}

    def fake_discover(store, *, now, **kw):
        rec["discover"].append(now)
        return {"the-argument": 0}

    def fake_execute(store, r2, **kw):
        rec["execute"].append(1)
        return None

    monkeypatch.setattr(podcast_poller, "discover", fake_discover)
    monkeypatch.setattr(podcast_executor, "run_one_podcast_episode", fake_execute)
    return rec


# --- _podcast_tick ---------------------------------------------------------


def test_discovery_runs_on_first_tick_even_at_low_monotonic_time(calls):
    # A freshly booted host can have monotonic() < the interval.
    _podcast_tick(MagicMock(), MagicMock(), 5.0)
    assert len(calls["discover"]) == 1
    assert calls["discover"][0].tzinfo is UTC
    assert len(calls["execute"]) == 1


def test_discovery_not_repeated_within_interval_but_executor_runs_each_tick(calls):
    _podcast_tick(MagicMock(), MagicMock(), 1000.0)
    _podcast_tick(MagicMock(), MagicMock(), 1010.0)
    _podcast_tick(MagicMock(), MagicMock(), 1000.0 + _PODCAST_POLL_INTERVAL - 1)
    assert len(calls["discover"]) == 1
    assert len(calls["execute"]) == 3


def test_discovery_again_after_interval(calls):
    _podcast_tick(MagicMock(), MagicMock(), 1000.0)
    _podcast_tick(MagicMock(), MagicMock(), 1000.0 + _PODCAST_POLL_INTERVAL)
    assert len(calls["discover"]) == 2


def test_discovery_exception_does_not_stop_executor_and_advances_clock(
    monkeypatch, calls, capsys
):
    attempts = []

    def boom(store, *, now, **kw):
        attempts.append(1)
        raise RuntimeError("feed exploded")

    monkeypatch.setattr(podcast_poller, "discover", boom)
    _podcast_tick(MagicMock(), MagicMock(), 100.0)
    assert len(calls["execute"]) == 1
    assert "feed exploded" in capsys.readouterr().out
    # The attempt counts: a broken discovery is not refetched on every 10 s tick.
    _podcast_tick(MagicMock(), MagicMock(), 110.0)
    assert len(attempts) == 1
    assert len(calls["execute"]) == 2


def test_executor_exception_does_not_propagate(monkeypatch, calls, capsys):
    def boom(store, r2, **kw):
        raise RuntimeError("executor exploded")

    monkeypatch.setattr(podcast_executor, "run_one_podcast_episode", boom)
    _podcast_tick(MagicMock(), MagicMock(), 100.0)  # must not raise
    assert "executor exploded" in capsys.readouterr().out
    assert len(calls["discover"]) == 1


def test_keyboard_interrupt_propagates_from_executor(monkeypatch, calls):
    def interrupt(store, r2, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(podcast_executor, "run_one_podcast_episode", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _podcast_tick(MagicMock(), MagicMock(), 100.0)


def test_keyboard_interrupt_propagates_from_discovery(monkeypatch, calls):
    def interrupt(store, *, now, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(podcast_poller, "discover", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _podcast_tick(MagicMock(), MagicMock(), 100.0)
    assert calls["execute"] == []


# --- recovery --------------------------------------------------------------


def _in_progress_store(tmp_path, failure_counts):
    store = StateStore(tmp_path / "t.db")
    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    for i, fc in enumerate(failure_counts):
        store.admit_podcast_episode(
            source="the-argument",
            guid=f"g{i}",
            item_json="{}",
            pub_date=f"2026-10-1{i}T00:00:00+00:00",
            display_title=f"Report: title {i}",
            episode_slug=f"slug-{i}",
            r2_key=f"episodes/yglesias/2026-10-1{i}-slug-{i}.mp3",
            admitted_via="poll",
            now=now,
        )
        assert store.claim_podcast_episode("the-argument", f"g{i}")
        store._conn.execute(
            "UPDATE podcast_episodes SET failure_count = ? WHERE guid = ?",
            (fc, f"g{i}"),
        )
    store._conn.commit()
    return store


def test_recovery_alerts_only_for_errored_rows(tmp_path, monkeypatch, capsys):
    # g0 has budget left; g1 is one interruption away from exhaustion.
    store = _in_progress_store(tmp_path, [0, PODCAST_MAX_FAILURES - 1])
    alerts = []
    monkeypatch.setattr(
        "pipeline.alerts.send_alert",
        lambda text, severity="info": alerts.append((text, severity)) or True,
    )
    _recover_podcast_episodes(store)
    assert len(alerts) == 1
    text, severity = alerts[0]
    assert severity == "error"
    assert "Report: title 1" in text
    assert "title 0" not in text
    out = capsys.readouterr().out
    assert "the-argument/g0" in out  # printed, not alerted
    assert store.get_podcast_episode("the-argument", "g0")["status"] == "pending"
    assert store.get_podcast_episode("the-argument", "g1")["status"] == "errored"


def test_recovery_no_rows_is_silent(tmp_path, monkeypatch, capsys):
    store = StateStore(tmp_path / "t.db")
    alerts = []
    monkeypatch.setattr(
        "pipeline.alerts.send_alert", lambda *a, **k: alerts.append(a) or True
    )
    _recover_podcast_episodes(store)
    assert alerts == []
    assert capsys.readouterr().out == ""


def test_recovery_swallows_store_and_alert_errors(monkeypatch, capsys):
    store = MagicMock()
    store.recover_interrupted_podcast_episodes.side_effect = RuntimeError("db locked")
    _recover_podcast_episodes(store)  # must not raise
    assert "db locked" in capsys.readouterr().out

    store.recover_interrupted_podcast_episodes.side_effect = None
    store.recover_interrupted_podcast_episodes.return_value = [
        {
            "source": "s",
            "guid": "a",
            "display_title": "A",
            "failure_count": 4,
            "status": "errored",
        },
        {
            "source": "s",
            "guid": "b",
            "display_title": "B",
            "failure_count": 4,
            "status": "errored",
        },
    ]
    sent = []

    def flaky(text, severity="info"):
        sent.append(text)
        if len(sent) == 1:
            raise RuntimeError("telegram down")
        return True

    monkeypatch.setattr("pipeline.alerts.send_alert", flaky)
    _recover_podcast_episodes(store)  # first alert raising must not skip the second
    assert len(sent) == 2


# --- consume_forever -------------------------------------------------------


def test_consume_forever_recovers_once_before_first_pull(monkeypatch, calls):
    store = MagicMock()
    store.list_due_the_rundown.return_value = []
    store.list_due_fp_digest.return_value = []
    order = []
    store.recover_interrupted_podcast_episodes.side_effect = lambda: (
        order.append("recover") or []
    )

    def pull(**kw):
        order.append("pull")
        if order.count("pull") >= 2:
            raise _Done
        return []

    mock_consumer = MagicMock()
    mock_consumer.pull.side_effect = pull
    monkeypatch.setattr(time, "sleep", lambda n: None)

    with patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer):
        with pytest.raises(_Done):
            consume_forever(store, MagicMock(), poll_interval=1)

    assert order[0] == "recover"
    assert order.count("recover") == 1
    # The tick ran after the first iteration's work (discovery + executor).
    assert len(calls["discover"]) == 1
    assert len(calls["execute"]) == 1
