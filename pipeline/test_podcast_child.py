"""``podcast_child`` without spawning: the child's body, its watchdog, and the
parent's handling of a (fake) process."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

import pytest

from pipeline import podcast_child as pc
from pipeline import podcast_report
from pipeline._podcast_child_testing import SCRIPT_TEXT, StubResult
from pipeline.podcast_feed import parse_podcast_feed
from pipeline.podcast_sources import get_podcast_source
from pipeline.podcast_transcribe import TranscriptionError


FIXTURE = Path(__file__).parent / "fixtures" / "argument_feed.xml"
GUID = "audioboom-01a13c3d-ae49-7326-a550-f82c9eff9be1"


@pytest.fixture
def source():
    return get_podcast_source("argument")


@pytest.fixture
def item():
    return next(
        i
        for i in parse_podcast_feed(FIXTURE.read_text(encoding="utf-8"))
        if i.guid == GUID
    )


@pytest.fixture
def work(tmp_path, source, item) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    (d / pc.JOB_NAME).write_text(
        json.dumps(podcast_report.item_metadata(source, item)), encoding="utf-8"
    )
    return d


def _read(work: Path, name: str) -> dict:
    return json.loads((work / name).read_text(encoding="utf-8"))


def test_body_rebuilds_the_item_and_writes_an_ok_result(work, source, item):
    seen: dict = {}

    def runner(src, it, wd, *, on_stage=None):
        seen.update(source=src, item=it, work_dir=wd)
        on_stage("claim")
        on_stage("write")
        script = wd / "script.txt"
        script.write_text(SCRIPT_TEXT, encoding="utf-8")
        return StubResult(script_path=script, piece_count=9, input_tokens=5)

    pc._child_body("argument", work, runner)

    assert seen["source"] == source
    assert seen["item"] == item  # frozen dataclass equality: every field survived
    assert seen["work_dir"] == work
    assert _read(work, pc.RESULT_NAME) == {
        "ok": True,
        "script_sha256": hashlib.sha256(SCRIPT_TEXT.encode()).hexdigest(),
        "piece_count": 9,
        "input_tokens": 5,
        "output_tokens": 200,
    }
    assert _read(work, pc.STAGE_NAME) == {"stage": "write"}
    assert not list(work.glob("*.tmp"))


def test_transcription_error_carries_reason_code_and_last_stage(work):
    def runner(src, it, wd, *, on_stage=None):
        on_stage("transcribe")
        raise TranscriptionError("key rejected", reason="asr_error", code=400)

    pc._child_body("argument", work, runner)

    assert _read(work, pc.RESULT_NAME) == {
        "ok": False,
        "stage": "transcribe",
        "reason": "asr_error",
        "code": 400,
        "detail": "key rejected",
    }


def test_other_exception_uses_its_type_name(work):
    def runner(src, it, wd, *, on_stage=None):
        on_stage("claim")
        raise RuntimeError("work dir belongs to someone else")

    pc._child_body("argument", work, runner)

    res = _read(work, pc.RESULT_NAME)
    assert (res["ok"], res["stage"], res["reason"], res["code"]) == (
        False,
        "claim",
        "RuntimeError",
        None,
    )
    assert "belongs to someone else" in res["detail"]


def test_failure_before_any_stage_reports_the_child_stage(work):
    def runner(src, it, wd, *, on_stage=None):
        raise ValueError("boom")

    pc._child_body("argument", work, runner)
    assert _read(work, pc.RESULT_NAME)["stage"] == "child"


@pytest.mark.parametrize(
    "content", [None, "not json", "[1, 2]", json.dumps({"guid": "x"})]
)
def test_missing_or_invalid_job_is_bad_job(work, content):
    job = work / pc.JOB_NAME
    if content is None:
        job.unlink()
    else:
        job.write_text(content, encoding="utf-8")

    def runner(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("runner must not run without a valid job")

    pc._child_body("argument", work, runner)

    res = _read(work, pc.RESULT_NAME)
    assert (res["ok"], res["stage"], res["reason"]) == (False, "job", "bad_job")


def test_unknown_source_is_bad_job(work):
    pc._child_body("nope", work, lambda *a, **k: None)
    res = _read(work, pc.RESULT_NAME)
    assert (res["stage"], res["reason"]) == ("job", "bad_job")


def test_runner_returning_no_script_is_an_error_not_an_ok(work):
    # A runner that "succeeds" without a script it can point at is not ok.
    def runner(src, it, wd, *, on_stage=None):
        return StubResult(script_path=wd / "missing.txt")

    pc._child_body("argument", work, runner)
    res = _read(work, pc.RESULT_NAME)
    assert res["ok"] is False and res["reason"] == "FileNotFoundError"


def test_a_stale_result_is_replaced_not_merged(work):
    (work / pc.RESULT_NAME).write_text(json.dumps({"ok": True}))
    pc._child_body("argument", work, lambda *a, **k: 1 / 0)
    assert _read(work, pc.RESULT_NAME)["reason"] == "ZeroDivisionError"


def test_resolve_runner():
    fn = pc._resolve_runner("pipeline._podcast_child_testing:happy_runner")
    from pipeline import _podcast_child_testing as t

    assert fn is t.happy_runner
    with pytest.raises((ValueError, AttributeError, ImportError)):
        pc._resolve_runner("no-colon")
    with pytest.raises(AttributeError):
        pc._resolve_runner("pipeline._podcast_child_testing:nope")


def test_watchdog_exits_when_the_parent_changes():
    codes: list[int] = []
    pc._watchdog_loop(
        100,
        deadline=1e18,
        poll_s=0.0,
        getppid=lambda: 1,
        clock=lambda: 0.0,
        exit_fn=codes.append,
    )
    assert codes == [3]


def test_watchdog_exits_after_deadline_plus_grace():
    codes: list[int] = []
    now = {"t": 0.0}

    def clock():
        now["t"] += pc.WATCHDOG_GRACE_SECONDS  # 10 s deadline passes on the 11th poll
        return now["t"]

    pc._watchdog_loop(
        100,
        deadline=10.0,
        poll_s=0.0,
        getppid=lambda: 100,
        clock=clock,
        exit_fn=codes.append,
    )
    assert codes == [3]
    assert now["t"] > 10.0 + pc.WATCHDOG_GRACE_SECONDS


def test_watchdog_stops_when_told_to():
    stop = threading.Event()
    stop.set()
    codes: list[int] = []
    pc._watchdog_loop(
        100,
        deadline=0.0,
        poll_s=0.0,
        getppid=lambda: 1,
        clock=lambda: 1e9,
        exit_fn=codes.append,
        stop=stop,
    )
    assert codes == []


def test_child_logging_keeps_signed_urls_out_of_the_log(monkeypatch):
    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
    pc._configure_child_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# --- the parent, against a fake process --------------------------------------


class FakeProc:
    """Stands in for a ``multiprocessing`` process; ``on_poll`` runs on each join."""

    def __init__(self, *, alive=True, on_join=None, start_exc=None, exitcode=None):
        self.alive = alive
        self.on_join = on_join
        self.start_exc = start_exc
        self.exitcode = exitcode
        self.killed = False
        self.pid = 4242

    def start(self):
        if self.start_exc is not None:
            raise self.start_exc

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        if self.on_join is not None:
            self.on_join()

    def kill(self):
        self.killed = True
        self.alive = False


def _patch_proc(monkeypatch, proc):
    seen: dict = {}

    def make(args):
        seen["args"] = args
        return proc

    monkeypatch.setattr(pc, "_make_process", make)
    return seen


def test_parent_writes_job_and_clears_stale_files(monkeypatch, tmp_path, source, item):
    work = tmp_path / "w"
    work.mkdir()
    (work / pc.RESULT_NAME).write_text(json.dumps({"ok": True}))
    (work / pc.STAGE_NAME).write_text(json.dumps({"stage": "write"}))
    proc = FakeProc(alive=False, exitcode=1)
    seen = _patch_proc(monkeypatch, proc)

    out = pc.run_episode_in_child(source, item, work, deadline_s=5.0)

    assert (out.ok, out.reason, out.stage) == (False, "child_no_result", "child")
    assert not (work / pc.RESULT_NAME).exists()
    assert json.loads((work / pc.JOB_NAME).read_text()) == podcast_report.item_metadata(
        source, item
    )
    key, wd, deadline, ppid, runner, bootstrap = seen["args"]
    assert (key, wd, runner, bootstrap) == (
        "argument",
        str(work),
        "pipeline.podcast_report:run_episode_to_script",
        None,
    )
    assert isinstance(deadline, float)


def test_spawn_failure_is_reported(monkeypatch, tmp_path, source, item):
    _patch_proc(monkeypatch, FakeProc(start_exc=OSError("fork failed")))
    out = pc.run_episode_in_child(source, item, tmp_path / "w", deadline_s=5.0)
    assert (out.ok, out.reason, out.stage) == (False, "spawn_failed", "child")
    assert "fork failed" in out.detail


def test_keyboard_interrupt_kills_the_child_and_propagates(
    monkeypatch, tmp_path, source, item
):
    def interrupt():
        raise KeyboardInterrupt

    proc = FakeProc(on_join=interrupt)
    _patch_proc(monkeypatch, proc)
    with pytest.raises(KeyboardInterrupt):
        pc.run_episode_in_child(source, item, tmp_path / "w", deadline_s=5.0)
    assert proc.killed


def test_result_landing_as_the_clock_expires_wins(monkeypatch, tmp_path, source, item):
    work = tmp_path / "w"
    script = "late but whole\n"

    def land():
        # Only visible once the child has been killed at the deadline.
        if proc.killed:
            (work / "script.txt").write_text(script)
            (work / pc.RESULT_NAME).write_text(
                json.dumps(
                    {
                        "ok": True,
                        "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
                        "piece_count": 1,
                        "input_tokens": 2,
                        "output_tokens": 3,
                    }
                )
            )

    proc = FakeProc()
    orig_kill = proc.kill

    def kill_then_land():
        orig_kill()
        land()

    proc.kill = kill_then_land
    _patch_proc(monkeypatch, proc)

    out = pc.run_episode_in_child(source, item, work, deadline_s=0.0)
    assert out.ok and out.result["piece_count"] == 1
    assert proc.killed


def test_deadline_names_the_stage_from_the_stage_file(
    monkeypatch, tmp_path, source, item
):
    work = tmp_path / "w"
    proc = FakeProc()
    _patch_proc(monkeypatch, proc)

    def write_stage():
        work.mkdir(exist_ok=True)
        (work / pc.STAGE_NAME).write_text(json.dumps({"stage": "transcribe"}))

    proc.on_join = write_stage
    out = pc.run_episode_in_child(source, item, work, deadline_s=0.0)
    assert (out.ok, out.reason, out.stage) == (False, "deadline", "transcribe")
    assert out.elapsed_s >= 0


@pytest.mark.parametrize("payload", ["not json", "[]", '"x"'])
def test_unparseable_result_is_invalid(monkeypatch, tmp_path, source, item, payload):
    work = tmp_path / "w"
    proc = FakeProc()
    _patch_proc(monkeypatch, proc)

    def land():
        (work / pc.RESULT_NAME).write_text(payload)

    proc.on_join = land
    out = pc.run_episode_in_child(source, item, work, deadline_s=5.0)
    assert (out.ok, out.reason) == (False, "invalid_result")
    assert proc.killed  # never leaves the child running


def test_ok_result_with_missing_or_empty_script_is_invalid(
    monkeypatch, tmp_path, source, item
):
    work = tmp_path / "w"
    proc = FakeProc()
    _patch_proc(monkeypatch, proc)

    def land():
        (work / "script.txt").write_text("")
        (work / pc.RESULT_NAME).write_text(
            json.dumps(
                {
                    "ok": True,
                    "script_sha256": hashlib.sha256(b"").hexdigest(),
                    "piece_count": 1,
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
            )
        )

    proc.on_join = land
    out = pc.run_episode_in_child(source, item, work, deadline_s=5.0)
    assert (out.ok, out.reason) == (False, "invalid_result")


def test_not_ok_result_keeps_stage_reason_code(monkeypatch, tmp_path, source, item):
    work = tmp_path / "w"
    proc = FakeProc()
    _patch_proc(monkeypatch, proc)

    def land():
        (work / pc.RESULT_NAME).write_text(
            json.dumps(
                {
                    "ok": False,
                    "stage": "claim",
                    "reason": "RuntimeError",
                    "code": None,
                    "detail": "owned by another",
                }
            )
        )

    proc.on_join = land
    out = pc.run_episode_in_child(source, item, work, deadline_s=5.0)
    assert (out.ok, out.stage, out.reason, out.code, out.detail) == (
        False,
        "claim",
        "RuntimeError",
        None,
        "owned by another",
    )
