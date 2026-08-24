"""Shared pytest fixtures for the pipeline test suite."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def captured_tts_input(monkeypatch) -> list[str]:
    """Stub ``ttsjoin``/``ffprobe``; return the list ttsjoin's input lands in.

    Shared by every test that asserts on the exact text handed to TTS (the
    title-prelude tests in ``test_processor_prelude.py`` and
    ``test_blog_poller.py``). Reads ``--input-file`` before the caller's
    tempdir is torn down, and looks up flags by name
    (``cmd.index("--input-file") + 1``) rather than position, so a future
    reordering of a caller's ttsjoin invocation doesn't silently break the
    capture.

    Deliberately does *not* patch feed regeneration or R2 upload -- callers
    differ on which module they import ``regenerate_and_upload_feed`` into
    and whether they need it patched at all, so that stays call-site-local.
    """
    captured: list[str] = []

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            input_file = Path(cmd[cmd.index("--input-file") + 1])
            captured.append(input_file.read_text(encoding="utf-8"))
            output_file = Path(cmd[cmd.index("--output-file") + 1])
            output_file.write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="60.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    return captured


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


@pytest.fixture(autouse=True)
def _block_real_article_fetches(request):
    """No test may fetch a real article over HTTP.

    ``fp_collector`` fetches article bodies during collection. Its fetch helper
    swallows every exception and returns "" (the degrade-to-excerpt path), so an
    unpatched fetch in a test does not fail — it makes a real outbound request to
    whatever hostname the fixture invented, and the test still passes green.
    Severing the transport makes that impossible rather than merely discouraged.

    Tests that exercise fetching patch ``pipeline.fp_collector._extract_article_text``,
    which sits above this and takes precedence.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return
    with patch(
        "pipeline.fp_collector.requests.get",
        side_effect=AssertionError(
            "A test made a real HTTP GET (outbound) through pipeline's requests "
            "module. Patch the fetch helper your code path uses (e.g. "
            "pipeline.fp_collector._extract_article_text)."
        ),
    ):
        yield
