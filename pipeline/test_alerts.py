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


def test_bounded_returns_none_on_timeout_without_waiting_for_the_sender():
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
        assert result is None
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
