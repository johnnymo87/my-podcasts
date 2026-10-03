"""The child side of the Gemini phase, exercised in-process with fakes.

Nothing here spawns a process, touches the network, or sleeps for a backoff:
``sleep`` is injected, and the one test that uses the real abortable wait ends it
with the abort event after a few milliseconds.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from pipeline.tts import asr as asr_module
from pipeline.tts import gemini_phase as gp
from pipeline.tts._phase_testing import (
    factories,
    fake_chunk_text,
    fake_pcm,
    make_chunks,
)
from pipeline.tts.asr import (
    ASR_BLOCKED,
    AsrUsage,
    GeminiTranscriber,
    Transcription,
    TranscriptionUnavailable,
)
from pipeline.tts.config import GeminiConfig
from pipeline.tts.providers import Synthesis, TTSProviderError


LEAF = GeminiConfig(model="fake-model", voice="Kore")
TEXT = fake_chunk_text(0)
OMITTED = " ".join(TEXT.split()[:8])


def _err(kind: str) -> TTSProviderError:
    return TTSProviderError(f"fake {kind} error", kind=kind)


def _asr_unavailable() -> TranscriptionUnavailable:
    return TranscriptionUnavailable("asr_error", "fake outage")


def _blocked() -> TranscriptionUnavailable:
    return TranscriptionUnavailable(ASR_BLOCKED, "no candidates (block_reason=OTHER)")


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
        self.audio: list[bytes] = []  # what each request was sent

    def make(self, timeout_s):
        self.timeouts.append(timeout_s)
        return self._transcribe

    def _transcribe(self, audio, mime):
        self.calls += 1
        self.audio.append(audio)
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


def main_child(
    scratch, *, parent_pid=1, fac=None, bootstrap=None, deadline=None, chunks=(TEXT,)
):
    """Write the child's input file, then call the spawn target in-process."""
    gp._write_input(scratch, list(chunks), LEAF)
    gp._child_main(
        str(scratch),
        deadline if deadline is not None else _far(),
        parent_pid,
        fac or factories("ok"),
        bootstrap,
    )


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


# --- a blocked ASR check is re-asked on the same audio (9p3.17) -------------------

PCM_SECOND = fake_pcm(7)  # a second synth's audio, told apart from PCM


def _attempts(scratch) -> list[dict]:
    return _progress(scratch)["attempts"]


def test_block_then_recheck_pass_reuses_the_same_audio_with_one_synth(scratch):
    provider = Provider([PCM, PCM_SECOND])
    asr = Asr([_blocked(), TEXT])
    rec, sleeps, abort = _run(provider, asr, scratch)
    assert (len(provider.calls), asr.calls) == (1, 2)
    assert asr.audio[0] == asr.audio[1] == gp.pcm_to_wav(PCM)  # byte-identical
    assert sleeps.seconds == [2.0]
    assert not abort.is_set()
    assert (scratch / "chunk-0000.pcm").read_bytes() == PCM
    assert rec["sha256"] == hashlib.sha256(PCM).hexdigest()
    first, second = _attempts(scratch)
    assert first["n"] == 1 and first["outcome"] == "asr_blocked"
    assert first["synth"]["status"] == "ok"
    assert first["asr"]["status"] == "unavailable"
    assert first["asr"]["reasons"] == ["asr_blocked"]
    assert second["n"] == 2 and second["synth"] is None
    assert second["recheck_of"] == 1
    assert second["outcome"] == "verified" and second["asr"]["status"] == "pass"


def _blocked_with_usage(inp=1777, out=0, think=None, elapsed=2.5):
    # What GeminiTranscriber raises for a blocked response (9p3.18): the probe's
    # 1777 prompt tokens, no candidates count.
    return TranscriptionUnavailable(
        ASR_BLOCKED,
        "no candidates (block_reason=OTHER)",
        AsrUsage(elapsed, inp, out, think),
    )


def test_a_blocked_attempt_records_its_usage_and_the_phase_totals_stay_known(scratch):
    from pipeline.tts import render

    asr = Asr([_blocked_with_usage(), TEXT])
    _run(Provider([PCM]), asr, scratch)
    first, second = _attempts(scratch)
    assert first["outcome"] == "asr_blocked" and first["asr"]["status"] == "unavailable"
    assert first["asr"]["elapsed_s"] == 2.5
    assert first["asr"]["input_tokens"] == 1777
    assert first["asr"]["output_tokens"] == 0
    assert first["asr"]["thinking_tokens"] is None
    assert second["outcome"] == "verified"
    tokens, _ = render._phase_totals([_progress(scratch)])
    # blocked (1777 / 0 / none reported = 0) + re-check (33 / 44 / 5)
    assert tokens["asr_input"] == 1777 + 33
    assert tokens["asr_output"] == 0 + 44
    assert tokens["asr_thinking"] == 0 + 5
    assert tokens["synth_prompt"] == 11 and tokens["synth_audio"] == 22


def test_a_blocked_attempt_whose_usage_is_unknown_makes_totals_null(scratch):
    from pipeline.tts import render

    asr = Asr([_blocked(), TEXT])  # no usage on the exception
    _run(Provider([PCM]), asr, scratch)
    tokens, _ = render._phase_totals([_progress(scratch)])
    assert tokens["asr_input"] is None and tokens["asr_output"] is None
    assert tokens["synth_prompt"] == 11


def test_an_asr_timeout_attempt_still_makes_asr_totals_null(scratch):
    from pipeline.tts import render

    asr = Asr([TranscriptionUnavailable("asr_timeout", "slow")])
    _fail(Provider([PCM]), asr, scratch)
    (attempt,) = _attempts(scratch)
    assert attempt["asr"]["elapsed_s"] is None
    assert attempt["asr"]["input_tokens"] is None
    tokens, _ = render._phase_totals([_progress(scratch)])
    assert tokens["asr_input"] is None and tokens["asr_thinking"] is None


def test_a_synth_attempt_has_no_recheck_of(scratch):
    _run(Provider([PCM]), Asr([TEXT]), scratch)
    (attempt,) = _attempts(scratch)
    assert "recheck_of" not in attempt


def test_three_blocks_end_in_asr_unavailable_after_one_synth_and_three_asr(scratch):
    provider = Provider([PCM, PCM_SECOND])
    asr = Asr([_blocked(), _blocked(), _blocked()])
    sleeps = Sleeps()
    abort = threading.Event()
    failure = _fail(provider, asr, scratch, sleeps=sleeps, abort=abort)
    assert failure.reason == gp.REASON_ASR_UNAVAILABLE
    assert (len(provider.calls), asr.calls) == (1, 3)
    assert len(set(asr.audio)) == 1
    assert sleeps.seconds == [2.0, 8.0]  # no third backoff, and no IndexError
    assert abort.is_set()
    assert "asr_blocked" in failure.detail
    assert [a["outcome"] for a in _attempts(scratch)] == ["asr_blocked"] * 3
    assert [a["n"] for a in _attempts(scratch)] == [1, 2, 3]
    assert [a.get("recheck_of") for a in _attempts(scratch)] == [None, 1, 1]
    assert not (scratch / "chunk-0000.pcm").exists()


def test_block_block_pass_backs_off_2_then_8(scratch):
    provider = Provider([PCM])
    asr = Asr([_blocked(), _blocked(), TEXT])
    _, sleeps, _ = _run(provider, asr, scratch)
    assert (len(provider.calls), asr.calls) == (1, 3)
    assert sleeps.seconds == [2.0, 8.0]
    assert [a["n"] for a in _attempts(scratch)] == [1, 2, 3]
    assert _attempts(scratch)[-1]["recheck_of"] == 1


@pytest.mark.parametrize(
    "reason", ["asr_error", "asr_timeout", "asr_empty", "asr_incomplete"]
)
def test_every_other_unavailable_reason_still_fails_at_once(scratch, reason):
    asr = Asr([TranscriptionUnavailable(reason, "x"), TEXT])
    failure = _fail(Provider([PCM, PCM]), asr, scratch)
    assert failure.reason == gp.REASON_ASR_UNAVAILABLE
    assert asr.calls == 1


def test_the_block_reason_is_matched_exactly_not_as_text(scratch):
    # The detail talks about a block; the reason is not asr_blocked. No retry.
    exc = TranscriptionUnavailable("asr_error", "asr_blocked: no candidates")
    asr = Asr([exc, TEXT])
    sleeps = Sleeps()
    failure = _fail(Provider([PCM]), asr, scratch, sleeps=sleeps)
    assert failure.reason == gp.REASON_ASR_UNAVAILABLE
    assert (asr.calls, sleeps.seconds) == (1, [])


def test_omission_then_synth_then_block_then_recheck_pass(scratch):
    provider = Provider([PCM_REJECTED, PCM])
    asr = Asr([OMITTED, _blocked(), TEXT])
    _, sleeps, _ = _run(provider, asr, scratch)
    assert (len(provider.calls), asr.calls) == (2, 3)
    # No backoff before the omission re-render; the re-check follows 2 tries, so 8 s.
    assert sleeps.seconds == [8.0]
    assert asr.audio[1] == asr.audio[2] == gp.pcm_to_wav(PCM)
    assert [a["n"] for a in _attempts(scratch)] == [1, 2, 3]
    assert [a["outcome"] for a in _attempts(scratch)] == [
        "omission",
        "asr_blocked",
        "verified",
    ]
    assert _attempts(scratch)[2]["recheck_of"] == 2  # the synth attempt that made it


