"""Gemini account-wide quota/billing errors: recognise them and alert once.

A leaf module (stdlib, ``google.genai.errors`` and ``pipeline.alerts`` only).

HTTP 402 (prepayment credits depleted) and 429 (rate/quota) are properties of
the *account*, not of whatever job happened to hit them. Podcast reports
already defer on them (``podcast_executor``). The daily jobs (The Rundown, FP
Digest) simply fail and retry on their normal backoff, but a failure whose
cause is a billing problem is worth saying so out loud, once, instead of
waiting ~12 hours for the retry-exhaustion alert.

``account_quota_code`` recognises such an error through the wrappers the
pipeline puts around it (an editor raises ``RuntimeError(...) from <APIError>``).
``alert_account_quota`` sends at most one alert per 6 hours per process, except
that a 402 (billing, severity ``error``) always alerts after a latched 429
(a ``warning``): a passing rate limit must not mute a depleted balance.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from google.genai import errors as genai_errors

from pipeline import alerts


if TYPE_CHECKING:
    from collections.abc import Callable

    from pipeline.alerts import AlertResult

logger = logging.getLogger(__name__)

# 402: prepayment credits depleted. 429: rate/quota, after any SDK retries.
ACCOUNT_QUOTA_CODES = frozenset({402, 429})

QUOTA_REALERT = timedelta(hours=6)
ALERT_DETAIL_CHARS = 200
# How many exceptions of a chain are examined. Real chains are 2-3 deep; the
# bound (with a seen-set) only has to keep a pathological cycle from hanging.
_MAX_CHAIN_NODES = 8

# In-process state; a consumer restart clears it (at most one extra alert).
# The code is latched with the time so a 402 can escalate past a 429 warning.
_alerted_at: datetime | None = None
_alerted_code: int | None = None


def account_quota_code(exc: BaseException) -> int | None:
    """The quota code (402/429) of a Gemini ``APIError`` in ``exc``'s chain.

    Walks ``exc``, then its ``__cause__`` / ``__context__`` links (at most
    ``_MAX_CHAIN_NODES`` exceptions, each visited once). Only a real
    ``google.genai.errors.APIError`` with an int ``code`` in
    ``ACCOUNT_QUOTA_CODES`` counts: an unrelated exception that merely carries
    a ``.code`` attribute does not.
    """
    seen: set[int] = set()
    queue: list[BaseException] = [exc]
    while queue and len(seen) < _MAX_CHAIN_NODES:
        node = queue.pop(0)
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, genai_errors.APIError):
            code = node.code
            if isinstance(code, int) and code in ACCOUNT_QUOTA_CODES:
                return code
        for link in (node.__cause__, node.__context__):
            if link is not None:
                queue.append(link)
    return None


def alert_account_quota(
    exc: BaseException,
    where: str,
    *,
    now: datetime | None = None,
    sender: Callable[..., bool] | None = None,
) -> AlertResult | None:
    """Alert (bounded, at most once per ``QUOTA_REALERT``) on a quota error.

    Returns ``None`` -- and sends nothing -- when ``exc`` is not an account
    quota error or an alert went out less than ``QUOTA_REALERT`` ago. The one
    exception: a 402 always alerts when the latched alert was a 429, even within
    ``QUOTA_REALERT`` (severity escalation). The latch is not reset by success.
    Otherwise
    returns the bounded sender's result. The 6 h suppression starts only once an
    alert was delivered (``True``) or may still land (``"timeout"``); ``False``
    and ``"dropped"`` leave it unarmed so the next failure tries again.

    Never raises: this runs inside a job's failure handler.
    """
    global _alerted_at, _alerted_code
    try:
        code = account_quota_code(exc)
        if code is None:
            return None
        when = now if now is not None else datetime.now(UTC)
        escalation = code == 402 and _alerted_code == 429
        if (
            _alerted_at is not None
            and when - _alerted_at < QUOTA_REALERT
            and not escalation
        ):
            return None
        if code == 402:
            severity = "error"
            what = "billing error (HTTP 402, credits depleted)"
            check = "Check AI Studio billing"
        else:
            severity = "warning"
            what = f"quota/rate-limit error (HTTP {code})"
            check = "Check AI Studio quota and billing"
        text = (
            f"Gemini account {what} in {where}: {str(exc)[:ALERT_DETAIL_CHARS]}. "
            "The job retries automatically on its normal backoff (~12 h budget); "
            f"no restart needed. {check}."
        )
        result = alerts.send_alert_bounded(text, severity, sender=sender)
        if result is True or result == "timeout":
            _alerted_at = when
            _alerted_code = code
        logger.info("gemini quota alert for %s: result=%s", where, result)
        return result
    except Exception:  # noqa: BLE001 -- must never disturb a failure handler
        logger.exception("could not send the Gemini quota alert for %s", where)
        return None
