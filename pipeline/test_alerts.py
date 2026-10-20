from __future__ import annotations

import threading
import time

import requests

from pipeline import alerts


class _Resp:
    def __init__(self, status_code=204):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300


def test_posts_to_alert_endpoint(monkeypatch):
    captured = {}

    def _post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Resp(204)

    monkeypatch.setattr(alerts.requests, "post", _post)
    monkeypatch.delenv("PIGEON_DAEMON_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("PIGEON_DAEMON_AUTH_TOKEN_FILE", "/nonexistent")

    assert alerts.send_alert("hello", severity="info") is True
    assert captured["url"].endswith("/alert")
    assert captured["json"] == {"text": "hello", "severity": "info"}
    assert "Authorization" not in captured["headers"]


def test_includes_bearer_when_token_configured(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        alerts.requests, "post", lambda url, **kw: (captured.update(kw), _Resp())[1]
    )
    monkeypatch.setenv("PIGEON_DAEMON_AUTH_TOKEN", "sekrit")
    alerts.send_alert("hello")
    assert captured["headers"]["Authorization"] == "Bearer sekrit"


def test_returns_false_on_timeout(monkeypatch):
    def _boom(*a, **kw):
        raise requests.Timeout("slow")

    monkeypatch.setattr(alerts.requests, "post", _boom)
    assert alerts.send_alert("hello") is False


def test_returns_false_on_non_2xx(monkeypatch):
    monkeypatch.setattr(alerts.requests, "post", lambda *a, **kw: _Resp(503))
    assert alerts.send_alert("hello") is False


# --- send_alert_bounded ------------------------------------------------------


def test_bounded_returns_the_senders_answer():
    seen = []

    def sender(text, severity="info"):
        seen.append((text, severity, threading.current_thread().daemon))
        return True

    assert alerts.send_alert_bounded("hi", "error", sender=sender) is True
    assert seen == [("hi", "error", True)]  # ran on a daemon thread
    assert alerts.send_alert_bounded("hi", sender=lambda t, severity: False) is False


def test_bounded_returns_timeout_without_waiting_for_the_sender():
    release = threading.Event()
    started = threading.Event()

    def hung(text, severity="info"):
        started.set()
        release.wait(30)
        return True

    try:
        t0 = time.monotonic()
        result = alerts.send_alert_bounded("hi", timeout_s=0.2, sender=hung)
        elapsed = time.monotonic() - t0
        assert result == "timeout"
        assert started.is_set()
        assert elapsed < 5.0
    finally:
        release.set()


def test_bounded_returns_false_when_the_sender_raises(caplog):
    def boom(text, severity="info"):
        raise RuntimeError("pigeon down")

    assert alerts.send_alert_bounded("the text", sender=boom) is False
    assert "the text" in caplog.text  # journald keeps what we failed to send


def test_bounded_default_sender_is_looked_up_at_call_time(monkeypatch):
    calls = []
    monkeypatch.setattr(
        alerts, "send_alert", lambda text, severity="info": calls.append(text) or True
    )
    assert alerts.send_alert_bounded("x") is True
    assert calls == ["x"]


# --- the one shared worker ---------------------------------------------------


def _worker_threads():
    return [t for t in threading.enumerate() if t.name == "bounded-alert-worker"]


class _Blocker:
    """A sender that blocks until released; counts what it was asked to send."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.texts = []

    def __call__(self, text, severity="info"):
        self.texts.append(text)
        self.started.set()
        self.release.wait(30)
        return True


def test_a_blocked_sender_times_out_and_the_next_alert_queues_behind_it():
    blocker = _Blocker()
    try:
        assert (
            alerts.send_alert_bounded("a", timeout_s=0.1, sender=blocker) == "timeout"
        )
        assert blocker.started.is_set()
        # The worker is stuck in "a": "b" is queued, not run, and times out too.
        assert (
            alerts.send_alert_bounded("b", timeout_s=0.1, sender=blocker) == "timeout"
        )
        assert blocker.texts == ["a"]
    finally:
        blocker.release.set()
        alerts._stop_alert_worker()


def test_a_full_queue_drops_the_alert_and_logs_its_text(caplog):
    blocker = _Blocker()
    try:
        assert (
            alerts.send_alert_bounded("first", timeout_s=0.1, sender=blocker)
            == "timeout"
        )
        # "first" holds the worker; the next ALERT_QUEUE_MAX fill the queue.
        for i in range(alerts.ALERT_QUEUE_MAX):
            assert (
                alerts.send_alert_bounded(f"q{i}", timeout_s=0.01, sender=blocker)
                == "timeout"
            )
        t0 = time.monotonic()
        result = alerts.send_alert_bounded("overflow-text", timeout_s=5, sender=blocker)
        assert result == "dropped"
        assert time.monotonic() - t0 < 1.0  # no wait, no new thread
        assert "overflow-text" in caplog.text
        assert "overflow-text" not in blocker.texts
    finally:
        blocker.release.set()
        alerts._stop_alert_worker()


def test_exactly_one_worker_thread_exists_after_many_calls():
    blocker = _Blocker()
    try:
        for i in range(alerts.ALERT_QUEUE_MAX + 5):
            alerts.send_alert_bounded(f"m{i}", timeout_s=0.01, sender=blocker)
        assert len(_worker_threads()) == 1
    finally:
        blocker.release.set()
        alerts._stop_alert_worker()
    assert _worker_threads() == []


def test_a_dead_worker_is_replaced():
    assert alerts.send_alert_bounded("one", sender=lambda t, severity: True) is True
    first = alerts._alert_worker
    alerts._alert_queue.put(alerts._STOP)  # kill it the way production never does
    first.join(5)
    assert not first.is_alive()
    assert alerts.send_alert_bounded("two", sender=lambda t, severity: True) is True
    assert alerts._alert_worker is not first
    assert alerts._alert_worker.is_alive()
    assert len(_worker_threads()) == 1


def test_a_raising_sender_does_not_kill_the_worker():
    def boom(text, severity="info"):
        raise RuntimeError("x")

    assert alerts.send_alert_bounded("a", sender=boom) is False
    worker = alerts._alert_worker
    assert alerts.send_alert_bounded("b", sender=lambda t, severity: True) is True
    assert alerts._alert_worker is worker