def test_omission_then_synth_then_block_then_recheck_block_is_asr_unavailable(
    scratch,
):
    provider = Provider([PCM_REJECTED, PCM])
    asr = Asr([OMITTED, _blocked(), _blocked()])
    sleeps = Sleeps()
    failure = _fail(provider, asr, scratch, sleeps=sleeps)
    assert failure.reason == gp.REASON_ASR_UNAVAILABLE
    assert (len(provider.calls), asr.calls) == (2, 3)
    assert sleeps.seconds == [8.0]


def test_transient_error_then_synth_then_block_then_recheck_pass(scratch):
    provider = Provider([_err("infra"), PCM])
    asr = Asr([_blocked(), TEXT])
    _, sleeps, _ = _run(provider, asr, scratch)
    assert (len(provider.calls), asr.calls) == (2, 2)
    assert sleeps.seconds == [2.0, 8.0]
    assert [a["n"] for a in _attempts(scratch)] == [1, 2, 3]
    assert _attempts(scratch)[2]["recheck_of"] == 2


def test_transient_error_then_synth_then_block_then_block_is_asr_unavailable(scratch):
    provider = Provider([_err("content"), PCM])
    asr = Asr([_blocked(), _blocked()])
    failure = _fail(provider, asr, scratch)
    assert failure.reason == gp.REASON_ASR_UNAVAILABLE
    assert (len(provider.calls), asr.calls) == (2, 2)


def test_a_block_on_the_last_try_is_asr_unavailable_not_exhausted(scratch):
    provider = Provider([_err("infra"), _err("infra"), PCM])
    asr = Asr([_blocked(), TEXT])
    sleeps = Sleeps()
    failure = _fail(provider, asr, scratch, sleeps=sleeps)
    assert failure.reason == gp.REASON_ASR_UNAVAILABLE
    assert (len(provider.calls), asr.calls) == (3, 1)  # no fourth try
    assert sleeps.seconds == [2.0, 8.0]


def test_a_transient_error_after_a_block_does_not_keep_the_block_verdict(scratch):
    # block (try 1), recheck omission (try 2), synth error (try 3): exhausted by the
    # synth error, which is what the chunk last suffered -- not a block.
    provider = Provider([PCM, _err("infra")])
    asr = Asr([_blocked(), OMITTED])
    failure = _fail(provider, asr, scratch)
    assert failure.reason == gp.REASON_EXHAUSTED
    assert (len(provider.calls), asr.calls) == (2, 2)


def test_block_then_recheck_omission_then_fresh_synth_pass(scratch):
    provider = Provider([PCM_REJECTED, PCM_SECOND])
    asr = Asr([_blocked(), OMITTED, TEXT])
    _, sleeps, _ = _run(provider, asr, scratch)
    assert (len(provider.calls), asr.calls) == (2, 3)
    assert sleeps.seconds == [2.0]  # the omission re-render keeps no backoff
    # The retained PCM is gone: the third request carries the second synth's audio.
    assert asr.audio[0] == asr.audio[1] == gp.pcm_to_wav(PCM_REJECTED)
    assert asr.audio[2] == gp.pcm_to_wav(PCM_SECOND)
    assert (scratch / "chunk-0000.pcm").read_bytes() == PCM_SECOND
    attempts = _attempts(scratch)
    assert [a["outcome"] for a in attempts] == ["asr_blocked", "omission", "verified"]
    assert attempts[1]["synth"] is None and attempts[1]["recheck_of"] == 1
    assert attempts[2]["synth"]["status"] == "ok" and "recheck_of" not in attempts[2]
    # A re-check's omission clip is named by that attempt's own n (2), not the synth's.
    assert attempts[1]["omission_audio"] == gp.omission_name(0, 2)
    assert (scratch / gp.omission_name(0, 2)).read_bytes() == PCM_REJECTED
    assert "omission_audio" not in attempts[0]


def test_block_then_recheck_omission_then_second_omission_fails(scratch):
    provider = Provider([PCM, PCM_SECOND])
    asr = Asr([_blocked(), OMITTED, OMITTED])
    failure = _fail(provider, asr, scratch)
    assert failure.reason == gp.REASON_SECOND_OMISSION
    assert (len(provider.calls), asr.calls) == (2, 3)


def test_a_recheck_omission_clip_is_collected_by_the_parent(scratch):
    _run(Provider([PCM_REJECTED, PCM]), Asr([_blocked(), OMITTED, TEXT]), scratch)
    clips = gp._collect_omission_audio(scratch, [_progress(scratch)])
    assert clips == ((0, 2, PCM_REJECTED),)


def test_deadline_shorter_than_the_recheck_backoff_fails_without_sleeping(scratch):
    provider = Provider([PCM])
    asr = Asr([_blocked(), TEXT])
    sleeps = Sleeps()
    failure = _fail(
        provider, asr, scratch, deadline=time.monotonic() + 1.0, sleeps=sleeps
    )
    assert failure.reason == gp.REASON_DEADLINE
    assert sleeps.seconds == []
    assert (len(provider.calls), asr.calls) == (1, 1)
    assert "backoff" in failure.detail


def test_abort_during_the_recheck_backoff_stops_quietly(scratch):
    provider = Provider([PCM])
    asr = Asr([_blocked(), TEXT])
    with pytest.raises(gp._ChunkAborted):
        _run(provider, asr, scratch, sleeps=Sleeps(aborted=True))
    assert (len(provider.calls), asr.calls) == (1, 1)


def test_abort_set_by_a_sibling_beats_the_recheck(scratch):
    abort = threading.Event()
    asr = Asr([_blocked(), TEXT])
    real_make = asr.make

    def make(timeout):
        t = real_make(timeout)
        abort.set()  # a sibling fails while our first verification runs
        return t

    with pytest.raises(gp._ChunkAborted):
        gp._render_chunk(
            0, TEXT, LEAF, _far(), scratch, Provider([PCM]), make, abort, Sleeps()
        )
    assert asr.calls == 1


def test_recheck_is_recorded_as_started_before_its_request(scratch):
    seen: list[dict] = []
    asr = Asr([_blocked(), TEXT])
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
    assert len(seen) == 2
    first, during = seen[1]["attempts"]
    assert first["outcome"] == "asr_blocked"
    assert during["n"] == 2 and during["synth"] is None and during["recheck_of"] == 1
    assert during["asr"]["status"] == "started"
    assert during["asr"]["input_tokens"] is None
    assert during["outcome"] is None


def test_a_fresh_transcriber_is_built_and_closed_per_recheck(scratch):
    asr = Asr([_blocked(), _blocked(), TEXT])
    closed: list[int] = []

    class Closing:
        def __init__(self, inner, k):
            self._inner, self._k = inner, k

        def __call__(self, audio, mime):
            return self._inner(audio, mime)

        def close(self):
            closed.append(self._k)

    built: list[Closing] = []

    def make(timeout):
        t = Closing(asr.make(timeout), len(built))
        built.append(t)
        return t

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
    assert len(built) == 3 and len(set(map(id, built))) == 3
    assert closed == [0, 1, 2]


def test_the_recheck_transcriber_timeout_is_recomputed(scratch):
    asr = Asr([_blocked(), TEXT])
    _run(Provider([PCM]), asr, scratch, deadline=time.monotonic() + 30.0)
    assert all(29.0 < t <= 30.0 for t in asr.timeouts) and len(asr.timeouts) == 2


def test_a_real_blocked_response_is_rechecked_through_the_real_transcriber(
    scratch, monkeypatch
):
    """No candidates + a real block_reason, as the SDK returns it, through the real
    GeminiTranscriber and verify_audio: re-checked once on the same bytes, then
    verified. Only the SDK client is faked."""
    from google.genai import types

    blocked = types.GenerateContentResponse(
        candidates=[],
        prompt_feedback=types.GenerateContentResponsePromptFeedback(
            block_reason=types.BlockedReason.OTHER
        ),
    )
    good = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason=types.FinishReason.STOP,
                content=types.Content(parts=[types.Part(text=TEXT)]),
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=5
        ),
    )
    sent: list[bytes] = []
    replies = [blocked, good]

    class Models:
        def generate_content(self, *, model, contents, config):
            sent.append(contents[0].inline_data.data)
            return replies.pop(0)

    class Client:
        models = Models()

        def close(self):
            pass

    monkeypatch.setattr(asr_module, "_make_genai_client", lambda timeout_s: Client())
    sleeps = Sleeps()
    rec = gp._render_chunk(
        0,
        TEXT,
        LEAF,
        _far(),
        scratch,
        Provider([PCM]),
        lambda timeout: GeminiTranscriber(timeout_s=timeout),
        threading.Event(),
        sleeps,
    )
    assert rec["file"] == "chunk-0000.pcm"
    assert (scratch / "chunk-0000.pcm").read_bytes() == PCM
    assert len(sent) == 2 and sent[0] == sent[1]  # the same audio bytes, twice
    assert sleeps.seconds == [2.0]
    first, second = _attempts(scratch)
    assert first["outcome"] == "asr_blocked"
    assert first["asr"]["reasons"] == [ASR_BLOCKED]
    assert "block_reason=OTHER" in first["asr"]["detail"]
    assert second["synth"] is None and second["recheck_of"] == 1
    assert second["outcome"] == "verified"


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
            "runner_error",
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
        main_child(
            scratch,
            parent_pid=4242,
            fac=(make_provider, Asr([TEXT]).make),
            bootstrap=lambda: order.append("bootstrap"),
        )
    assert info.value.code == 0
    # The bootstrap (network denial) must precede anything that could build a
    # client; the watchdog precedes both so a hung bootstrap is bounded.
    assert order == ["watchdog", "bootstrap", "factory"]
    assert json.loads((scratch / gp.RESULT_NAME).read_text())["status"] == "ok"


def test_child_main_hands_the_parent_pid_to_the_watchdog(scratch, child_env):
    with pytest.raises(_Exited):
        main_child(scratch, parent_pid=4242)
    assert child_env[0][0] == 4242


