from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from google.genai import errors as genai_errors

from pipeline import gemini_quota


def _api_error(code: int) -> genai_errors.APIError:
    return genai_errors.APIError(code, {"error": {"message": "credits depleted"}})


T0 = datetime(2026, 10, 22, 4, 30, tzinfo=UTC)


class _Sender:
    def __init__(self, *results):
        self.results = list(results) or [True]
        self.calls: list[tuple[str, str]] = []

    def __call__(self, text, severity="info"):
        self.calls.append((text, severity))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return result


# --- account_quota_code -------------------------------------------------------


def test_account_quota_codes_constant() -> None:
    assert gemini_quota.ACCOUNT_QUOTA_CODES == frozenset({402, 429})


@pytest.mark.parametrize(
    ("code", "expected"), [(402, 402), (429, 429), (400, None), (500, None)]
)
def test_account_quota_code_for_api_errors(code, expected) -> None:
    assert gemini_quota.account_quota_code(_api_error(code)) == expected


def test_account_quota_code_via_cause() -> None:
    api = _api_error(402)
    try:
        try:
            raise api
        except Exception as e:
            raise RuntimeError("Rundown editor: Gemini call failed") from e
    except RuntimeError as wrapped:
        assert gemini_quota.account_quota_code(wrapped) == 402


def test_account_quota_code_via_context() -> None:
    try:
        try:
            raise _api_error(429)
        except Exception:
            raise RuntimeError("while handling")  # noqa: B904 -- implicit __context__
    except RuntimeError as wrapped:
        assert wrapped.__cause__ is None
        assert gemini_quota.account_quota_code(wrapped) == 429


def test_account_quota_code_ignores_non_api_errors_with_a_code() -> None:
    class Fake(Exception):
        code = 402

    assert gemini_quota.account_quota_code(Fake("looks like it")) is None
    assert gemini_quota.account_quota_code(RuntimeError("plain")) is None


def test_account_quota_code_rejects_non_int_code() -> None:
    err = _api_error(402)
    err.code = "402"  # type: ignore[assignment]
    assert gemini_quota.account_quota_code(err) is None


def test_account_quota_code_survives_a_cycle() -> None:
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert gemini_quota.account_quota_code(a) is None


def test_account_quota_code_bounded_depth() -> None:
    api = _api_error(402)
    chain: BaseException = api
    for i in range(20):
        outer = RuntimeError(f"layer {i}")
        outer.__cause__ = chain
        chain = outer
    # Buried deeper than the walk limit: not found, and no hang.
    assert gemini_quota.account_quota_code(chain) is None


# --- alert_account_quota ------------------------------------------------------


def test_alert_sends_nothing_for_a_non_quota_error() -> None:
    sender = _Sender()
    assert (
        gemini_quota.alert_account_quota(
            RuntimeError("boom"), "The Rundown", now=T0, sender=sender
        )
        is None
    )
    assert sender.calls == []
    assert gemini_quota._alerted_at is None


def test_alert_402_is_an_error_with_billing_text() -> None:
    sender = _Sender()
    exc = RuntimeError("Rundown editor: Gemini call failed")
    exc.__cause__ = _api_error(402)
    result = gemini_quota.alert_account_quota(exc, "The Rundown", now=T0, sender=sender)
    assert result is True
    ((text, severity),) = sender.calls
    assert severity == "error"
    assert "billing error (HTTP 402, credits depleted)" in text
    assert "in The Rundown" in text
    assert "Check AI Studio billing" in text
    assert "retries automatically" in text


def test_alert_429_is_a_warning_with_quota_text() -> None:
    sender = _Sender()
    gemini_quota.alert_account_quota(
        _api_error(429), "FP Digest", now=T0, sender=sender
    )
    ((text, severity),) = sender.calls
    assert severity == "warning"
    assert "quota/rate-limit error (HTTP 429)" in text
    assert "in FP Digest" in text
    assert "Check AI Studio quota and billing" in text


def test_alert_detail_is_capped() -> None:
    sender = _Sender()
    err = genai_errors.APIError(402, {"error": {"message": "x" * 1000}})
    gemini_quota.alert_account_quota(err, "The Rundown", now=T0, sender=sender)
    ((text, _),) = sender.calls
    # The detail is the first 200 chars of str(exc); the 1000-char message
    # therefore cannot appear in full.
    assert str(err)[:200] in text
    assert str(err)[:201] not in text


