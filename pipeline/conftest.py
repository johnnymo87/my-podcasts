"""Shared pytest fixtures for the pipeline test suite."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _block_real_telegram_posts(request):
    """Make "no test posts to production Telegram" a structural guarantee.

    ``PIGEON_DAEMON_URL`` defaults to ``http://127.0.0.1:4731`` (``pigeon.py``),
    and the pigeon daemon is genuinely listening on this host - so an unpatched
    ``send_alert`` in a test does not fail, it posts to the real Telegram
    channel. Today every alerting path in the suite is patched, but that is a
    property maintained by hand: any future test that leaves a stale daily job
    row in place would reach the real audit, send a real alert, and still pass
    green. This fixture removes that whole failure mode by severing the
    transport underneath.

    ``send_alert`` swallows every exception by design, so blocking here is safe
    for callers that do not patch it - they observe a ``False`` return, exactly
    as they would when the daemon is down.

    ``test_alerts.py`` and ``test_opencode_client.py`` patch this same target
    themselves to exercise the transport; their patches nest inside this one and
    take precedence, so they are unaffected.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return
    with patch(
        "pipeline.alerts.requests.post",
        side_effect=AssertionError(
            "Test attempted a real pigeon/Telegram POST. Patch "
            "pipeline.alerts.send_alert (or requests.post) in your test."
        ),
    ):
        yield