def test_child_main_exits_even_on_a_base_exception(scratch, child_env):
    def bootstrap():
        raise KeyboardInterrupt

    with pytest.raises(_Exited) as info:
        main_child(scratch, bootstrap=bootstrap)
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
        main_child(scratch)
    assert events[-3:] == ["flush-out", "flush-err", "exit"]


def test_child_main_bootstrap_failure_is_child_error(scratch, child_env):
    def bootstrap():
        raise RuntimeError("bootstrap broke")

    with pytest.raises(_Exited) as info:
        main_child(scratch, bootstrap=bootstrap)
    assert info.value.code == 0
    result = json.loads((scratch / gp.RESULT_NAME).read_text())
    assert result["reason"] == gp.REASON_CHILD_ERROR
    assert "bootstrap broke" in result["detail"]


def test_child_main_exits_nonzero_when_the_result_cannot_be_written(
    scratch, child_env, monkeypatch
):
    def boom(*a, **k):
        raise OSError("disk full")

    gp._write_input(scratch, [TEXT], LEAF)  # before the disk "fills"
    monkeypatch.setattr(gp, "_atomic_write", boom)
    with pytest.raises(_Exited) as info:
        gp._child_main(str(scratch), _far(), 1, factories("ok"), None)
    assert info.value.code == 1


def test_child_main_arguments_are_picklable(scratch):
    import pickle

    args = (str(scratch), 123.0, 1, factories("ok"), None)
    assert pickle.loads(pickle.dumps(args))[0] == str(scratch)


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


# --- the parent side: result validation ----------------------------------------


def _good_result(scratch: Path, n: int = 2) -> dict:
    records = []
    for i in range(n):
        data = fake_pcm(i)
        (scratch / gp.chunk_name(i)).write_bytes(data)
        records.append(
            {
                "index": i,
                "file": gp.chunk_name(i),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return gp._result_ok(records)


def test_valid_ok_result_yields_the_pcm_in_order(scratch):
    result = _good_result(scratch, 3)
    assert gp._validate_result(result, scratch, 3) == [fake_pcm(i) for i in range(3)]


def test_failed_result_with_a_known_reason_is_valid(scratch):
    result = gp._result_failed(gp.REASON_FATAL, "nope", 1)
    assert gp._validate_result(result, scratch, 2) is None


def _invalid(result, scratch, n=2):
    with pytest.raises(gp._InvalidResult) as info:
        gp._validate_result(result, scratch, n)
    return str(info.value)


def test_missing_index_is_invalid(scratch):
    assert "indices" in _invalid(_good_result(scratch, 2), scratch, 3)


def test_extra_index_is_invalid(scratch):
    assert "indices" in _invalid(_good_result(scratch, 3), scratch, 2)


def test_duplicate_or_out_of_order_indices_are_invalid(scratch):
    result = _good_result(scratch, 2)
    result["chunks"] = [result["chunks"][1], result["chunks"][0]]
    assert "indices" in _invalid(result, scratch)
    result["chunks"] = [result["chunks"][0], result["chunks"][0]]
    assert "indices" in _invalid(result, scratch)


def test_missing_file_is_invalid(scratch):
    result = _good_result(scratch)
    (scratch / gp.chunk_name(1)).unlink()
    assert "chunk-0001.pcm" in _invalid(result, scratch)


def test_byte_count_mismatch_is_invalid(scratch):
    result = _good_result(scratch)
    result["chunks"][0]["bytes"] += 2
    assert "bytes" in _invalid(result, scratch)


def test_odd_byte_count_is_invalid(scratch):
    result = _good_result(scratch)
    (scratch / gp.chunk_name(0)).write_bytes(b"\x01\x02\x03")
    result["chunks"][0]["bytes"] = 3
    result["chunks"][0]["sha256"] = hashlib.sha256(b"\x01\x02\x03").hexdigest()
    assert "odd" in _invalid(result, scratch)


def test_zero_bytes_is_invalid(scratch):
    result = _good_result(scratch)
    (scratch / gp.chunk_name(0)).write_bytes(b"")
    result["chunks"][0]["bytes"] = 0
    result["chunks"][0]["sha256"] = hashlib.sha256(b"").hexdigest()
    assert "empty" in _invalid(result, scratch)


def test_sha_mismatch_is_invalid(scratch):
    result = _good_result(scratch)
    data = bytearray((scratch / gp.chunk_name(0)).read_bytes())
    data[0] ^= 0xFF  # same length, different audio
    (scratch / gp.chunk_name(0)).write_bytes(bytes(data))
    assert "sha256" in _invalid(result, scratch)


def test_a_result_naming_another_file_is_invalid(scratch):
    result = _good_result(scratch)
    result["chunks"][0]["file"] = "../../etc/passwd"
    assert "file" in _invalid(result, scratch)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(schema=99),
        lambda r: r.update(status="maybe"),
        lambda r: r.update(chunks="not a list"),
        lambda r: r.update(chunks=[{"index": 0}]),
        lambda r: r["chunks"][0].update(index=True),
        lambda r: r["chunks"][0].update(bytes="8"),
    ],
)
def test_structurally_wrong_ok_results_are_invalid(scratch, mutate):
    result = _good_result(scratch)
    mutate(result)
    _invalid(result, scratch)


def test_not_a_dict_is_invalid(scratch):
    _invalid(["ok"], scratch)


@pytest.mark.parametrize(
    "result",
    [
        {"schema": 1, "status": "failed", "reason": "made_up", "detail": ""},
        {"schema": 1, "status": "failed", "reason": None, "detail": ""},
        {"schema": 1, "status": "failed", "reason": "fatal", "failed_chunk": "0"},
        # An unhashable reason must be refused, not crash the membership test.
        {"schema": 1, "status": "failed", "reason": ["fatal"], "detail": ""},
        {"schema": 1, "status": "failed", "reason": {"a": 1}, "detail": ""},
        # failed_chunk must name a real chunk (n is 2 here), and not be a bool.
        {"schema": 1, "status": "failed", "reason": "fatal", "failed_chunk": -1},
        {"schema": 1, "status": "failed", "reason": "fatal", "failed_chunk": 2},
        {"schema": 1, "status": "failed", "reason": "fatal", "failed_chunk": True},
    ],
)
def test_failed_results_with_bad_reason_or_chunk_are_invalid(scratch, result):
    _invalid(result, scratch)


def test_read_result_is_none_when_absent(scratch):
    assert gp._read_result(scratch) is None


def test_read_result_rejects_partial_or_garbage_json(scratch):
    (scratch / gp.RESULT_NAME).write_text('{"schema": 1, "status": "o')
    with pytest.raises(gp._InvalidResult):
        gp._read_result(scratch)
    (scratch / gp.RESULT_NAME).write_bytes(b"\xff\xfe")
    with pytest.raises(gp._InvalidResult):
        gp._read_result(scratch)


def test_read_progress_tolerates_missing_partial_and_wrong_shape(scratch):
    _atomic = gp._atomic_write_json
    _atomic(scratch / gp.progress_name(0), {"index": 0, "attempts": [{"n": 1}]})
    (scratch / gp.progress_name(1)).write_text("{not json")
    _atomic(scratch / gp.progress_name(2), {"index": 2, "attempts": "nope"})
    records = gp._read_progress(scratch, 4)
    assert records[0]["attempts"] == [{"n": 1}]
    assert records[1] == {"index": 1, "attempts": [], "progress": "unreadable"}
    assert records[2] == {"index": 2, "attempts": [], "progress": "unreadable"}
    assert records[3] == {"index": 3, "attempts": [], "progress": "missing"}


# --- the parent side: the runner with a fake process ---------------------------


class FakeProc:
    """Stands in for ``multiprocessing.Process``; ``start`` may run the child."""

    pid = 4242

    def __init__(
        self,
        args,
        *,
        on_start=None,
        on_kill=None,
        exits=False,
        unkillable=False,
        kill_raises=None,
    ):
        self.args = args
        self._unkillable = unkillable  # kill() is sent but the process lives on
        self._kill_raises = kill_raises
        self.scratch = Path(args[0])
        self._on_start = on_start
        self._on_kill = on_kill
        self._exits = exits  # exits right after start (having run on_start)
        self.alive = False
        self.exitcode = None
        self.killed = 0
        self.closed = False
        self.joins: list[float | None] = []

    def start(self):
        self.alive = True
        if self._on_start:
            self._on_start(self)
        if self._exits:
            self.alive = False
            self.exitcode = 0

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.joins.append(timeout)
        if self.alive and timeout:
            time.sleep(min(timeout, 0.02))

    def kill(self):
        self.killed += 1
        if self._kill_raises:  # it died just before the signal: kill() raises
            self.alive = False
            raise self._kill_raises
        if not self._unkillable:
            self.alive = False
            self.exitcode = -9
        if self._on_kill:
            self._on_kill(self)

    def close(self):
        if self.alive:  # like multiprocessing.Process.close()
            raise ValueError("Cannot close a process while it is still running")
        self.closed = True


def run_child_inline(proc, deadline=None):
    scratch, own_deadline, _ppid, fac, _boot = proc.args
    chunks, leaf = gp._read_input(Path(scratch))
    gp._run_child(chunks, leaf, deadline or own_deadline, scratch, fac)


@pytest.fixture
def phase(monkeypatch, tmp_path):
    """Run ``run_gemini_phase`` against a fake process: (outcome, procs, roots)."""
    roots = tmp_path / "roots"
    procs: list[FakeProc] = []

    def run(make_proc, *, n=2, budget_s=5.0, fac=None, chunks=None):
        def _make(args):
            proc = make_proc(args)
            procs.append(proc)
            return proc

        monkeypatch.setattr(gp, "_make_process", _make)
        outcome = gp.run_gemini_phase(
            make_chunks(n) if chunks is None else chunks,
            LEAF,
            budget_s=budget_s,
            scratch_root=roots,
            _factories=fac or factories("ok"),
        )
        return outcome, procs, roots

    return run


