"""The child side of the Gemini phase, exercised in-process with fakes.

Nothing here spawns a process, touches the network, or sleeps for a backoff:
``sleep`` is injected, and the one test that uses the real abortable wait ends it
with the abort event after a few milliseconds.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from pipeline.tts import gemini_phase as gp
from pipeline.tts._phase_testing import factories, fake_chunk_text, fake_pcm
from pipeline.tts.asr import Transcription, TranscriptionUnavailable
from pipeline.tts.config import GeminiConfig
from pipeline.tts.providers import Synthesis, TTSProviderError


LEAF = GeminiConfig(model="fake-model", voice="Kore")
TEXT = fake_chunk_text(0)
OMITTED = " ".join(TEXT.split()[:8])


def _err(kind: str) -> TTSProviderError:
    return TTSProviderError(f"fake {kind} error", kind=kind)


def _asr_unavailable() -> TranscriptionUnavailable:
    return TranscriptionUnavailable("asr_error", "fake outage")


class Provider:
    """Scripted provider: each item is PCM bytes or an exception to raise."""

    def __init__(self, script, *, on_call=None):
        self._script = list(script)
        self.calls: list[tuple[str, float | None]] = []
        self._on_call = on_call

    def synthesize_detailed(self, text, cfg, *, timeout=None):
        self.calls.append((text, timeout))
        if self._on_call is not None:
            self._on_call(len(self.calls))
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return Synthesis(
            pcm=item,
            finish_reason="STOP",
            prompt_tokens=11,
            audio_tokens=22,
            elapsed_s=0.5,
        )


class Asr:
    """Scripted transcriber factory: items are transcript text or an exception."""

    def __init__(self, script):
        self._script = list(script)
        self.timeouts: list[float] = []
        self.calls = 0

    def make(self, timeout_s):
        self.timeouts.append(timeout_s)
        return self._transcribe

    def _transcribe(self, audio, mime):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return Transcription(
            text=item,
            model="fake-asr",
            prompt_version="0",
            finish_reason="STOP",
            elapsed_s=0.25,
            input_tokens=33,
            output_tokens=44,
            thinking_tokens=5,
        )


class Sleeps:
    def __init__(self, aborted: bool = False):
        self.seconds: list[float] = []
        self._aborted = aborted

    def __call__(self, abort, seconds):
        self.seconds.append(seconds)
        return self._aborted


@pytest.fixture
def scratch(tmp_path) -> Path:
    d = tmp_path / "scratch"
    d.mkdir()
    return d


@pytest.fixture
def release():
    """Blocking fakes wait on this; set at teardown so no thread leaks."""
    ev = threading.Event()
    yield ev
    ev.set()


def _far() -> float:
    return time.monotonic() + 1000.0


def _run(provider, asr, scratch, *, deadline=None, sleeps=None, abort=None, chunk=TEXT):
    sleeps = sleeps if sleeps is not None else Sleeps()
    abort = abort if abort is not None else threading.Event()
    rec = gp._render_chunk(
        0,
        chunk,
        LEAF,
        deadline if deadline is not None else _far(),
        scratch,
        provider,
        asr.make,
        abort,
        sleeps,
    )
    return rec, sleeps, abort


def _fail(provider, asr, scratch, **kw):
    with pytest.raises(gp._ChunkFailure) as info:
        _run(provider, asr, scratch, **kw)
    return info.value


def _progress(scratch, i=0) -> dict:
    return json.loads((scratch / gp.progress_name(i)).read_text())


PCM = fake_pcm(0)


# --- _render_chunk: the happy path and retries ---------------------------------


def test_clean_chunk_writes_pcm_and_reports_it(scratch):
    rec, sleeps, abort = _run(Provider([PCM]), Asr([TEXT]), scratch)
    assert rec == {
        "index": 0,
        "file": "chunk-0000.pcm",
        "bytes": len(PCM),
        "sha256": hashlib.sha256(PCM).hexdigest(),
    }
    assert (scratch / "chunk-0000.pcm").read_bytes() == PCM
    assert sleeps.seconds == []
    assert not abort.is_set()


def test_content_error_then_success_backs_off_once(scratch):
    provider = Provider([_err("content"), PCM])
    _, sleeps, _ = _run(provider, Asr([TEXT]), scratch)
    assert len(provider.calls) == 2
    assert sleeps.seconds == [2.0]


def test_two_transient_errors_then_success_back_off_2_then_8(scratch):
    provider = Provider([_err("infra"), _err("content"), PCM])
    _, sleeps, _ = _run(provider, Asr([TEXT]), scratch)
    assert len(provider.calls) == 3
    assert sleeps.seconds == [2.0, 8.0]


def test_three_transient_errors_exhaust_the_counter(scratch):
    provider = Provider([_err("infra"), _err("infra"), _err("content")])
    sleeps = Sleeps()
    failure = _fail(provider, Asr([]), scratch, sleeps=sleeps)
    assert failure.reason == gp.REASON_EXHAUSTED
    assert len(provider.calls) == 3
    assert sleeps.seconds == [2.0, 8.0]
    assert "fake content error" in failure.detail


def test_fatal_error_fails_at_once_and_sets_abort(scratch):
    provider = Provider([_err("fatal"), PCM])
    abort = threading.Event()
    sleeps = Sleeps()
    failure = _fail(provider, Asr([]), scratch, abort=abort, sleeps=sleeps)
    assert failure.reason == gp.REASON_FATAL
    assert len(provider.calls) == 1
    assert sleeps.seconds == []
    assert abort.is_set()


# --- verification --------------------------------------------------------------


def test_omission_then_pass_rerenders_without_backoff(scratch):
    provider = Provider([PCM, PCM])
    asr = Asr([OMITTED, TEXT])
    _, sleeps, _ = _run(provider, asr, scratch)
    assert (len(provider.calls), asr.calls) == (2, 2)
    assert sleeps.seconds == []


def test_second_omission_fails_the_chunk(scratch):
    provider = Provider([PCM, PCM, PCM])
    asr = Asr([OMITTED, OMITTED])
    failure = _fail(provider, asr, scratch)
    assert failure.reason == gp.REASON_SECOND_OMISSION
    assert (len(provider.calls), asr.calls) == (2, 2)


def test_omission_after_two_transient_errors_is_exhausted(scratch):
    provider = Provider([_err("infra"), _err("infra"), PCM])
    asr = Asr([OMITTED])
    failure = _fail(provider, asr, scratch)
    assert failure.reason == gp.REASON_EXHAUSTED
    assert (len(provider.calls), asr.calls) == (3, 1)


def test_asr_unavailable_is_not_retried(scratch):
    provider = Provider([PCM, PCM])
    asr = Asr([_asr_unavailable(), TEXT])
    abort = threading.Event()
    failure = _fail(provider, asr, scratch, abort=abort)
    assert failure.reason == gp.REASON_ASR_UNAVAILABLE
    assert (len(provider.calls), asr.calls) == (1, 1)
    assert abort.is_set()
    assert not (scratch / "chunk-0000.pcm").exists()


def test_only_verified_audio_reaches_the_scratch_dir(scratch):
    with pytest.raises(gp._ChunkFailure):
        _run(Provider([PCM, PCM]), Asr([OMITTED, OMITTED]), scratch)
    assert list(scratch.glob("chunk-*")) == []


# --- deadline and abort --------------------------------------------------------


def test_deadline_shorter_than_backoff_fails_without_sleeping(scratch):
    provider = Provider([_err("infra"), PCM])
    sleeps = Sleeps()
    failure = _fail(
        provider, Asr([TEXT]), scratch, deadline=time.monotonic() + 1.0, sleeps=sleeps
    )
    assert failure.reason == gp.REASON_DEADLINE
    assert sleeps.seconds == []
    assert len(provider.calls) == 1


def test_deadline_already_passed_makes_no_call(scratch):
    provider = Provider([PCM])
    failure = _fail(provider, Asr([TEXT]), scratch, deadline=time.monotonic() - 1.0)
    assert failure.reason == gp.REASON_DEADLINE
    assert provider.calls == []


def test_deadline_passing_before_asr_fails_the_chunk(scratch):
    # The clock is real, so let the deadline lapse from inside the synth call.
    deadline = time.monotonic() + 0.3
    provider = Provider([PCM], on_call=lambda n: time.sleep(0.35))
    asr = Asr([TEXT])
    failure = _fail(provider, asr, scratch, deadline=deadline)
    assert failure.reason == gp.REASON_DEADLINE
    assert asr.calls == 0


def test_synth_and_asr_timeouts_are_min_of_remaining_and_90(scratch):
    provider = Provider([PCM])
    asr = Asr([TEXT])
    _run(provider, asr, scratch, deadline=time.monotonic() + 500.0)
    assert provider.calls[0][1] == 90.0
    assert asr.timeouts == [90.0]


def test_timeouts_shrink_to_the_remaining_budget(scratch):
    provider = Provider([PCM])
    asr = Asr([TEXT])
    _run(provider, asr, scratch, deadline=time.monotonic() + 30.0)
    assert 29.0 < provider.calls[0][1] <= 30.0
    assert 29.0 < asr.timeouts[0] <= 30.0


def test_a_fresh_transcriber_is_built_per_verification(scratch):
    asr = Asr([OMITTED, TEXT])
    _run(Provider([PCM, PCM]), asr, scratch)
    assert len(asr.timeouts) == 2


def test_abort_set_before_start_makes_no_call(scratch):
    provider = Provider([PCM])
    abort = threading.Event()
    abort.set()
    with pytest.raises(gp._ChunkAborted):
        _run(provider, Asr([TEXT]), scratch, abort=abort)
    assert provider.calls == []


def test_abort_during_backoff_ends_it_early(scratch):
    provider = Provider([_err("infra"), PCM])
    abort = threading.Event()
    threading.Timer(0.05, abort.set).start()
    started = time.monotonic()
    with pytest.raises(gp._ChunkAborted):
        # The default sleep is the real abortable wait: an 8 s budget of backoff
        # must end in milliseconds.
        gp._render_chunk(
            0, TEXT, LEAF, _far(), scratch, provider, Asr([TEXT]).make, abort, None
        )
    assert time.monotonic() - started < 1.5
    assert len(provider.calls) == 1


def test_abort_after_a_backoff_makes_no_further_call(scratch):
    provider = Provider([_err("infra"), PCM])
    with pytest.raises(gp._ChunkAborted):
        _run(provider, Asr([TEXT]), scratch, sleeps=Sleeps(aborted=True))
    assert len(provider.calls) == 1


# --- progress records ----------------------------------------------------------


def test_attempt_is_recorded_as_started_before_the_request(scratch):
    seen: list[dict] = []
    provider = Provider([PCM], on_call=lambda n: seen.append(_progress(scratch)))
    _run(provider, Asr([TEXT]), scratch)
    (during,) = seen
    (attempt,) = during["attempts"]
    assert attempt["synth"]["status"] == "started"
    assert attempt["synth"]["prompt_tokens"] is None
    assert attempt["synth"]["audio_tokens"] is None
    assert attempt["asr"] is None


def test_asr_attempt_is_recorded_as_started_before_the_request(scratch):
    seen: list[dict] = []
    asr = Asr([TEXT])
    real_make = asr.make

    def make(timeout):
        seen.append(_progress(scratch))
        return real_make(timeout)

    gp._render_chunk(
        0,
        TEXT,
        LEAF,
        _far(),
        scratch,
        Provider([PCM]),
        make,
        threading.Event(),
        Sleeps(),
    )
    (attempt,) = seen[0]["attempts"]
    assert attempt["synth"]["status"] == "ok"
    assert attempt["asr"]["status"] == "started"
    assert attempt["asr"]["input_tokens"] is None


def test_finished_attempt_holds_synth_and_asr_tokens_separately(scratch):
    _run(Provider([PCM]), Asr([TEXT]), scratch)
    (attempt,) = _progress(scratch)["attempts"]
    assert attempt["synth"]["status"] == "ok"
    assert (attempt["synth"]["prompt_tokens"], attempt["synth"]["audio_tokens"]) == (
        11,
        22,
    )
    assert attempt["synth"]["pcm_bytes"] == len(PCM)
    assert attempt["asr"]["status"] == "pass"
    assert (
        attempt["asr"]["input_tokens"],
        attempt["asr"]["output_tokens"],
        attempt["asr"]["thinking_tokens"],
    ) == (33, 44, 5)
    assert attempt["outcome"] == "verified"


def test_discarded_omission_attempt_keeps_its_tokens(scratch):
    _run(Provider([PCM, PCM]), Asr([OMITTED, TEXT]), scratch)
    first, second = _progress(scratch)["attempts"]
    assert first["outcome"] == "omission"
    assert first["synth"]["audio_tokens"] == 22
    assert first["asr"]["status"] == "omission"
    assert first["asr"]["output_tokens"] == 44
    assert second["outcome"] == "verified"


def test_failed_request_is_recorded_with_unknown_tokens(scratch):
    with pytest.raises(gp._ChunkFailure):
        _run(Provider([_err("fatal")]), Asr([]), scratch)
    (attempt,) = _progress(scratch)["attempts"]
    assert attempt["synth"]["status"] == "error"
    assert attempt["synth"]["kind"] == "fatal"
    assert "fake fatal error" in attempt["synth"]["error"]
    assert attempt["synth"]["prompt_tokens"] is None
    assert attempt["outcome"] == "fatal"


def test_asr_unavailable_attempt_is_recorded(scratch):
    with pytest.raises(gp._ChunkFailure):
        _run(Provider([PCM]), Asr([_asr_unavailable()]), scratch)
    (attempt,) = _progress(scratch)["attempts"]
    assert attempt["asr"]["status"] == "unavailable"
    assert attempt["asr"]["input_tokens"] is None
    assert attempt["outcome"] == "asr_unavailable"


# --- atomic writes -------------------------------------------------------------


def test_atomic_write_replaces_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "x.bin"
    gp._atomic_write(target, b"one")
    gp._atomic_write(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in tmp_path.iterdir()] == ["x.bin"]


def test_atomic_write_never_exposes_a_partial_file(tmp_path, monkeypatch):
    target = tmp_path / "x.bin"
    gp._atomic_write(target, b"old")

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(gp, "_replace", boom)
    with pytest.raises(OSError):
        gp._atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["x.bin"]


# --- _run_child ----------------------------------------------------------------


def test_run_child_all_chunks_ok(scratch):
    chunks = [fake_chunk_text(i) for i in range(3)]
    result = gp._run_child(chunks, LEAF, _far(), scratch, factories("ok"))
    assert result["status"] == "ok"
    assert result["reason"] is None
    assert result["failed_chunk"] is None
    assert [c["index"] for c in result["chunks"]] == [0, 1, 2]
    for c in result["chunks"]:
        data = (scratch / c["file"]).read_bytes()
        assert data == fake_pcm(c["index"])
        assert c["bytes"] == len(data)
        assert c["sha256"] == hashlib.sha256(data).hexdigest()
    assert json.loads((scratch / gp.RESULT_NAME).read_text()) == result


def test_run_child_fatal_chunk_fails_the_phase(scratch):
    chunks = [fake_chunk_text(i) for i in range(3)]
    result = gp._run_child(
        chunks, LEAF, _far(), scratch, factories("fatal_on_chunk", chunk=1)
    )
    assert result["status"] == "failed"
    assert result["reason"] == gp.REASON_FATAL
    assert result["failed_chunk"] == 1
    assert "chunks" not in result
    assert json.loads((scratch / gp.RESULT_NAME).read_text()) == result


def test_run_child_omission_chunk_fails_with_second_omission(scratch):
    chunks = [fake_chunk_text(i) for i in range(2)]
    result = gp._run_child(
        chunks, LEAF, _far(), scratch, factories("omission_on_chunk", chunk=1)
    )
    assert result["reason"] == gp.REASON_SECOND_OMISSION
    assert result["failed_chunk"] == 1


class BlockingProvider:
    """Chunk 0 fails fatally; every other chunk blocks until released."""

    def __init__(self, release: threading.Event, *, fatal_chunk: int | None = 0):
        self._release = release
        self._fatal = fatal_chunk
        self.entered = threading.Event()

    def synthesize_detailed(self, text, cfg, *, timeout=None):
        i = int(text.split()[1])
        if i == self._fatal:
            self.entered.wait(5)  # the sibling is now in flight
            raise _err("fatal")
        self.entered.set()
        self._release.wait(30)
        return Synthesis(fake_pcm(i), "STOP", None, None, 0.0)


def test_run_child_returns_on_early_fatal_without_waiting_for_a_blocked_sibling(
    scratch, release
):
    provider = BlockingProvider(release)
    chunks = [fake_chunk_text(i) for i in range(2)]
    started = time.monotonic()
    result = gp._run_child(
        chunks, LEAF, _far(), scratch, (lambda: provider, Asr([]).make)
    )
    assert time.monotonic() - started < 5.0
    assert not release.is_set()  # the sibling really was still blocked
    assert (result["status"], result["reason"], result["failed_chunk"]) == (
        "failed",
        gp.REASON_FATAL,
        0,
    )
    assert json.loads((scratch / gp.RESULT_NAME).read_text()) == result


def test_run_child_reports_deadline_while_every_chunk_is_blocked(scratch, release):
    provider = BlockingProvider(release, fatal_chunk=None)
    chunks = [fake_chunk_text(i) for i in range(2)]
    started = time.monotonic()
    result = gp._run_child(
        chunks,
        LEAF,
        time.monotonic() + 0.3,
        scratch,
        (lambda: provider, Asr([]).make),
    )
    assert time.monotonic() - started < 5.0
    assert result["status"] == "failed"
    assert result["reason"] == gp.REASON_DEADLINE


def test_unexpected_exception_in_a_chunk_is_child_error(scratch):
    class Broken:
        def synthesize_detailed(self, text, cfg, *, timeout=None):
            raise RuntimeError("kaboom in chunk")

    chunks = [fake_chunk_text(i) for i in range(2)]
    result = gp._run_child(chunks, LEAF, _far(), scratch, (Broken, Asr([]).make))
    assert result["status"] == "failed"
    assert result["reason"] == gp.REASON_CHILD_ERROR
    assert "RuntimeError" in result["detail"]
    assert "kaboom in chunk" in result["detail"]
    assert result["failed_chunk"] in (0, 1)
    assert json.loads((scratch / gp.RESULT_NAME).read_text()) == result


def test_provider_construction_failure_is_child_error(scratch):
    def make_provider():
        raise ValueError("no key")

    result = gp._run_child([TEXT], LEAF, _far(), scratch, (make_provider, Asr([]).make))
    assert result["reason"] == gp.REASON_CHILD_ERROR
    assert "no key" in result["detail"]
    assert result["failed_chunk"] is None


def test_no_chunks_is_child_error(scratch):
    result = gp._run_child([], LEAF, _far(), scratch, factories("ok"))
    assert result["reason"] == gp.REASON_CHILD_ERROR


def test_run_child_closes_the_provider_it_built(scratch):
    closed = []

    class P(Provider):
        def close(self):
            closed.append(True)

    result = gp._run_child(
        [TEXT], LEAF, _far(), scratch, (lambda: P([PCM]), Asr([TEXT]).make)
    )
    assert result["status"] == "ok"
    assert closed == [True]


def test_failure_reasons_are_the_closed_set():
    assert gp.FALLBACK_REASONS == frozenset(
        {
            "fatal",
            "exhausted",
            "deadline",
            "asr_unavailable",
            "second_omission",
            "child_error",
            "child_no_result",
            "invalid_result",
            "spawn_failed",
        }
    )


# --- _child_main ---------------------------------------------------------------


class _Exited(BaseException):
    def __init__(self, code):
        self.code = code


@pytest.fixture
def child_env(monkeypatch):
    """Patch the hard exit and the watchdog so a test can call _child_main."""
    watchdogs: list[tuple] = []

    def fake_exit(code):
        raise _Exited(code)

    monkeypatch.setattr(gp, "_hard_exit", fake_exit)
    monkeypatch.setattr(gp, "_start_watchdog", lambda *a, **k: watchdogs.append(a))
    return watchdogs


def test_child_main_runs_watchdog_then_bootstrap_then_factories(
    scratch, child_env, monkeypatch
):
    order: list[str] = []
    monkeypatch.setattr(gp, "_start_watchdog", lambda *a, **k: order.append("watchdog"))

    def make_provider():
        order.append("factory")
        return Provider([PCM])

    with pytest.raises(_Exited) as info:
        gp._child_main(
            [TEXT],
            LEAF,
            _far(),
            str(scratch),
            4242,
            (make_provider, Asr([TEXT]).make),
            lambda: order.append("bootstrap"),
        )
    assert info.value.code == 0
    # The bootstrap (network denial) must precede anything that could build a
    # client; the watchdog precedes both so a hung bootstrap is bounded.
    assert order == ["watchdog", "bootstrap", "factory"]
    assert json.loads((scratch / gp.RESULT_NAME).read_text())["status"] == "ok"


def test_child_main_hands_the_parent_pid_to_the_watchdog(scratch, child_env):
    with pytest.raises(_Exited):
        gp._child_main([TEXT], LEAF, _far(), str(scratch), 4242, factories("ok"), None)
    assert child_env[0][0] == 4242


def test_child_main_exits_even_on_a_base_exception(scratch, child_env):
    def bootstrap():
        raise KeyboardInterrupt

    with pytest.raises(_Exited) as info:
        gp._child_main(
            [TEXT], LEAF, _far(), str(scratch), 1, factories("ok"), bootstrap
        )
    assert info.value.code == 1  # no result was written: the parent sees no_result
    assert not (scratch / gp.RESULT_NAME).exists()


def test_child_main_flushes_std_streams_before_exiting(scratch, child_env, monkeypatch):
    events: list[str] = []

    class Stream:
        def __init__(self, name):
            self.name = name

        def write(self, text):
            return len(text)

        def flush(self):
            events.append(f"flush-{self.name}")

    monkeypatch.setattr(gp.sys, "stdout", Stream("out"))
    monkeypatch.setattr(gp.sys, "stderr", Stream("err"))
    real_exit = gp._hard_exit

    def recording_exit(code):
        events.append("exit")
        real_exit(code)

    monkeypatch.setattr(gp, "_hard_exit", recording_exit)
    with pytest.raises(_Exited):
        gp._child_main([TEXT], LEAF, _far(), str(scratch), 1, factories("ok"), None)
    assert events[-3:] == ["flush-out", "flush-err", "exit"]


def test_child_main_bootstrap_failure_is_child_error(scratch, child_env):
    def bootstrap():
        raise RuntimeError("bootstrap broke")

    with pytest.raises(_Exited) as info:
        gp._child_main(
            [TEXT], LEAF, _far(), str(scratch), 1, factories("ok"), bootstrap
        )
    assert info.value.code == 0
    result = json.loads((scratch / gp.RESULT_NAME).read_text())
    assert result["reason"] == gp.REASON_CHILD_ERROR
    assert "bootstrap broke" in result["detail"]


def test_child_main_exits_nonzero_when_the_result_cannot_be_written(
    scratch, child_env, monkeypatch
):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(gp, "_atomic_write", boom)
    with pytest.raises(_Exited) as info:
        gp._child_main([TEXT], LEAF, _far(), str(scratch), 1, factories("ok"), None)
    assert info.value.code == 1


def test_child_main_arguments_are_picklable(scratch):
    import pickle

    args = ([TEXT], LEAF, 123.0, str(scratch), 1, factories("ok"), None)
    assert pickle.loads(pickle.dumps(args))[0] == [TEXT]


# --- watchdog ------------------------------------------------------------------


def _watch(**kw):
    exits: list[int] = []
    gp._watchdog_loop(
        kw.pop("parent_pid", 100),
        kw.pop("deadline", _far()),
        poll_s=0.001,
        getppid=kw.pop("getppid", lambda: 100),
        clock=kw.pop("clock", time.monotonic),
        exit_fn=exits.append,
        stop=kw.pop("stop", None),
    )
    return exits


def test_watchdog_exits_3_when_the_parent_changes():
    ppids = iter([100, 100, 1])
    assert _watch(getppid=lambda: next(ppids)) == [3]


def test_watchdog_exits_3_after_the_deadline_plus_grace():
    now = [0.0]

    def clock():
        now[0] += 0.5
        return now[0]

    assert _watch(deadline=2.0, clock=clock) == [3]
    assert now[0] >= 2.0 + gp.WATCHDOG_GRACE_SECONDS


def test_watchdog_leaves_a_healthy_child_alone():
    stop = threading.Event()
    calls = [0]

    def getppid():
        calls[0] += 1
        if calls[0] > 5:
            stop.set()
        return 100

    assert _watch(getppid=getppid, stop=stop) == []


# --- review follow-ups ---------------------------------------------------------


def test_a_siblings_failure_beats_a_spurious_deadline_in_the_backoff_branch(scratch):
    # Chunk fails transiently with 1 s left (< the 2 s backoff), but a sibling
    # has already failed: this chunk must yield, not report its own "deadline".
    abort = threading.Event()
    provider = Provider([_err("infra")], on_call=lambda n: abort.set())
    with pytest.raises(gp._ChunkAborted):
        _run(
            provider,
            Asr([]),
            scratch,
            abort=abort,
            deadline=time.monotonic() + 1.0,
        )


def test_a_siblings_failure_beats_exhausted(scratch):
    abort = threading.Event()
    provider = Provider(
        [_err("infra")] * 3, on_call=lambda n: abort.set() if n == 3 else None
    )
    with pytest.raises(gp._ChunkAborted):
        _run(provider, Asr([]), scratch, abort=abort)


def test_a_worker_base_exception_is_child_error_not_a_full_budget_deadline(scratch):
    class Hard(BaseException):
        pass

    class Broken:
        def synthesize_detailed(self, text, cfg, *, timeout=None):
            raise Hard("worker died")

    started = time.monotonic()
    # A short deadline, so a regression fails in seconds rather than hanging.
    result = gp._run_child(
        [TEXT, fake_chunk_text(1)],
        LEAF,
        time.monotonic() + 5.0,
        scratch,
        (Broken, Asr([]).make),
    )
    assert time.monotonic() - started < 3.0
    assert result["reason"] == gp.REASON_CHILD_ERROR
    assert "Hard" in result["detail"] and "worker died" in result["detail"]


def test_progress_is_persisted_before_the_chunk_pcm(scratch, monkeypatch):
    seen: list[dict] = []
    real = gp._atomic_write

    def spy(path, data):
        if Path(path).name == gp.chunk_name(0):
            seen.append(_progress(scratch))
        real(path, data)

    monkeypatch.setattr(gp, "_atomic_write", spy)
    _run(Provider([PCM]), Asr([TEXT]), scratch)
    (during,) = seen
    (attempt,) = during["attempts"]
    assert attempt["outcome"] == "verified"
    assert attempt["asr"]["status"] == "pass"


def test_a_progress_write_failure_does_not_fail_the_chunk(scratch, monkeypatch, capsys):
    real = gp._atomic_write_json

    def flaky(path, obj):
        if Path(path).name.startswith("progress-"):
            raise OSError("progress disk hiccup")
        real(path, obj)

    monkeypatch.setattr(gp, "_atomic_write_json", flaky)
    rec, _, _ = _run(Provider([PCM]), Asr([TEXT]), scratch)
    assert (scratch / rec["file"]).read_bytes() == PCM
    assert "progress disk hiccup" in capsys.readouterr().err


def test_a_chunk_pcm_write_failure_is_not_swallowed(scratch, monkeypatch):
    real = gp._atomic_write

    def flaky(path, data):
        if Path(path).suffix == ".pcm":
            raise OSError("pcm disk full")
        real(path, data)

    monkeypatch.setattr(gp, "_atomic_write", flaky)
    with pytest.raises(OSError, match="pcm disk full"):
        _run(Provider([PCM]), Asr([TEXT]), scratch)


def test_child_error_detail_carries_the_traceback_and_is_logged(scratch, capsys):
    class Broken:
        def synthesize_detailed(self, text, cfg, *, timeout=None):
            raise RuntimeError("kaboom")

    result = gp._run_child([TEXT], LEAF, _far(), scratch, (Broken, Asr([]).make))
    assert "Traceback" in result["detail"]
    assert "synthesize_detailed" in result["detail"]
    assert result["detail"].rstrip().endswith("RuntimeError: kaboom")
    assert "RuntimeError: kaboom" in capsys.readouterr().err


def test_child_error_detail_is_capped_but_keeps_the_exception_line(scratch):
    class Broken:
        def synthesize_detailed(self, text, cfg, *, timeout=None):
            raise RuntimeError("x" * 6000 + " the-end")

    result = gp._run_child([TEXT], LEAF, _far(), scratch, (Broken, Asr([]).make))
    detail = result["detail"]
    assert len(detail) <= gp.ERROR_DETAIL_CHARS + 50
    assert detail.endswith("the-end")


def test_traceback_keeps_the_exception_line_when_capped():
    try:
        raise ValueError("the important message")
    except ValueError as exc:
        detail = gp._describe(exc)
    assert detail.rstrip().endswith("ValueError: the important message")
