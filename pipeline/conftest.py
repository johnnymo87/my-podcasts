"""Shared pytest fixtures for the pipeline test suite."""

from __future__ import annotations

import builtins
import contextlib
import io
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


_PERSIST = "/persist"
_WRITE_MODE_CHARS = frozenset("wax+")


def _under_persist(path: object) -> bool:
    """Is ``path`` at or below /persist (``..`` resolved, symlinks not)?"""
    if isinstance(path, int):  # an already-open file descriptor
        return False
    try:
        text = os.path.abspath(os.fsdecode(os.fspath(path)))  # type: ignore[arg-type]
    except TypeError:
        return False
    return text == _PERSIST or text.startswith(_PERSIST + "/")


def _opens_for_write(mode: object) -> bool:
    return isinstance(mode, str) and not _WRITE_MODE_CHARS.isdisjoint(mode)


@contextlib.contextmanager
def persist_write_guard():
    """Refuse (and record) any write-open or mkdir under /persist.

    Patches the entry points a test could reach a write through: ``open``
    (builtin and ``io``), ``Path.open``/``write_text``/``write_bytes``/``mkdir``,
    ``os.mkdir`` and ``os.makedirs``. Reads are allowed. Every refusal is also
    appended to the yielded list so the caller can fail the test even when the
    code under test swallowed the ``AssertionError`` (manifest writes do, by
    design).

    Also refused: ``sqlite3.connect`` to a path there (it creates the file),
    ``os.open`` with a write/create flag, and ``Path.touch``.

    Not a sandbox. Known gaps, accepted: a subprocess; ``shutil`` helpers that
    go through ``os.open`` *read* flags then write by fd; a path spelled
    ``//persist/...`` (POSIX keeps two leading slashes distinct, ``abspath``
    does not collapse them); a relative path resolved against a cwd under
    /persist; a path reached through a symlink (``..`` is resolved lexically,
    symlinks are not); and a ``TMPDIR`` that points under /persist. It is a
    tripwire for the ordinary accidents (an unredirected archive root, a
    default cache dir, a state DB), not a defence against a hostile test.
    """
    violations: list[str] = []

    def refuse(what: str, path: object) -> AssertionError:
        message = (
            f"A test tried to {what} {os.fspath(path)!r} (under {_PERSIST}). "  # type: ignore[arg-type]
            "Redirect the path into tmp_path, or mark the test allow_persist."
        )
        violations.append(message)
        return AssertionError(message)

    real_open = builtins.open
    real_io_open = io.open
    real_path_open = Path.open
    real_write_text = Path.write_text
    real_write_bytes = Path.write_bytes
    real_mkdir = Path.mkdir
    real_os_mkdir = os.mkdir
    real_makedirs = os.makedirs
    real_os_open = os.open
    real_touch = Path.touch
    real_sqlite_connect = sqlite3.connect
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def guarded_open(file, mode="r", *args, **kwargs):
        if _opens_for_write(mode) and _under_persist(file):
            raise refuse("open for writing", file)
        return real_open(file, mode, *args, **kwargs)

    def guarded_io_open(file, mode="r", *args, **kwargs):
        if _opens_for_write(mode) and _under_persist(file):
            raise refuse("open for writing", file)
        return real_io_open(file, mode, *args, **kwargs)

    def guarded_path_open(self, mode="r", *args, **kwargs):
        if _opens_for_write(mode) and _under_persist(self):
            raise refuse("open for writing", self)
        return real_path_open(self, mode, *args, **kwargs)

    def guarded_write_text(self, *args, **kwargs):
        if _under_persist(self):
            raise refuse("write", self)
        return real_write_text(self, *args, **kwargs)

    def guarded_write_bytes(self, *args, **kwargs):
        if _under_persist(self):
            raise refuse("write", self)
        return real_write_bytes(self, *args, **kwargs)

    def guarded_mkdir(self, *args, **kwargs):
        if _under_persist(self):
            raise refuse("make directory", self)
        return real_mkdir(self, *args, **kwargs)

    def guarded_os_mkdir(path, *args, **kwargs):
        if _under_persist(path):
            raise refuse("make directory", path)
        return real_os_mkdir(path, *args, **kwargs)

    def guarded_makedirs(name, *args, **kwargs):
        if _under_persist(name):
            raise refuse("make directories", name)
        return real_makedirs(name, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & write_flags and _under_persist(path):
            raise refuse("open for writing", path)
        return real_os_open(path, flags, *args, **kwargs)

    def guarded_touch(self, *args, **kwargs):
        if _under_persist(self):
            raise refuse("touch", self)
        return real_touch(self, *args, **kwargs)

    def guarded_sqlite_connect(database, *args, **kwargs):
        if _under_persist(database):
            raise refuse("connect (creating) sqlite database", database)
        return real_sqlite_connect(database, *args, **kwargs)

    with (
        patch.object(os, "open", guarded_os_open),
        patch.object(Path, "touch", guarded_touch),
        patch.object(sqlite3, "connect", guarded_sqlite_connect),
        patch.object(builtins, "open", guarded_open),
        patch.object(io, "open", guarded_io_open),
        patch.object(Path, "open", guarded_path_open),
        patch.object(Path, "write_text", guarded_write_text),
        patch.object(Path, "write_bytes", guarded_write_bytes),
        patch.object(Path, "mkdir", guarded_mkdir),
        patch.object(os, "mkdir", guarded_os_mkdir),
        patch.object(os, "makedirs", guarded_makedirs),
    ):
        yield violations


@pytest.fixture(autouse=True)
def _block_persist_writes(request):
    """No test may create a file or directory under the host's real /persist.

    This is the structural form of bead ``my-podcasts-9p3.12``: a test that
    leaves a default path (the script archive, a cache or manifest dir) pointing
    at /persist passes on a dev box and pollutes -- or fails on -- a host that
    differs. Opt out with ``@pytest.mark.allow_persist`` (unused today).
    """
    if request.node.get_closest_marker("allow_persist"):
        yield []
        return
    with persist_write_guard() as violations:
        yield violations
    if violations:
        pytest.fail(
            f"{len(violations)} write(s) under {_PERSIST}; first: {violations[0]}",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _isolate_script_archive(tmp_path, monkeypatch):
    """``publish_script`` archives its inputs under ``SCRIPT_ARCHIVE_ROOT``.

    In production that is /persist/my-podcasts/scripts. Redirect it for every
    test so none depends on (or writes to) the host's real archive. A test that
    asserts on the archive patches the constant itself; that patch nests inside
    this one and wins.
    """
    monkeypatch.setattr(
        "pipeline.script_processor.SCRIPT_ARCHIVE_ROOT", tmp_path / "scripts"
    )


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
def _guard_violations():
    """Every refusal a ``_block_real_*`` guard makes, failed at teardown.

    A guard raises ``AssertionError`` at the call site, but plenty of production
    code catches ``Exception`` on purpose (``send_alert``, the article fetch, the
    Gemini phase's ``child_error`` path) and so would turn a real-API attempt
    into a green test. Recording the refusal here makes that impossible: the
    test fails at teardown whatever the code under test did with the exception.
    A test that *means* to provoke a refusal asserts on this list and clears it.
    """
    violations: list[str] = []
    yield violations
    if violations:
        pytest.fail(
            f"{len(violations)} guarded call(s) made by the test; "
            f"first: {violations[0]}",
            pytrace=False,
        )


def _refusal(violations: list[str], message: str):
    """A callable that records ``message`` in ``violations`` and raises it."""

    def refuse(*args, **kwargs):
        violations.append(message)
        raise AssertionError(message)

    return refuse


@pytest.fixture(autouse=True)
def _block_real_telegram_posts(request, _guard_violations):
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
        side_effect=_refusal(
            _guard_violations,
            "Test attempted a real pigeon/Telegram POST. Patch "
            "pipeline.alerts.send_alert (or requests.post) in your test.",
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _block_real_article_fetches(request, _guard_violations):
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
        side_effect=_refusal(
            _guard_violations,
            "A test made a real HTTP GET (outbound) through pipeline's requests "
            "module. Patch the fetch helper your code path uses (e.g. "
            "pipeline.fp_collector._extract_article_text).",
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _block_real_openai_tts(request, _guard_violations):
    """No test may build a real OpenAI client (a real TTS call costs money).

    Tests exercising the provider patch ``pipeline.tts.providers._make_openai_client``
    themselves; their patch nests inside this one and wins.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    refuse = _refusal(
        _guard_violations,
        "A test tried to build a real OpenAI client. Patch "
        "pipeline.tts.providers._make_openai_client or pipeline.tts.render_episode.",
    )
    with patch("pipeline.tts.providers._make_openai_client", refuse):
        yield


@pytest.fixture(autouse=True)
def _block_real_gemini_tts(request, _guard_violations):
    """No test may build a real Gemini TTS HTTP session (a real call costs money).

    Tests inject a fake via
    ``monkeypatch.setattr(providers, "_make_gemini_session", lambda: fake)``;
    their patch nests inside this one and wins.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    refuse = _refusal(
        _guard_violations,
        "A test tried to build a real Gemini TTS session. Patch "
        "pipeline.tts.providers._make_gemini_session.",
    )
    with patch("pipeline.tts.providers._make_gemini_session", refuse):
        yield


@pytest.fixture(autouse=True)
def _block_real_gemini_asr(request, _guard_violations):
    """No test may build a real Gemini client for TTS verification (costs money).

    Tests patch ``pipeline.tts.asr._make_genai_client`` themselves; their patch
    nests inside this one and wins.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    refuse = _refusal(
        _guard_violations,
        "A test tried to build a real Gemini ASR client. Patch "
        "pipeline.tts.asr._make_genai_client or pass a fake transcriber.",
    )
    with patch("pipeline.tts.asr._make_genai_client", refuse):
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


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    """``_default_state_db_path`` (``pipeline.__main__``) falls back to /persist."""
    monkeypatch.setenv("MY_PODCASTS_STATE_DB", str(tmp_path / "state.sqlite3"))


@pytest.fixture(autouse=True)
def _harmless_api_keys(monkeypatch):
    """No test process, nor any child it spawns, holds a real API key.

    A spawned child inherits ``os.environ``; if the developer's shell exports a
    real ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``, which the genai SDK also
    reads), a child whose fake is bypassed by a bug could spend money. Dummies
    make that request fail authentication instead. Tests that need a specific
    value set it themselves; their setenv wins.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-gemini-key-for-tests")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key-for-tests")