def test_runner_happy_path_returns_ordered_pcm_and_cleans_up(phase):
    outcome, procs, roots = phase(
        lambda a: FakeProc(a, on_start=run_child_inline, exits=True), n=3
    )
    assert outcome.ok and outcome.reason is None
    assert outcome.pcm_parts == [fake_pcm(i) for i in range(3)]
    assert [r["index"] for r in outcome.chunk_records] == [0, 1, 2]
    assert outcome.chunk_records[0]["attempts"][0]["outcome"] == "verified"
    assert outcome.child_pid == 4242
    assert outcome.spawn_s >= 0 and outcome.elapsed_s >= outcome.spawn_s
    assert list(roots.iterdir()) == []  # scratch removed
    assert procs[0].closed


def test_runner_reports_a_child_failure_and_kills_a_lingering_child(phase):
    def start(proc):
        gp._atomic_write_json(
            proc.scratch / gp.RESULT_NAME,
            gp._result_failed(gp.REASON_FATAL, "boom", 1),
        )

    outcome, procs, roots = phase(lambda a: FakeProc(a, on_start=start))  # stays alive
    assert (outcome.ok, outcome.reason, outcome.failed_chunk) == (False, "fatal", 1)
    assert outcome.detail == "boom"
    assert outcome.pcm_parts is None
    assert procs[0].killed == 1 and procs[0].closed
    assert list(roots.iterdir()) == []


def test_child_that_dies_without_a_result_is_child_no_result(phase):
    outcome, _, roots = phase(lambda a: FakeProc(a, exits=True))
    assert (outcome.ok, outcome.reason) == (False, gp.REASON_CHILD_NO_RESULT)
    assert "exit code 0" in outcome.detail
    assert list(roots.iterdir()) == []


def test_result_written_just_before_the_child_exits_is_not_lost(phase):
    # is_alive() is False and the result is on disk: the result wins.
    outcome, _, _ = phase(lambda a: FakeProc(a, on_start=run_child_inline, exits=True))
    assert outcome.ok


def test_hung_child_is_killed_at_the_deadline(phase):
    started = time.monotonic()
    outcome, procs, roots = phase(lambda a: FakeProc(a), budget_s=0.3)
    assert time.monotonic() - started < 2.0
    assert (outcome.ok, outcome.reason) == (False, gp.REASON_DEADLINE)
    assert procs[0].killed == 1
    assert gp.REAP_TIMEOUT_SECONDS in procs[0].joins  # join() after kill()
    assert list(roots.iterdir()) == []


def test_ok_result_that_lands_at_the_deadline_is_kept_and_validated(phase):
    outcome, procs, _ = phase(
        lambda a: FakeProc(a, on_kill=lambda p: run_child_inline(p, _far())),
        budget_s=0.2,
    )
    assert procs[0].killed == 1
    assert outcome.ok and outcome.pcm_parts == [fake_pcm(0), fake_pcm(1)]


def test_bad_ok_result_that_lands_at_the_deadline_is_still_rejected(phase):
    def on_kill(proc):
        run_child_inline(proc, _far())
        (proc.scratch / gp.chunk_name(0)).write_bytes(b"\x00\x00")  # tampered

    outcome, _, _ = phase(lambda a: FakeProc(a, on_kill=on_kill), budget_s=0.2)
    assert (outcome.ok, outcome.reason) == (False, gp.REASON_INVALID_RESULT)
    assert outcome.pcm_parts is None


def test_invalid_ok_result_means_no_audio_is_used(phase):
    def start(proc):
        run_child_inline(proc)
        (proc.scratch / gp.chunk_name(1)).unlink()

    outcome, _, _ = phase(lambda a: FakeProc(a, on_start=start, exits=True))
    assert (outcome.ok, outcome.reason) == (False, gp.REASON_INVALID_RESULT)
    assert outcome.pcm_parts is None
    assert "chunk-0001.pcm" in outcome.detail


def test_unparseable_result_is_invalid_result(phase):
    def start(proc):
        (proc.scratch / gp.RESULT_NAME).write_text('{"status": "ok", "chu')

    outcome, procs, _ = phase(lambda a: FakeProc(a, on_start=start))
    assert (outcome.ok, outcome.reason) == (False, gp.REASON_INVALID_RESULT)
    assert procs[0].killed == 1


def test_unreadable_progress_never_fails_the_phase(phase):
    def start(proc):
        run_child_inline(proc)
        (proc.scratch / gp.progress_name(0)).write_text("{garbage")
        (proc.scratch / gp.progress_name(1)).unlink()

    outcome, _, _ = phase(lambda a: FakeProc(a, on_start=start, exits=True))
    assert outcome.ok
    assert [r.get("progress") for r in outcome.chunk_records] == [
        "unreadable",
        "missing",
    ]


def test_spawn_failure_is_spawn_failed_and_cleans_up(phase):
    def start(proc):
        raise OSError("cannot fork")

    outcome, procs, roots = phase(lambda a: FakeProc(a, on_start=start))
    assert (outcome.ok, outcome.reason) == (False, gp.REASON_SPAWN_FAILED)
    assert "cannot fork" in outcome.detail
    assert list(roots.iterdir()) == []
    # A process that never started is not reaped: kill() on it would raise.
    assert procs[0].killed == 0 and procs[0].joins == []


def test_keyboard_interrupt_kills_the_child_cleans_up_and_propagates(
    monkeypatch, phase
):
    def interrupt(proc, timeout):
        raise KeyboardInterrupt

    monkeypatch.setattr(gp, "_join_child", interrupt)
    made: list[FakeProc] = []
    with pytest.raises(KeyboardInterrupt):
        phase(lambda a: made.append(FakeProc(a)) or made[-1])
    assert made[0].killed == 1 and made[0].closed
    assert list((made[0].scratch.parent).iterdir()) == []


def test_the_child_gets_one_absolute_deadline_computed_before_start(phase):
    seen = {}

    def start(proc):
        seen["deadline"] = proc.args[1]
        seen["at_start"] = time.monotonic()
        run_child_inline(proc)

    before = time.monotonic()
    phase(lambda a: FakeProc(a, on_start=start, exits=True), budget_s=50.0)
    assert before + 50.0 <= seen["deadline"] <= seen["at_start"] + 50.0


def test_the_child_receives_only_picklable_arguments(phase):
    import pickle

    captured = {}

    def start(proc):
        captured["args"] = proc.args
        run_child_inline(proc)

    phase(
        lambda a: FakeProc(a, on_start=start, exits=True),
        fac=factories("ok"),
    )
    pickle.dumps(captured["args"])
    assert captured["args"][2] == os.getpid()  # parent pid, for the watchdog


def test_child_reported_start_time_is_used_when_available(phase):
    def start(proc):
        gp._atomic_write_json(
            proc.scratch / gp.STARTED_NAME,
            {"pid": 1, "monotonic": time.monotonic()},
        )
        run_child_inline(proc)

    outcome, _, _ = phase(lambda a: FakeProc(a, on_start=start, exits=True))
    assert outcome.child_started_s is not None and outcome.child_started_s >= 0


def test_child_reported_start_time_is_none_when_absent_or_bad(phase):
    outcome, _, _ = phase(lambda a: FakeProc(a, on_start=run_child_inline, exits=True))
    assert outcome.child_started_s is None

    def start(proc):
        (proc.scratch / gp.STARTED_NAME).write_text("garbage")
        run_child_inline(proc)

    outcome, _, _ = phase(lambda a: FakeProc(a, on_start=start, exits=True))
    assert outcome.child_started_s is None and outcome.ok


def test_child_main_reports_when_it_started(scratch, child_env):
    before = time.monotonic()
    with pytest.raises(_Exited):
        main_child(scratch)
    started = json.loads((scratch / gp.STARTED_NAME).read_text())
    assert started["pid"] == os.getpid()
    assert before <= started["monotonic"] <= time.monotonic()


# --- review round: runner cleanup, scratch, budget ------------------------------


def test_an_unkillable_child_costs_one_reap_keeps_the_dir_and_logs(phase, capsys):
    outcome, procs, roots = phase(lambda a: FakeProc(a, unkillable=True), budget_s=0.2)
    assert (outcome.ok, outcome.reason) == (False, gp.REASON_DEADLINE)
    # One kill and one bounded wait, not two of each.
    assert procs[0].killed == 1
    assert procs[0].joins.count(gp.REAP_TIMEOUT_SECONDS) == 1
    (kept,) = roots.iterdir()  # not deleted under a live child
    assert kept.name.startswith("gemini-phase-")
    assert "still alive" in capsys.readouterr().err
    assert not procs[0].closed


def test_a_kill_that_raises_on_an_already_dead_child_is_absorbed(phase, capsys):
    outcome, procs, roots = phase(
        lambda a: FakeProc(a, kill_raises=ProcessLookupError("gone")), budget_s=0.2
    )
    assert (outcome.ok, outcome.reason) == (False, gp.REASON_DEADLINE)
    assert procs[0].killed == 1  # tried once, not again from the cleanup
    assert procs[0].closed
    assert list(roots.iterdir()) == []
    assert "kill() raised" in capsys.readouterr().err


