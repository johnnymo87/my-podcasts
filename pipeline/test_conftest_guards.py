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
