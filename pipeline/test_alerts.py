from __future__ import annotations

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