def test_scratch_root_that_cannot_be_used_is_spawn_failed_not_a_raise(
    monkeypatch, tmp_path
):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    monkeypatch.setattr(
        gp, "_make_process", lambda a: pytest.fail("must not spawn without scratch")
    )
    outcome = gp.run_gemini_phase(
        make_chunks(2), LEAF, budget_s=5.0, scratch_root=not_a_dir / "sub"
    )
    assert (outcome.ok, outcome.reason) == (False, gp.REASON_SPAWN_FAILED)
    assert outcome.chunk_records == []
    assert outcome.pcm_parts is None


@pytest.mark.parametrize("budget", [0.0, -1.0])
def test_no_budget_means_deadline_without_spawning(monkeypatch, tmp_path, budget):
    monkeypatch.setattr(
        gp, "_make_process", lambda a: pytest.fail("must not spawn with no budget")
    )
    roots = tmp_path / "roots"
    outcome = gp.run_gemini_phase(
        make_chunks(2), LEAF, budget_s=budget, scratch_root=roots
    )
    assert (outcome.ok, outcome.reason) == (False, gp.REASON_DEADLINE)
    assert outcome.spawn_s == 0.0 and outcome.child_pid is None
    assert not roots.exists()


def _old(path: Path, age_s: float) -> None:
    t = time.time() - age_s
    os.utime(path, (t, t))


def test_stale_scratch_dirs_are_swept_but_fresh_and_foreign_ones_are_not(phase):
    def make(a):
        return FakeProc(a, on_start=run_child_inline, exits=True)

    _, _, roots = phase(make)  # creates the (empty) scratch root
    stale = roots / "gemini-phase-stale"
    fresh = roots / "gemini-phase-fresh"
    foreign = roots / "someone-elses-dir"
    a_file = roots / "gemini-phase-but-a-file"
    for d in (stale, fresh, foreign):
        d.mkdir()
        (d / "chunk-0000.pcm").write_bytes(b"\x00\x00")
    a_file.write_text("x")
    for old in (stale, foreign, a_file):
        _old(old, gp.STALE_SCRATCH_SECONDS + 60)

    outcome, _, _ = phase(make)
    assert outcome.ok
    assert not stale.exists()
    assert fresh.exists() and foreign.exists() and a_file.exists()


def test_the_stale_threshold_dwarfs_the_phase_budget():
    assert gp.STALE_SCRATCH_SECONDS >= 10 * gp.GEMINI_BUDGET_SECONDS


def test_sweeping_never_raises(tmp_path, monkeypatch):
    gp._sweep_stale_scratch(tmp_path / "does-not-exist")  # no such root
    f = tmp_path / "f"
    f.write_text("x")
    gp._sweep_stale_scratch(f)  # not a directory

    def boom(*a, **k):
        raise OSError("perm")

    (tmp_path / "gemini-phase-x").mkdir()
    _old(tmp_path / "gemini-phase-x", gp.STALE_SCRATCH_SECONDS + 60)
    monkeypatch.setattr(gp.shutil, "rmtree", boom)
    gp._sweep_stale_scratch(tmp_path)  # the failing delete is swallowed


def test_runner_error_outcome_carries_a_traceback_and_no_audio():
    try:
        raise RuntimeError("runner bug")
    except RuntimeError as exc:
        out = gp.runner_error_outcome(exc, elapsed_s=1.5)
    assert (out.ok, out.reason, out.pcm_parts) == (False, "runner_error", None)
    assert "Traceback" in out.detail and out.detail.rstrip().endswith(
        "RuntimeError: runner bug"
    )
    assert len(out.detail) <= gp.ERROR_DETAIL_CHARS + 50
    assert out.chunk_records == [] and out.elapsed_s == 1.5


def test_a_child_cannot_report_the_parent_only_runner_error_reason(scratch):
    result = gp._result_failed(gp.REASON_RUNNER_ERROR, "claimed", 0)
    with pytest.raises(gp._InvalidResult):
        gp._validate_result(result, scratch, 2)
    assert gp.REASON_RUNNER_ERROR not in gp.CHILD_REASONS
    assert gp.CHILD_REASONS == gp.FALLBACK_REASONS - {gp.REASON_RUNNER_ERROR}


def test_runner_error_outcome_reports_spawn_time_as_unknown():
    out = gp.runner_error_outcome(RuntimeError("x"), elapsed_s=0.5)
    assert out.spawn_s is None and out.child_started_s is None


def test_read_progress_flags_attempts_that_are_not_dicts(scratch):
    gp._atomic_write_json(
        scratch / gp.progress_name(0), {"index": 0, "attempts": [{"n": 1}, "junk"]}
    )
    (record,) = gp._read_progress(scratch, 1)
    assert record == {"index": 0, "attempts": [], "progress": "unreadable"}


# --- the child reads its input from a file, not through the spawn pipe --------------


def test_input_roundtrips_chunks_and_leaf(scratch):
    chunks = ["Chunk 0 one", 'Chunk 1 \u00e9t\u00e9 "quoted" \n\n two']
    gp._write_input(scratch, chunks, GeminiConfig("m", "Kore", style="calm"))
    got_chunks, got_leaf = gp._read_input(scratch)
    assert got_chunks == chunks
    assert got_leaf == GeminiConfig("m", "Kore", style="calm")


@pytest.mark.parametrize(
    "content",
    [
        None,  # no file at all
        b"\xff\xfe not utf8",
        b"{not json",
        b"[]",
        b'{"schema": 99, "chunks": ["a"], "leaf": {}}',
        b'{"schema": 1, "chunks": "abc", "leaf": {}}',
        b'{"schema": 1, "chunks": [], "leaf": {"provider": "gemini"}}',
        b'{"schema": 1, "chunks": ["a", ""], "leaf": {"provider": "gemini",'
        b' "model": "m", "voice": "Kore", "style": ""}}',
        b'{"schema": 1, "chunks": ["a", 5], "leaf": {"provider": "gemini",'
        b' "model": "m", "voice": "Kore", "style": ""}}',
        b'{"schema": 1, "chunks": ["a"], "leaf": {"provider": "openai",'
        b' "model": "m", "voice": "nova"}}',
        b'{"schema": 1, "chunks": ["a"], "leaf": {"provider": "gemini",'
        b' "model": "m", "voice": "nova", "style": ""}}',
    ],
)
def test_unusable_input_is_refused(scratch, content):
    if content is not None:
        (scratch / gp.INPUT_NAME).write_bytes(content)
    with pytest.raises(gp._InputError):
        gp._read_input(scratch)


@pytest.mark.parametrize("break_it", ["missing", "garbage", "wrong_leaf"])
def test_child_main_with_bad_input_is_child_error(scratch, child_env, break_it):
    gp._write_input(scratch, [TEXT], LEAF)
    path = scratch / gp.INPUT_NAME
    if break_it == "missing":
        path.unlink()
    elif break_it == "garbage":
        path.write_text("{oops")
    else:
        data = json.loads(path.read_text())
        data["leaf"] = {"provider": "openai", "model": "m", "voice": "nova"}
        path.write_text(json.dumps(data))
    with pytest.raises(_Exited) as info:
        gp._child_main(str(scratch), _far(), 1, factories("ok"), None)
    assert info.value.code == 0
    result = json.loads((scratch / gp.RESULT_NAME).read_text())
    assert result["reason"] == gp.REASON_CHILD_ERROR
    assert "input" in result["detail"]


def test_the_runner_writes_the_input_before_start_and_passes_only_small_args(phase):
    import pickle

    big = [f"Chunk {i} " + "x" * 3000 for i in range(60)]  # a 180 KB episode
    seen = {}

    def start(proc):
        seen["args_bytes"] = len(pickle.dumps(proc.args))
        seen["input"] = gp._read_input(proc.scratch)  # already on disk at start()

    outcome, _, _ = phase(lambda a: FakeProc(a, on_start=start, exits=True), chunks=big)
    assert seen["args_bytes"] < 5_000  # nothing of the episode rides the spawn pipe
    assert seen["input"] == (big, LEAF)
    assert outcome.reason == gp.REASON_CHILD_NO_RESULT  # the fake never ran a child


def test_an_unwritable_input_is_spawn_failed_and_nothing_is_spawned(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        gp, "_make_process", lambda a: pytest.fail("must not spawn without input")
    )

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(gp, "_write_input", boom)
    roots = tmp_path / "roots"
    outcome = gp.run_gemini_phase(
        make_chunks(2), LEAF, budget_s=5.0, scratch_root=roots
    )
    assert (outcome.ok, outcome.reason) == (False, gp.REASON_SPAWN_FAILED)
    assert "disk full" in outcome.detail
    assert list(roots.iterdir()) == []  # the scratch dir was still cleaned up


def test_default_transcriber_factory_uses_the_production_thinking_setting():
    from pipeline.tts import asr

    t = gp._default_make_transcriber(30.0)
    assert t.thinking == "low" and t.timeout_s == 30.0
    assert t.policy == asr.ASR_POLICY


# --- diagnosable verdicts: spans, counts and the transcript in the ASR record ---

from pipeline.tts.normalize import normalize_tokens  # noqa: E402
from pipeline.tts.verify import verify_audio  # noqa: E402


_TOKENS = normalize_tokens(TEXT)
# The ASR keeps the first 8 words, says two other words where the rest of the
# chunk should be, and so drops len(_TOKENS) - _HEAD tokens in one contiguous
# gap. (Normalization spells digits out, so 8 words are more than 8 tokens.)
_HEAD = len(normalize_tokens(" ".join(TEXT.split()[:8])))
DROPPED = " ".join(TEXT.split()[:8]) + " xray yankee"
_DROPPED_EXCERPT = " ".join(_TOKENS[_HEAD : _HEAD + 30])


def _asr(scratch, k=0) -> dict:
    return _progress(scratch)["attempts"][k]["asr"]


