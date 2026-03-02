from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from pipeline.consumer import consume_forever


class _Done(BaseException):
    """Sentinel used to break out of consume_forever's infinite loop in tests.
    Inherits from BaseException (not Exception) so it bypasses except-Exception clauses."""


def test_consume_forever_retries_on_pull_exception(monkeypatch) -> None:
    """Verify that a transient error from consumer.pull() triggers sleep-and-retry
    instead of crashing consume_forever()."""
    store = MagicMock()
    store.list_due_things_happen.return_value = []
    r2_client = MagicMock()

    call_count = 0

    def flaky_pull(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a 502 / transient API error on first call
            raise Exception("502 Bad Gateway")
        # Second call: exit the infinite loop via a BaseException sentinel
        raise _Done("done")

    mock_consumer = MagicMock()
    mock_consumer.pull.side_effect = flaky_pull

    slept = []
    monkeypatch.setattr(time, "sleep", lambda n: slept.append(n))

    with patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    # Should have made 2 pull calls (first failed, second succeeded and raised _Done)
    assert call_count == 2, f"Expected 2 pull calls, got {call_count}"
    # Should have slept once after the first failure
    assert 5 in slept, f"Expected sleep(5) after failure, got slept={slept}"
