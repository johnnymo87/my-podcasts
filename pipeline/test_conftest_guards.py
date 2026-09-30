"""The autouse guards in ``pipeline/conftest.py`` guard what they claim to.

The ``/persist`` write guard is the structural version of bead ``my-podcasts-9p3.12``:
a test that archives a script (or writes a manifest, cache entry, ...) to the
host's real ``/persist`` passes on a dev box and fails -- or worse, pollutes
production state -- elsewhere.

Every probe path below sits under a directory that does not exist, so even with
the guard off (``allow_persist``) nothing can be created.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest


_MISSING = "/persist/guard-test-nonexistent-dir"


def _refused(violations: list[str], fn) -> None:
    with pytest.raises(AssertionError, match="/persist"):
        fn()
    assert violations, "a refused write must also be recorded for teardown"
    violations.clear()  # we asserted on it; don't fail this test at teardown


def test_open_for_write_under_persist_is_refused(_block_persist_writes):
    for mode in ("w", "a", "x", "wb", "ab", "r+", "w+b"):
        _refused(_block_persist_writes, lambda m=mode: open(f"{_MISSING}/f", m))
    _refused(_block_persist_writes, lambda: io.open(f"{_MISSING}/f", "w"))  # noqa: UP020
    _refused(_block_persist_writes, lambda: Path(f"{_MISSING}/f").open("w"))


def test_path_helpers_under_persist_are_refused(_block_persist_writes):
    p = Path(_MISSING) / "x"
    _refused(_block_persist_writes, lambda: p.write_text("x"))
    _refused(_block_persist_writes, lambda: p.write_bytes(b"x"))
    _refused(_block_persist_writes, lambda: p.mkdir(parents=True))
    _refused(_block_persist_writes, lambda: os.makedirs(f"{_MISSING}/a/b"))
    _refused(_block_persist_writes, lambda: os.mkdir(f"{_MISSING}/a"))


def test_sqlite_os_open_and_touch_under_persist_are_refused(_block_persist_writes):
    import sqlite3

    _refused(
        _block_persist_writes, lambda: sqlite3.connect(f"{_MISSING}/state.sqlite3")
    )
    for flags in (os.O_WRONLY, os.O_RDWR, os.O_RDONLY | os.O_CREAT):
        _refused(_block_persist_writes, lambda f=flags: os.open(f"{_MISSING}/f", f))
    _refused(_block_persist_writes, lambda: Path(f"{_MISSING}/f").touch())


def test_os_open_for_reading_under_persist_is_allowed(_block_persist_writes):
    with pytest.raises(FileNotFoundError):
        os.open(f"{_MISSING}/f", os.O_RDONLY)
    assert _block_persist_writes == []


def test_dotdot_does_not_escape_the_guard(_block_persist_writes):
    _refused(
        _block_persist_writes,
        lambda: open(f"/tmp/../persist/{_MISSING.rsplit('/', 1)[1]}/f", "w"),
    )


def test_reads_under_persist_are_allowed(_block_persist_writes):
    with pytest.raises(FileNotFoundError):
        open(f"{_MISSING}/f")
    with pytest.raises(FileNotFoundError):
        open(f"{_MISSING}/f", "rb")
    assert not Path(f"{_MISSING}/f").exists()
    assert _block_persist_writes == []


def test_writes_outside_persist_are_allowed(tmp_path, _block_persist_writes):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f.txt").write_text("hi")
    os.makedirs(tmp_path / "c" / "d")
    with open(tmp_path / "c" / "d" / "g", "wb") as f:
        f.write(b"x")
    assert _block_persist_writes == []


def test_swallowed_refusal_is_still_recorded():
    """Code that catches ``Exception`` around a write must not hide the violation.

    The fixture fails the test at teardown from this record, so a production
    path that swallows the AssertionError (manifest writes do, by design) cannot
    turn a /persist write green.
    """
    from pipeline.conftest import persist_write_guard

    with persist_write_guard() as violations:
        try:
            open(f"{_MISSING}/f", "w")
        except Exception:
            pass
    assert len(violations) == 1
    assert "/persist" in violations[0]


@pytest.mark.allow_persist
def test_allow_persist_marker_turns_the_guard_off():
    # The guard is off, so the real filesystem answers (the directory is absent).
    with pytest.raises(FileNotFoundError):
        open(f"{_MISSING}/f", "w")


def test_script_archive_root_is_redirected_off_persist(tmp_path):
    from pipeline import script_processor

    root = Path(script_processor.SCRIPT_ARCHIVE_ROOT)
    assert root == tmp_path / "scripts"
    assert not str(root).startswith("/persist")


def test_state_db_env_is_redirected_off_persist(tmp_path):
    from pipeline.__main__ import _default_state_db_path

    assert _default_state_db_path() == tmp_path / "state.sqlite3"


def test_no_api_key_is_in_the_environment():
    for var in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "OPENAI_API_KEY",
    ):
        assert var not in os.environ, var


# --- the _block_real_* guards record, so a swallowed refusal still fails -------


def _swallowing(call) -> None:
    """What production code that catches ``Exception`` around a call does."""
    try:
        call()
    except Exception:  # noqa: BLE001 -- the point of the test
        pass


def _openai():
    from pipeline.tts import providers

    providers._make_openai_client(1.0)


def _gemini_tts():
    from pipeline.tts import providers

    providers._make_gemini_session()


def _gemini_asr():
    from pipeline.tts import asr

    asr._make_genai_client(1.0)


def _telegram():
    from pipeline import alerts

    alerts.requests.post("http://127.0.0.1:1/")


def _genai_client():
    from google import genai

    genai.Client(api_key="k")


def _article_fetch():
    from pipeline import fp_collector

    fp_collector.requests.get("http://127.0.0.1:1/")


@pytest.mark.parametrize(
    "call",
    [_openai, _gemini_tts, _gemini_asr, _telegram, _article_fetch, _genai_client],
    ids=lambda f: f.__name__.strip("_"),
)
def test_a_swallowed_guard_refusal_is_still_recorded(call, _guard_violations):
    _swallowing(call)
    assert len(_guard_violations) == 1
    _guard_violations.clear()  # we asserted on it; don't fail this test at teardown


def test_recorded_guard_violation_fails_the_test_at_teardown(tmp_path):
    """End to end: the teardown really turns a recorded violation into a failure."""
    conftest = Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    start = conftest.index("@pytest.fixture(autouse=True)\ndef _guard_violations")
    end = conftest.index("@pytest.fixture(autouse=True)\ndef _block_real_telegram")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "conftest.py").write_text("import pytest\n\n\n" + conftest[start:end])
    (proj / "test_it.py").write_text(
        "def test_swallows(_guard_violations):\n    _guard_violations.append('boom')\n"
    )
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "test_it.py"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode != 0
    assert "1 passed" in out.stdout and "1 error" in out.stdout
    assert "1 guarded call(s)" in out.stdout and "boom" in out.stdout


def test_genai_client_aimed_at_loopback_is_let_through():
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key="k", http_options=types.HttpOptions(base_url="http://127.0.0.1:9")
    )
    client.close()


def test_genai_client_stub_by_a_test_wins_over_the_guard(_guard_violations):
    from unittest.mock import patch

    with patch("google.genai.Client") as stub:
        from google import genai

        genai.Client(api_key="k")
    assert stub.called
    assert _guard_violations == []