def test_omission_attempt_records_the_flagged_span_and_the_transcript(scratch):
    _run(Provider([PCM, PCM]), Asr([DROPPED, TEXT]), scratch)
    asr = _asr(scratch, 0)
    assert asr["status"] == "omission"
    assert asr["script_tokens"] == len(_TOKENS)
    assert asr["transcript_tokens"] == _HEAD + 2
    assert asr["matched_tokens"] == _HEAD
    assert asr["max_net_missing"] >= 6
    flagged = [s for s in asr["spans"] if s["flagged"]]
    assert len(flagged) == 1
    span = flagged[0]
    assert span["excerpt"] == _DROPPED_EXCERPT
    assert span["heard"] == "xray yankee"
    assert (span["script_start"], span["script_end"]) == (_HEAD, len(_TOKENS))
    assert (span["script_words"], span["transcript_words"]) == (len(_TOKENS) - _HEAD, 2)
    assert span["net_missing"] == asr["max_net_missing"] == len(_TOKENS) - _HEAD - 2
    assert set(span) == {
        "script_start",
        "script_end",
        "script_words",
        "transcript_words",
        "net_missing",
        "flagged",
        "excerpt",
        "heard",
    }
    assert asr["transcript"] == DROPPED  # the raw ASR text, verbatim


def test_passing_attempt_records_one_span_and_no_transcript(scratch):
    # One substituted word: a short gap, nowhere near flagged, still recorded.
    words = TEXT.split()
    swapped = len(normalize_tokens(words[30]))
    words[30] = "qq"
    _run(Provider([PCM]), Asr([" ".join(words)]), scratch)
    asr = _asr(scratch)
    assert asr["status"] == "pass"
    assert asr["transcript"] is None
    [span] = asr["spans"]
    assert span["flagged"] is False
    assert span["heard"] == "qq"
    assert span["net_missing"] == asr["max_net_missing"] == swapped - 1
    assert asr["script_tokens"] == len(_TOKENS) and asr["matched_tokens"] > 0


def test_clean_pass_has_no_spans_and_a_zero_margin(scratch):
    _run(Provider([PCM]), Asr([TEXT]), scratch)
    asr = _asr(scratch)
    assert asr["spans"] == []
    assert asr["max_net_missing"] == 0
    assert asr["transcript"] is None


def test_a_pass_records_only_the_span_with_the_largest_net():
    from dataclasses import replace

    spans = _synthetic_spans(nets=[1, 3, 2], flagged=False)
    base = verify_audio(b"", "audio/wav", TEXT, transcriber=_fixed_transcriber(TEXT))
    verdict = replace(base, analysis=replace(base.analysis, spans=spans))
    assert verdict.status == "pass"
    [span] = gp._recorded_spans(verdict)
    assert span["net_missing"] == 3
    assert gp._asr_record(verdict)["max_net_missing"] == 3


def test_recall_floor_only_omission_still_records_three_spans(scratch):
    # Every 10th word substituted: recall under the floor, but every gap is one
    # word, so no span is flagged. The best clues are the largest unflagged ones.
    words = TEXT.split()
    for i in range(5, len(words), 10):
        words[i] = f"zz{i}"
    _run(Provider([PCM, PCM]), Asr([" ".join(words), TEXT]), scratch)
    asr = _asr(scratch, 0)
    assert asr["status"] == "omission"
    assert asr["reasons"] == ["recall_below_floor"]
    assert not any(s["flagged"] for s in asr["spans"])
    assert len(asr["spans"]) == gp.MIN_RECORDED_OMISSION_SPANS == 3


def test_omission_spans_list_flagged_first_and_are_capped(scratch):
    # A recorded omission never lists more than MAX_RECORDED_SPANS, flagged first.
    verdict = verify_audio(
        b"", "audio/wav", TEXT, transcriber=_fixed_transcriber(DROPPED)
    )
    spans = gp._recorded_spans(verdict)
    assert spans[0]["flagged"] is True

    many = _spans_verdict(flagged=12, unflagged=5)
    recorded = gp._recorded_spans(many)
    assert len(recorded) == gp.MAX_RECORDED_SPANS == 8
    assert all(s["flagged"] for s in recorded)
    # largest first among the flagged, so the cap drops the least informative
    assert [s["net_missing"] for s in recorded] == sorted(
        (s["net_missing"] for s in recorded), reverse=True
    )


def test_two_omissions_name_what_was_dropped_in_the_failure_detail(scratch):
    failure = _fail(Provider([PCM, PCM]), Asr([DROPPED, DROPPED]), scratch)
    assert failure.reason == gp.REASON_SECOND_OMISSION
    assert failure.detail.startswith(
        "omission (long_unmatched_span,recall_below_floor) recall "
    )
    assert f", max net {len(_TOKENS) - _HEAD - 2}: " in failure.detail
    # the excerpt is trimmed to 12 tokens, and says what the ASR heard instead
    assert f'script "{" ".join(_TOKENS[_HEAD : _HEAD + 12])}"' in failure.detail
    assert 'heard "xray yankee"' in failure.detail


def test_omission_summary_trims_script_and_heard_to_twelve_tokens():
    fillers = [f"zz{chr(97 + i)}" for i in range(20)]  # letters: one token each
    transcript = " ".join(TEXT.split()[:8] + fillers)
    verdict = verify_audio(
        b"", "audio/wav", TEXT, transcriber=_fixed_transcriber(transcript)
    )
    summary = gp._omission_summary(verdict)
    script_part = summary.split('script "', 1)[1].split('" heard "', 1)[0]
    heard_part = summary.split('heard "', 1)[1].rstrip('"')
    assert len(script_part.split()) == 12
    assert heard_part == " ".join(fillers[:12])


def test_omission_summary_reports_recall_to_three_places():
    verdict = verify_audio(
        b"", "audio/wav", TEXT, transcriber=_fixed_transcriber(DROPPED)
    )
    recall = f"{verdict.recall:.3f}"
    assert f") recall {recall}, max net " in gp._omission_summary(verdict)


def test_omission_summary_never_raises_and_falls_back_to_the_old_text():
    class Broken:
        reasons = ("long_unmatched_span",)

        @property
        def analysis(self):
            raise RuntimeError("boom")

        recall = None

    assert gp._omission_summary(Broken()) == "omission (long_unmatched_span)"
    assert gp._omission_summary(object()) == "omission (flagged)"


def _fixed_transcriber(text):
    def t(audio, mime):
        return Transcription(text, "fake-asr", "0", "STOP", 0.0, 1, 1, 0)

    return t


def _synthetic_spans(*, nets, flagged):
    from pipeline.tts.verify import Span

    return tuple(
        Span(10 * i, 10 * i + 1, i, i, 1, 0, net, flagged, "e", "h")
        for i, net in enumerate(nets)
    )


def _spans_verdict(*, flagged, unflagged):
    """A real omission verdict whose analysis is swapped for synthetic spans."""
    from dataclasses import replace

    spans = _synthetic_spans(nets=[100 + i for i in range(flagged)], flagged=True)
    spans += _synthetic_spans(nets=list(range(unflagged)), flagged=False)
    base = verify_audio(b"", "audio/wav", TEXT, transcriber=_fixed_transcriber(DROPPED))
    return replace(base, analysis=replace(base.analysis, spans=spans))


def test_asr_record_of_an_unavailable_verdict_has_no_diagnostics():
    verdict = verify_audio(
        b"", "audio/wav", TEXT, transcriber=_fixed_transcriber("... --- ...")
    )
    assert verdict.status == "unavailable" and verdict.transcript is not None
    rec = gp._asr_record(verdict)
    assert rec["status"] == "unavailable"
    assert rec["spans"] == []
    assert rec["transcript"] is None  # omission only
    for key in ("script_tokens", "transcript_tokens", "matched_tokens"):
        assert rec[key] is None
    assert rec["max_net_missing"] is None


def test_asr_record_of_a_raising_transcriber_has_no_diagnostics():
    def boom(audio, mime):
        raise _asr_unavailable()

    rec = gp._asr_record(verify_audio(b"", "audio/wav", TEXT, transcriber=boom))
    assert rec["status"] == "unavailable"
    assert rec["spans"] == [] and rec["transcript"] is None
    assert rec["max_net_missing"] is None and rec["script_tokens"] is None


def test_the_transcript_is_capped_with_a_marker():
    raw = DROPPED + " pad" * 2000  # far over the cap
    rec = gp._asr_record(
        verify_audio(b"", "audio/wav", TEXT, transcriber=_fixed_transcriber(raw))
    )
    assert rec["status"] == "omission"
    assert (
        rec["transcript"] == raw[: gp.MAX_RECORDED_TRANSCRIPT_CHARS] + "...[truncated]"
    )
    assert gp.MAX_RECORDED_TRANSCRIPT_CHARS == 6000


def test_a_transcript_at_the_cap_is_kept_whole():
    raw = (DROPPED + " pad" * 2000)[: gp.MAX_RECORDED_TRANSCRIPT_CHARS]
    rec = gp._asr_record(
        verify_audio(b"", "audio/wav", TEXT, transcriber=_fixed_transcriber(raw))
    )
    assert rec["transcript"] == raw


def test_a_diagnostics_bug_degrades_the_record_and_never_raises(monkeypatch, capsys):
    verdict = verify_audio(
        b"", "audio/wav", TEXT, transcriber=_fixed_transcriber(DROPPED)
    )

    def boom(v):
        raise RuntimeError("diagnostics broke")

    monkeypatch.setattr(gp, "_recorded_spans", boom)
    rec = gp._asr_record(verdict)
    # the original fields survive; the new ones are the same-shaped empties
    assert rec["status"] == "omission" and rec["recall"] == verdict.recall
    assert rec["spans"] == [] and rec["transcript"] is None
    assert rec["script_tokens"] is None and rec["max_net_missing"] is None
    assert "diagnostics broke" in capsys.readouterr().err