def test_second_alert_within_six_hours_is_suppressed_then_resumes() -> None:
    sender = _Sender()
    err = _api_error(402)
    assert gemini_quota.alert_account_quota(err, "A", now=T0, sender=sender) is True
    assert (
        gemini_quota.alert_account_quota(
            err, "B", now=T0 + timedelta(hours=5, minutes=59), sender=sender
        )
        is None
    )
    assert len(sender.calls) == 1
    assert (
        gemini_quota.alert_account_quota(
            err, "C", now=T0 + timedelta(hours=6), sender=sender
        )
        is True
    )
    assert len(sender.calls) == 2


def test_undelivered_alert_does_not_latch() -> None:
    sender = _Sender(False, True)
    err = _api_error(402)
    assert gemini_quota.alert_account_quota(err, "A", now=T0, sender=sender) is False
    assert gemini_quota._alerted_at is None
    assert (
        gemini_quota.alert_account_quota(
            err, "A", now=T0 + timedelta(minutes=1), sender=sender
        )
        is True
    )
    assert len(sender.calls) == 2


def test_dropped_alert_does_not_latch(monkeypatch) -> None:
    monkeypatch.setattr(
        gemini_quota.alerts, "send_alert_bounded", lambda *a, **k: "dropped"
    )
    assert gemini_quota.alert_account_quota(_api_error(402), "A", now=T0) == "dropped"
    assert gemini_quota._alerted_at is None


def test_timeout_alert_latches(monkeypatch) -> None:
    monkeypatch.setattr(
        gemini_quota.alerts, "send_alert_bounded", lambda *a, **k: "timeout"
    )
    assert gemini_quota.alert_account_quota(_api_error(402), "A", now=T0) == "timeout"
    assert gemini_quota._alerted_at == T0
    sender = _Sender()
    assert (
        gemini_quota.alert_account_quota(
            _api_error(402), "A", now=T0 + timedelta(hours=1), sender=sender
        )
        is None
    )


def test_alert_never_raises(monkeypatch) -> None:
    def boom(*a, **k):
        raise ValueError("pigeon exploded")

    monkeypatch.setattr(gemini_quota.alerts, "send_alert_bounded", boom)
    assert gemini_quota.alert_account_quota(_api_error(402), "A", now=T0) is None
    assert gemini_quota._alerted_at is None


def test_alert_default_now_is_used(monkeypatch) -> None:
    sender = _Sender()
    assert gemini_quota.alert_account_quota(_api_error(402), "A", sender=sender) is True
    assert gemini_quota._alerted_at is not None
    assert gemini_quota._alerted_at.tzinfo is not None


def test_402_after_a_429_alerts_even_within_six_hours() -> None:
    """Severity escalation: a 429 warning must not mute the billing error."""
    sender = _Sender()
    assert (
        gemini_quota.alert_account_quota(_api_error(429), "A", now=T0, sender=sender)
        is True
    )
    assert (
        gemini_quota.alert_account_quota(
            _api_error(402), "B", now=T0 + timedelta(minutes=5), sender=sender
        )
        is True
    )
    assert [sev for _, sev in sender.calls] == ["warning", "error"]
    # The 402 is now the latched code, so a repeat 402 is suppressed again.
    assert (
        gemini_quota.alert_account_quota(
            _api_error(402), "C", now=T0 + timedelta(minutes=10), sender=sender
        )
        is None
    )
    assert len(sender.calls) == 2


def test_429_after_a_402_stays_suppressed_within_six_hours() -> None:
    sender = _Sender()
    assert (
        gemini_quota.alert_account_quota(_api_error(402), "A", now=T0, sender=sender)
        is True
    )
    assert (
        gemini_quota.alert_account_quota(
            _api_error(429), "B", now=T0 + timedelta(minutes=5), sender=sender
        )
        is None
    )
    assert len(sender.calls) == 1
    # ...and the 6 h rule still applies to it afterwards.
    assert (
        gemini_quota.alert_account_quota(
            _api_error(429), "C", now=T0 + timedelta(hours=6), sender=sender
        )
        is True
    )


def test_undelivered_429_does_not_arm_the_escalation_state() -> None:
    sender = _Sender(False, True)
    assert (
        gemini_quota.alert_account_quota(_api_error(429), "A", now=T0, sender=sender)
        is False
    )
    assert gemini_quota._alerted_code is None
    assert (
        gemini_quota.alert_account_quota(
            _api_error(429), "A", now=T0 + timedelta(minutes=1), sender=sender
        )
        is True
    )
