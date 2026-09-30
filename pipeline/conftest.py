"""Shared pytest fixtures for the pipeline test suite."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


def _install_fake_render(monkeypatch) -> tuple[list[dict], list[str]]:
    """Stub ``pipeline.tts.render_episode``; return (calls, texts).

    Each call records ``text``, ``config`` and the keyword args, and writes a
    small fake mp3 to ``out_mp3`` so callers can stat/upload it. Call sites
    must invoke ``tts.render_episode`` through the ``pipeline.tts`` module
    attribute for this patch to reach them.
    """
    from pipeline.tts import RenderResult

    calls: list[dict] = []
    texts: list[str] = []

    def fake(text, config, out_mp3, **kwargs):
        calls.append({"text": text, "config": config, "out_mp3": out_mp3, **kwargs})
        texts.append(text)
        Path(out_mp3).write_bytes(b"\xff\xfb\x90\x00" * 100)
        return RenderResult(
            provider=config.primary.provider,
            config=config,
            rendered=config.primary,
            cached=False,
            chunks=1,
            manifest_path=None,
        )

    monkeypatch.setattr("pipeline.tts.render_episode", fake)
    return calls, texts


@pytest.fixture
def fake_tts_render(monkeypatch) -> list[dict]:
    """Stub the renderer; return the list of recorded calls.

    Does not stub ``ffprobe``: tests that need a duration patch
    ``subprocess.run`` themselves. Use this *or* ``captured_tts_input``,
    never both.
    """
    calls, _ = _install_fake_render(monkeypatch)
    return calls


@pytest.fixture
def captured_tts_input(monkeypatch) -> list[str]:
    """Stub the renderer and ``ffprobe`` (60 s); return texts handed to TTS.

    Shared by every test that asserts on the exact text handed to TTS (the
    title-prelude tests in ``test_processor_prelude.py`` and
    ``test_blog_poller.py``). The capture is at the ``render_episode``
    boundary, so it sees exactly what the renderer would have been given.

    Deliberately does *not* patch feed regeneration or R2 upload -- callers
    differ on which module they import ``regenerate_and_upload_feed`` into
    and whether they need it patched at all, so that stays call-site-local.
    """
    _, texts = _install_fake_render(monkeypatch)

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="60.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    return texts


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


@pytest.fixture(autouse=True)
def _block_real_openai_tts(request):
    """No test may build a real OpenAI client (a real TTS call costs money).

    Tests exercising the provider patch ``pipeline.tts.providers._make_openai_client``
    themselves; their patch nests inside this one and wins.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    def _refuse(timeout: float):
        raise AssertionError(
            "A test tried to build a real OpenAI client. Patch "
            "pipeline.tts.providers._make_openai_client or pipeline.tts.render_episode."
        )

    with patch("pipeline.tts.providers._make_openai_client", _refuse):
        yield


@pytest.fixture(autouse=True)
def _block_real_gemini_asr(request):
    """No test may build a real Gemini client for TTS verification (costs money).

    Tests patch ``pipeline.tts.asr._make_genai_client`` themselves; their patch
    nests inside this one and wins.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    def _refuse(timeout_s: float):
        raise AssertionError(
            "A test tried to build a real Gemini ASR client. Patch "
            "pipeline.tts.asr._make_genai_client or pass a fake transcriber."
        )

    with patch("pipeline.tts.asr._make_genai_client", _refuse):
        yield


@pytest.fixture(autouse=True)
def _isolate_tts_state_dirs(tmp_path, monkeypatch):
    """No test may write TTS manifests or cache entries under /persist.

    ``render_episode`` resolves its default dirs at call time, so patching the
    module constants is enough to redirect every caller that omits the kwargs.
    """
    monkeypatch.setattr(
        "pipeline.tts.manifest.DEFAULT_MANIFEST_DIR", tmp_path / "tts-renders"
    )
    monkeypatch.setattr("pipeline.tts.cache.DEFAULT_CACHE_DIR", tmp_path / "tts-cache")