def test_a_killed_asr_request_leaves_a_same_shaped_started_record(scratch):
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
    started = seen[0]["attempts"][0]["asr"]
    finished = _asr(scratch)
    assert started["status"] == "started"
    assert set(started) == set(finished)
    assert started["spans"] == [] and started["transcript"] is None
    for key in (
        "script_tokens",
        "transcript_tokens",
        "matched_tokens",
        "max_net_missing",
    ):
        assert started[key] is None


# --- keeping the audio of every omission attempt -----------------------------------

PCM_REJECTED = fake_pcm(5)  # told apart from PCM, the audio that passes


def test_omission_name_is_the_documented_one():
    assert gp.omission_name(0, 1) == "omission-0000-1.pcm"
    assert gp.omission_name(12, 2) == "omission-0012-2.pcm"


def test_an_omission_attempt_keeps_its_rejected_pcm_and_records_the_name(scratch):
    _run(Provider([PCM_REJECTED, PCM]), Asr([OMITTED, TEXT]), scratch)
    first, second = _progress(scratch)["attempts"]
    assert first["outcome"] == "omission"
    assert first["omission_audio"] == "omission-0000-1.pcm"
    assert (scratch / "omission-0000-1.pcm").read_bytes() == PCM_REJECTED
    assert "omission_audio" not in second  # a verified attempt keeps nothing
    assert (scratch / "chunk-0000.pcm").read_bytes() == PCM  # the audio that shipped
    assert not list(scratch.glob("*.tmp"))


def test_a_second_omission_keeps_both_attempts(scratch):
    failure = _fail(Provider([PCM_REJECTED, PCM]), Asr([OMITTED, OMITTED]), scratch)
    assert failure.reason == gp.REASON_SECOND_OMISSION
    names = [a["omission_audio"] for a in _progress(scratch)["attempts"]]
    assert names == ["omission-0000-1.pcm", "omission-0000-2.pcm"]
    assert (scratch / "omission-0000-1.pcm").read_bytes() == PCM_REJECTED
    assert (scratch / "omission-0000-2.pcm").read_bytes() == PCM
    assert list(scratch.glob("chunk-*")) == []  # still no unverified chunk audio


def test_the_clip_is_on_disk_before_the_progress_that_names_it(scratch, monkeypatch):
    seen: list[bool] = []
    real = gp._atomic_write_json

    def spy(path, obj):
        for attempt in obj.get("attempts", []) if isinstance(obj, dict) else []:
            name = attempt.get("omission_audio")
            if name:
                seen.append((scratch / name).exists())
        real(path, obj)

    monkeypatch.setattr(gp, "_atomic_write_json", spy)
    _run(Provider([PCM_REJECTED, PCM]), Asr([OMITTED, TEXT]), scratch)
    assert seen and all(seen)


def test_a_failing_clip_write_changes_nothing_about_the_chunk(
    scratch, monkeypatch, capsys
):
    real = gp._atomic_write

    def flaky(path, data):
        if Path(path).name.startswith("omission-"):
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(gp, "_atomic_write", flaky)
    rec, _, abort = _run(Provider([PCM_REJECTED, PCM]), Asr([OMITTED, TEXT]), scratch)
    assert rec["file"] == "chunk-0000.pcm" and not abort.is_set()
    first, _second = _progress(scratch)["attempts"]
    assert first["outcome"] == "omission" and "omission_audio" not in first
    assert not list(scratch.glob("omission-*"))
    assert "omission-0000-1.pcm not saved" in capsys.readouterr().err


def test_a_failing_clip_write_does_not_change_a_second_omission_failure(
    scratch, monkeypatch
):
    real = gp._atomic_write

    def flaky(path, data):
        if Path(path).name.startswith("omission-"):
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(gp, "_atomic_write", flaky)
    failure = _fail(Provider([PCM, PCM]), Asr([OMITTED, OMITTED]), scratch)
    assert failure.reason == gp.REASON_SECOND_OMISSION


def _records_with_clips(scratch, spec):
    """``spec``: {chunk: [(n, data-or-None)]}; writes the files, returns records."""
    records = []
    for i in range(max(spec) + 1):
        attempts = []
        for n, data in spec.get(i, []):
            name = gp.omission_name(i, n)
            if data is not None:
                (scratch / name).write_bytes(data)
            attempts.append({"n": n, "outcome": "omission", "omission_audio": name})
        records.append({"index": i, "attempts": attempts})
    return records


def test_clips_are_collected_in_chunk_then_attempt_order(scratch):
    spec = {0: [(1, fake_pcm(1)), (2, fake_pcm(2))], 2: [(1, fake_pcm(3))]}
    recs = _records_with_clips(scratch, spec)
    assert gp._collect_omission_audio(scratch, recs) == (
        (0, 1, fake_pcm(1)),
        (0, 2, fake_pcm(2)),
        (2, 1, fake_pcm(3)),
    )


def test_at_most_four_clips_are_collected(scratch):
    spec = {i: [(1, fake_pcm(i)), (2, fake_pcm(i))] for i in range(3)}
    clips = gp._collect_omission_audio(scratch, _records_with_clips(scratch, spec))
    assert gp.MAX_OMISSION_CLIPS == 4
    assert [(i, n) for i, n, _ in clips] == [(0, 1), (0, 2), (1, 1), (1, 2)]


def test_unusable_clip_files_are_skipped_not_fatal(scratch, monkeypatch):
    monkeypatch.setattr(gp, "MAX_OMISSION_CLIP_BYTES", 100)
    spec = {
        0: [
            (1, b"\x01\x00" * 10),  # good
            (2, b"\x01\x00\x01"),  # odd length
        ],
        1: [(1, b""), (2, None)],  # empty; missing
        2: [(1, b"\x01\x00" * 51)],  # 102 bytes: over the cap
    }
    recs = _records_with_clips(scratch, spec)
    assert gp._collect_omission_audio(scratch, recs) == ((0, 1, b"\x01\x00" * 10),)


def test_a_clip_with_a_name_that_is_not_the_canonical_one_is_ignored(scratch):
    (scratch / "other.pcm").write_bytes(b"\x01\x00" * 10)
    (scratch.parent / "outside.pcm").write_bytes(b"\x01\x00" * 10)
    recs = [
        {
            "index": 0,
            "attempts": [
                {"n": 1, "omission_audio": "other.pcm"},
                {"n": 1, "omission_audio": "../outside.pcm"},
                {"n": 2, "omission_audio": gp.omission_name(0, 1)},  # wrong attempt
                {"n": 1, "omission_audio": None},
                {"n": 1},
            ],
        }
    ]
    (scratch / gp.omission_name(0, 1)).write_bytes(b"\x01\x00" * 10)
    assert gp._collect_omission_audio(scratch, recs) == ()


def test_a_symlinked_clip_is_not_followed(scratch):
    target = scratch.parent / "secret.pcm"
    target.write_bytes(b"\x01\x00" * 10)
    (scratch / gp.omission_name(0, 1)).symlink_to(target)
    recs = [
        {"index": 0, "attempts": [{"n": 1, "omission_audio": gp.omission_name(0, 1)}]}
    ]
    assert gp._collect_omission_audio(scratch, recs) == ()


@pytest.mark.parametrize(
    "records",
    [
        [{"index": 0, "attempts": [], "progress": "missing"}],
        [{"index": 0, "attempts": [], "progress": "unreadable"}],
        [{"index": 0, "attempts": "nope"}],
        [{"index": 0, "attempts": [None, "str", 7]}],
        [{"index": 0, "attempts": [{"n": "1", "omission_audio": "x"}]}],
        [{"index": 0, "attempts": [{"n": True, "omission_audio": "x"}]}],
        ["not a dict", None, 3],
        None,
    ],
)
def test_malformed_progress_yields_no_clips_and_never_raises(scratch, records):
    assert gp._collect_omission_audio(scratch, records) == ()


def test_collecting_clips_never_raises_when_the_scratch_dir_is_gone(tmp_path):
    recs = [
        {"index": 0, "attempts": [{"n": 1, "omission_audio": gp.omission_name(0, 1)}]}
    ]
    assert gp._collect_omission_audio(tmp_path / "gone", recs) == ()
    assert gp._collect_omission_audio(None, recs) == ()


def test_a_read_failure_on_one_clip_keeps_the_others(scratch, monkeypatch):
    recs = _records_with_clips(scratch, {0: [(1, fake_pcm(1)), (2, fake_pcm(2))]})
    real = Path.read_bytes

    def flaky(self):
        if self.name == gp.omission_name(0, 1):
            raise OSError("io error")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    assert gp._collect_omission_audio(scratch, recs) == ((0, 2, fake_pcm(2)),)


def test_an_omission_file_in_scratch_does_not_make_a_good_result_invalid(scratch):
    result = _good_result(scratch, 2)
    (scratch / gp.omission_name(0, 1)).write_bytes(b"\x01\x00" * 10)
    (scratch / "omission-garbage.pcm").write_bytes(b"\x01")  # even an odd one
    assert gp._validate_result(result, scratch, 2) == [fake_pcm(0), fake_pcm(1)]


def test_phase_outcome_defaults_to_no_omission_audio():
    out = gp.PhaseOutcome(
        ok=False,
        reason="deadline",
        detail="",
        pcm_parts=None,
        chunk_records=[],
        elapsed_s=0.0,
        spawn_s=0.0,
    )
    assert out.omission_audio == ()


def test_the_runner_hands_back_the_rejected_audio_of_a_failed_phase(phase):
    outcome, _, roots = phase(
        lambda a: FakeProc(a, on_start=run_child_inline, exits=True),
        n=2,
        fac=factories("omission_on_chunk", chunk=1),
    )
    assert outcome.reason == gp.REASON_SECOND_OMISSION and outcome.failed_chunk == 1
    assert outcome.omission_audio == ((1, 1, fake_pcm(1)), (1, 2, fake_pcm(1)))
    assert list(roots.iterdir()) == []  # read before the scratch dir went


def test_a_clean_phase_has_no_omission_audio(phase):
    outcome, _, _ = phase(lambda a: FakeProc(a, on_start=run_child_inline, exits=True))
    assert outcome.ok and outcome.omission_audio == ()


def test_the_runner_keeps_the_audio_of_an_omission_that_a_rerender_fixed(phase):
    # chunk 0: first attempt dropped words, the re-render passes: the phase is ok
    # and the rejected audio is still returned for listening.
    def start(proc):
        def provider():
            return Provider([PCM_REJECTED, fake_pcm(0)])

        asr = Asr([OMITTED, TEXT])
        gp._write_input(proc.scratch, [TEXT], LEAF)
        chunks, leaf = gp._read_input(proc.scratch)
        gp._run_child(
            chunks, leaf, time.monotonic() + 30, proc.scratch, (provider, asr.make)
        )

    outcome, _, _ = phase(lambda a: FakeProc(a, on_start=start, exits=True), n=1)
    assert outcome.ok
    assert outcome.omission_audio == ((0, 1, PCM_REJECTED),)


def test_a_clip_collection_bug_never_fails_the_phase(phase, monkeypatch):
    def boom(scratch, records):
        raise RuntimeError("collector broke")

    monkeypatch.setattr(gp, "_collect_omission_audio", boom)
    outcome, _, _ = phase(lambda a: FakeProc(a, on_start=run_child_inline, exits=True))
    assert outcome.ok and outcome.omission_audio == ()


# --- review round -----------------------------------------------------------------


def _many_records(scratch, per_chunk):
    """Files and records for ``per_chunk`` = {chunk: [n, ...]} (PCM tags chunk/n)."""
    records = []
    for i in range(max(per_chunk) + 1):
        attempts = []
        for n in per_chunk.get(i, []):
            name = gp.omission_name(i, n)
            (scratch / name).write_bytes(bytes([i + 1, n]) * 10)
            attempts.append({"n": n, "omission_audio": name})
        records.append({"index": i, "attempts": attempts})
    return records


def test_the_failing_chunks_clips_win_a_full_cap(scratch):
    # Four earlier clips would fill the cap by themselves; the chunk that failed
    # the phase must not lose its clips to them.
    recs = _many_records(scratch, {0: [1, 2], 1: [1, 2], 2: [1, 2]})
    clips = gp._collect_omission_audio(scratch, recs, failed_chunk=2)
    got = [(i, n) for i, n, _ in clips]
    assert len(got) == gp.MAX_OMISSION_CLIPS
    assert {(2, 1), (2, 2)} <= set(got)
    assert got == sorted(got)  # output is always in (chunk, n) order


def test_the_remaining_cap_goes_to_the_earliest_other_clips(scratch):
    recs = _many_records(scratch, {0: [1, 2], 1: [1, 2], 2: [1, 2]})
    clips = gp._collect_omission_audio(scratch, recs, failed_chunk=2)
    assert [(i, n) for i, n, _ in clips] == [(0, 1), (0, 2), (2, 1), (2, 2)]


def test_without_a_failed_chunk_the_cap_keeps_the_earliest(scratch):
    recs = _many_records(scratch, {0: [1, 2], 1: [1, 2], 2: [1, 2]})
    clips = gp._collect_omission_audio(scratch, recs)
    assert [(i, n) for i, n, _ in clips] == [(0, 1), (0, 2), (1, 1), (1, 2)]
    assert gp._collect_omission_audio(scratch, recs, failed_chunk=None) == clips


def test_an_unusable_failing_chunk_clip_is_replaced_by_the_next_candidate(scratch):
    recs = _many_records(scratch, {0: [1, 2], 1: [1, 2], 2: [1]})
    (scratch / gp.omission_name(2, 1)).write_bytes(b"\x01")  # odd length
    clips = gp._collect_omission_audio(scratch, recs, failed_chunk=2)
    assert [(i, n) for i, n, _ in clips] == [(0, 1), (0, 2), (1, 1), (1, 2)]


def test_a_failed_chunk_that_is_not_an_index_changes_nothing(scratch):
    recs = _many_records(scratch, {0: [1], 1: [1]})
    base = gp._collect_omission_audio(scratch, recs)
    for bogus in (99, -1, "x", True, 1.5):
        assert gp._collect_omission_audio(scratch, recs, failed_chunk=bogus) == base


def test_the_runner_prioritises_the_failed_chunk_through_the_real_child(phase):
    # Chunks 0-3 each omit once and then pass (up to four clips); chunk 4 omits
    # twice and fails the phase, so the cap is over-subscribed whenever the
    # others finish first. The failing chunk's two clips must always be there.
    # (Which earlier clips exist is a race between workers; the unit tests above
    # pin the selection deterministically.)
    def start(proc):
        texts = make_chunks(5)

        class Prov:  # shared across chunk threads, so it keys on the text
            def synthesize_detailed(self, text, cfg, *, timeout=None):
                i = int(text.split()[1])
                return Synthesis(
                    pcm=fake_pcm(i),
                    finish_reason="STOP",
                    prompt_tokens=1,
                    audio_tokens=1,
                    elapsed_s=0.0,
                )

        seen: dict[int, int] = {}

        def make_asr(timeout):
            def transcribe(audio, mime):
                i = int.from_bytes(audio[44:46], "little") - 1  # first sample
                seen[i] = seen.get(i, 0) + 1
                bad = i == 4 or seen[i] == 1
                text = texts[i]
                if bad:
                    text = " ".join(text.split()[:8])
                return Transcription(text, "fake-asr", "0", "STOP", 0.0, 1, 1, 0)

            return transcribe

        gp._write_input(proc.scratch, texts, LEAF)
        chunks, leaf = gp._read_input(proc.scratch)
        gp._run_child(
            chunks, leaf, time.monotonic() + 30, proc.scratch, (Prov, make_asr)
        )

    outcome, _, _ = phase(lambda a: FakeProc(a, on_start=start, exits=True), n=5)
    assert outcome.reason == gp.REASON_SECOND_OMISSION and outcome.failed_chunk == 4
    got = [(i, n) for i, n, _ in outcome.omission_audio]
    assert {(4, 1), (4, 2)} <= set(got) and len(got) <= gp.MAX_OMISSION_CLIPS
    assert got == sorted(got)


def test_a_kill_after_a_clip_was_written_still_returns_the_clip(phase, release):
    # Chunk 0: first attempt is an omission (clip written), the re-render hangs,
    # the deadline passes. The runner must hand back the clip it can still read.
    class HangingSecondCall:
        def __init__(self):
            self.calls = 0

        def synthesize_detailed(self, text, cfg, *, timeout=None):
            self.calls += 1
            if self.calls == 2:
                release.wait(30)
            return Synthesis(
                pcm=PCM_REJECTED,
                finish_reason="STOP",
                prompt_tokens=1,
                audio_tokens=1,
                elapsed_s=0.0,
            )

    provider = HangingSecondCall()
    asr = Asr([OMITTED])

    def start(proc):
        gp._write_input(proc.scratch, [TEXT], LEAF)
        chunks, leaf = gp._read_input(proc.scratch)
        gp._run_child(
            chunks,
            leaf,
            time.monotonic() + 0.4,
            proc.scratch,
            (lambda: provider, asr.make),
        )

    outcome, _, roots = phase(
        lambda a: FakeProc(a, on_start=start, exits=True), n=1, chunks=[TEXT]
    )
    assert outcome.reason == gp.REASON_DEADLINE
    assert outcome.omission_audio == ((0, 1, PCM_REJECTED),)
    assert list(roots.iterdir()) == []


def test_a_killed_child_leaves_its_clip_readable_in_the_progress_files(phase):
    # The kill case proper: the child never reports, the parent kills at its
    # deadline; the clip the child had already written is still collected.
    def start(proc):
        pcm_path = proc.scratch / gp.omission_name(0, 1)
        pcm_path.write_bytes(PCM_REJECTED)
        attempt = {
            "n": 1,
            "outcome": "omission",
            "omission_audio": gp.omission_name(0, 1),
        }
        gp._atomic_write_json(
            proc.scratch / gp.progress_name(0),
            {"schema": 1, "index": 0, "attempts": [attempt, {"n": 2}]},
        )

    outcome, procs, _ = phase(lambda a: FakeProc(a, on_start=start), n=1, budget_s=0.3)
    assert outcome.reason == gp.REASON_DEADLINE and procs[0].killed == 1
    assert outcome.omission_audio == ((0, 1, PCM_REJECTED),)


def test_a_recall_floor_only_summary_says_there_was_no_flagged_span(scratch):
    words = TEXT.split()
    for i in range(5, len(words), 10):
        words[i] = f"zz{chr(97 + i % 26)}"
    failure = _fail(
        Provider([PCM, PCM]), Asr([" ".join(words), " ".join(words)]), scratch
    )
    assert failure.reason == gp.REASON_SECOND_OMISSION
    assert "(recall_below_floor) recall " in failure.detail
    assert " (no flagged span): script " in failure.detail


def test_a_flagged_summary_does_not_say_no_flagged_span():
    verdict = verify_audio(
        b"", "audio/wav", TEXT, transcriber=_fixed_transcriber(DROPPED)
    )
    assert "no flagged span" not in gp._omission_summary(verdict)
