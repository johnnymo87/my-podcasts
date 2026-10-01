"""``render_episode`` with a Gemini primary: phase, verification, fallback, alert.

``gemini_phase.run_gemini_phase`` is replaced by canned ``PhaseOutcome`` objects,
so nothing spawns; the real-spawn behaviour of the phase itself is covered by
``test_gemini_phase_spawn.py``. The OpenAI provider is a fake whose PCM bytes
differ from the canned Gemini PCM, so "no Gemini audio reached the fallback
output" is a byte-level assertion, not an inference.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import threading
import time

import pytest

from pipeline.tts import cache, chunker, render
from pipeline.tts import gemini_phase as gp
from pipeline.tts.asr import Transcription
from pipeline.tts.config import GeminiConfig, OpenAIConfig, RenderConfig
from pipeline.tts.providers import GeminiProvider, OpenAIProvider, TTSProviderError
from pipeline.tts.verify import verify_audio


GEMINI = GeminiConfig("gemini-3.8-flash-lite-tts", "Kore")
FALLBACK = OpenAIConfig("tts-1-hd", "nova")
CFG = RenderConfig(GEMINI, FALLBACK)
CFG_NO_FALLBACK = RenderConfig(GEMINI, None)
# Several chunks. (chunk_text's 3000-char target is fixed, so re-chunking for
# OpenAI's 4096 ceiling yields the same boundaries; the ceiling is only a backstop.)
TEXT = "\n\n".join(
    f"Paragraph {i}. " + "Sentence number one here. " * 40 for i in range(7)
)
OPENAI_PCM = b"\x02\x00" * 24_000  # 1 s, pattern 0x0002
MARK = b"\xaa\x0a"  # every canned Gemini sample


N_GEMINI = len(chunker.chunk_text(TEXT, ceiling=GeminiProvider.max_chars))
N_OPENAI = len(chunker.chunk_text(TEXT, ceiling=OpenAIProvider.max_chars))


def gemini_pcm(i: int) -> bytes:
    return MARK * 12_000 + bytes([i, 0]) * 12_000  # 1 s, carries the chunk index


def attempt(
    *, audio=22, prompt=11, asr_in=33, asr_out=44, asr_think=5, outcome="verified"
):
    return {
        "n": 1,
        "synth": {
            "status": "ok",
            "kind": None,
            "error": None,
            "elapsed_s": 0.5,
            "finish_reason": "STOP",
            "prompt_tokens": prompt,
            "audio_tokens": audio,
            "pcm_bytes": 48_000,
        },
        "asr": {
            "status": "pass",
            "reasons": [],
            "detail": "",
            "recall": 1.0,
            "elapsed_s": 0.25,
            "input_tokens": asr_in,
            "output_tokens": asr_out,
            "thinking_tokens": asr_think,
        },
        "outcome": outcome,
    }


def ok_outcome(chunks):
    return gp.PhaseOutcome(
        ok=True,
        reason=None,
        detail="",
        pcm_parts=[gemini_pcm(i) for i in range(len(chunks))],
        chunk_records=[
            {"index": i, "attempts": [attempt()]} for i in range(len(chunks))
        ],
        elapsed_s=12.5,
        spawn_s=0.002,
        child_started_s=1.4,
        child_pid=4242,
    )


def failed_outcome(reason="deadline", detail="the budget ran out", chunks=3):
    return gp.PhaseOutcome(
        ok=False,
        reason=reason,
        detail=detail,
        pcm_parts=None,
        chunk_records=[{"index": i, "attempts": [attempt()]} for i in range(chunks)],
        elapsed_s=360.0,
        spawn_s=0.003,
        failed_chunk=1,
        child_pid=4242,
        child_started_s=1.5,
    )


class FakeOpenAI:
    max_chars = 4096

    def __init__(self):
        self.calls: list[str] = []
        self.script: list = []

    def close(self):
        pass

    def synthesize(self, text, config):
        self.calls.append(text)
        if self.script:
            exc = self.script.pop(0)
            if exc is not None:
                raise exc
        return OPENAI_PCM


class Env:
    def __init__(self, monkeypatch, tmp_path):
        self.tmp = tmp_path
        self.phase_calls: list[tuple] = []
        self.outcome = None  # callable(chunks) -> PhaseOutcome, or an exception
        self.openai = FakeOpenAI()
        self.encoded: list[bytes] = []
        self.encode_kwargs: list[dict] = []
        self.alerts: list[str] = []
        self.alert_result = True

        def fake_phase(chunks, leaf, **kwargs):
            self.phase_calls.append((list(chunks), leaf, kwargs))
            out = self.outcome
            if isinstance(out, BaseException):
                raise out
            return out(chunks) if callable(out) else out

        monkeypatch.setattr(gp, "run_gemini_phase", fake_phase)
        monkeypatch.setattr(render, "_provider_for", lambda leaf: self.openai)
        monkeypatch.setattr(render, "_sleep", lambda s: None)

        def fake_encode(pcm, out, **kw):
            self.encoded.append(pcm)
            self.encode_kwargs.append(kw)
            out.write_bytes(b"ID3" + pcm[:10])

        monkeypatch.setattr(render, "encode_mp3", fake_encode)

        def fake_alert(text, severity="info"):
            self.alerts.append(text)
            if isinstance(self.alert_result, BaseException):
                raise self.alert_result
            return self.alert_result

        monkeypatch.setattr("pipeline.alerts.send_alert", fake_alert)

    def render(self, config=CFG, **kw):
        kw.setdefault("manifest_dir", self.tmp / "m")
        kw.setdefault("cache_dir", self.tmp / "c")
        kw.setdefault("episode_id", "2026-09-30-fp")
        return render.render_episode(
            TEXT,
            config,
            self.tmp / "out.mp3",
            feed_slug="fp-digest",
            **kw,
        )

    def manifests(self):
        return [
            json.loads(p.read_text())
            for p in sorted((self.tmp / "m" / "fp-digest").glob("*.json"))
        ]

    def cache_result(self, config=CFG):
        key = cache.cache_key(TEXT, config)
        return json.loads((self.tmp / "c" / key / "result.json").read_text())


@pytest.fixture
def env(monkeypatch, tmp_path) -> Env:
    yield Env(monkeypatch, tmp_path)
    # The alert worker is module-level and outlives a test: stop it (and drop any
    # queued alerts) before the monkeypatched ``send_alert`` is undone, or it could
    # drain the queue through the real one.
    render._stop_alert_worker()


# --- Gemini ok ---------------------------------------------------------------


def test_gemini_ok_renders_gemini_audio_and_caches_it_as_passed(env):
    env.outcome = ok_outcome
    result = env.render()
    ((chunks, leaf, kwargs),) = env.phase_calls
    assert chunks == chunker.chunk_text(TEXT, ceiling=GeminiProvider.max_chars)
    assert len(chunks) == N_GEMINI >= 3 and leaf == GEMINI
    assert kwargs["budget_s"] == gp.GEMINI_BUDGET_SECONDS
    assert env.encoded == [b"".join(gemini_pcm(i) for i in range(N_GEMINI))]
    assert (result.provider, result.rendered, result.cached) == (
        "gemini",
        GEMINI,
        False,
    )
    assert result.fallback_reason is None and result.chunks == N_GEMINI
    assert env.openai.calls == [] and env.alerts == []
    entry = env.cache_result()
    assert (entry["provider"], entry["verification"]) == ("gemini", "passed")
    assert entry["fallback_reason"] is None and entry["rendered"]["voice"] == "Kore"


def test_second_render_of_a_gemini_episode_hits_the_cache_without_a_phase(env):
    env.outcome = ok_outcome
    env.render()
    again = env.render()
    assert len(env.phase_calls) == 1
    assert again.cached and again.provider == "gemini" and again.rendered == GEMINI
    assert env.alerts == []


# --- fallback -----------------------------------------------------------------


def test_failed_phase_discards_all_gemini_pcm_and_renders_whole_text_with_openai(env):
    env.outcome = failed_outcome("deadline")
    result = env.render()
    (audio,) = env.encoded
    assert MARK not in audio  # not one Gemini sample
    assert audio == OPENAI_PCM * len(env.openai.calls)
    # The WHOLE text, chunked afresh for OpenAI (never the Gemini chunks reused).
    assert len(env.openai.calls) == N_OPENAI >= 3
    assert all(len(c) <= OpenAIProvider.max_chars for c in env.openai.calls)
    assert " ".join(env.openai.calls).split() == TEXT.split()
    assert (result.provider, result.rendered, result.cached) == (
        "openai",
        FALLBACK,
        False,
    )
    assert result.fallback_reason == "deadline" and result.chunks == N_OPENAI
    entry = env.cache_result()
    assert (entry["provider"], entry["verification"]) == ("openai", "not_run_openai")
    assert entry["fallback_reason"] == "deadline"
    assert entry["rendered"]["voice"] == "nova"


def test_exactly_one_alert_with_the_documented_text(env):
    env.outcome = failed_outcome("fatal")
    env.render()
    assert env.alerts == [
        "TTS fallback: fp-digest 2026-09-30-fp: "
        "Gemini gemini-3.8-flash-lite-tts/Kore fatal -> OpenAI nova rendered"
    ]
    assert env.manifests()[0]["alert_sent"] is True


def test_replay_of_a_cached_fallback_is_a_hit_with_no_phase_and_no_alert(env):
    env.outcome = failed_outcome("exhausted")
    env.render()
    phase_calls, alerts = len(env.phase_calls), len(env.alerts)
    replay = env.render()
    assert replay.cached and replay.rendered == FALLBACK and replay.provider == "openai"
    assert replay.fallback_reason == "exhausted"
    assert (len(env.phase_calls), len(env.alerts)) == (phase_calls, alerts)
    hit = env.manifests()[-1]
    assert hit["status"] == "cache_hit" and hit["gemini_phase"] is None
    assert hit["fallback_reason"] == "exhausted" and hit["alert_sent"] is None


def test_failed_phase_without_a_fallback_raises_with_no_alert(env):
    env.outcome = failed_outcome("asr_unavailable")
    with pytest.raises(
        render.TTSRenderError, match="Gemini phase failed: asr_unavailable"
    ):
        env.render(CFG_NO_FALLBACK)
    assert env.alerts == [] and env.openai.calls == [] and env.encoded == []
    [m] = env.manifests()
    assert m["status"] == "failed" and m["rendered_config"] is None
    assert m["gemini_phase"]["outcome"] == "failed"
    assert m["gemini_phase"]["reason"] == "asr_unavailable"
    assert m["fallback_reason"] is None and m["alert_sent"] is None
    assert not (env.tmp / "out.mp3").exists()


def test_both_failing_names_both_reasons_alerts_failed_and_writes_a_failed_manifest(
    env,
):
    env.outcome = failed_outcome("second_omission")
    env.openai.script = [TTSProviderError("401 bad key", retryable=False)]
    with pytest.raises(render.TTSRenderError) as info:
        env.render()
    assert "second_omission" in str(info.value) and "401 bad key" in str(info.value)
    (alert,) = env.alerts
    assert alert.startswith(
        "TTS fallback: fp-digest 2026-09-30-fp: Gemini gemini-3.8-flash-lite-tts/Kore "
        "second_omission (chunk index 1: the budget ran out) -> OpenAI nova FAILED: "
    )
    assert "401 bad key" in alert
    [m] = env.manifests()
    assert m["status"] == "failed" and m["alert_sent"] is True
    assert m["fallback_reason"] == "second_omission"
    assert m["rendered_config"] is None
    assert cache.lookup(env.tmp / "c", cache.cache_key(TEXT, CFG)) is None


def test_unexpected_fallback_exception_is_loud_and_still_alerts(env, monkeypatch):
    env.outcome = failed_outcome("deadline")

    def boom(leaf):
        raise KeyError("OPENAI_API_KEY")

    monkeypatch.setattr(render, "_provider_for", boom)
    with pytest.raises(KeyError):
        env.render()
    (alert,) = env.alerts
    assert "FAILED" in alert
    assert env.manifests()[0]["status"] == "failed"


def test_notify_fallback_false_sends_no_alert(env):
    env.outcome = failed_outcome("deadline")
    result = env.render(notify_fallback=False)
    assert env.alerts == [] and result.fallback_reason == "deadline"
    assert env.manifests()[0]["alert_sent"] is None


def test_alert_delivery_is_bounded_and_recorded_as_timeout(env, monkeypatch):
    release = threading.Event()

    def hang(text, severity="info"):
        release.wait(30)
        return True

    monkeypatch.setattr("pipeline.alerts.send_alert", hang)
    monkeypatch.setattr(render, "ALERT_WAIT_SECONDS", 0.2)
    env.outcome = failed_outcome("deadline")
    started = time.monotonic()
    try:
        result = env.render()
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 3.0 and result.fallback_reason == "deadline"
    assert env.manifests()[0]["alert_sent"] == "timeout"


@pytest.mark.parametrize(
    "outcome, expected",
    [(True, True), (False, False), (RuntimeError("pigeon down"), False)],
)
def test_alert_sent_records_the_delivery_result(env, outcome, expected):
    env.alert_result = outcome
    env.outcome = failed_outcome("deadline")
    env.render()
    assert env.manifests()[0]["alert_sent"] is expected


def test_the_alert_wait_default_is_twelve_seconds():
    assert render.ALERT_WAIT_SECONDS == 12.0


# --- manifest --------------------------------------------------------------------


def test_manifest_gemini_phase_on_success(env):
    env.outcome = ok_outcome
    env.render()
    [m] = env.manifests()
    phase = m["gemini_phase"]
    assert phase["outcome"] == "ok" and phase["reason"] is None
    assert phase["budget_s"] == gp.GEMINI_BUDGET_SECONDS
    assert (phase["elapsed_s"], phase["spawn_s"], phase["child_started_s"]) == (
        12.5,
        0.002,
        1.4,
    )
    assert [r["index"] for r in phase["chunks"]] == list(range(N_GEMINI))
    assert phase["chunks"][0]["attempts"][0]["synth"]["audio_tokens"] == 22
    assert phase["tokens"] == {
        "synth_prompt": 11 * N_GEMINI,
        "synth_audio": 22 * N_GEMINI,
        "asr_input": 33 * N_GEMINI,
        "asr_output": 44 * N_GEMINI,
        "asr_thinking": 5 * N_GEMINI,
    }
    assert phase["audio_seconds_generated"] == pytest.approx(1.0 * N_GEMINI)
    assert phase["audio_seconds_used"] == pytest.approx(1.0 * N_GEMINI)
    # chunks = the audio that shipped: the Gemini chunks, with seconds.
    assert [c["index"] for c in m["chunks"]] == list(range(N_GEMINI))
    assert sum(c["audio_seconds"] for c in m["chunks"]) == pytest.approx(N_GEMINI)
    assert m["rendered_provider"] == "gemini" and m["fallback_reason"] is None
    assert m["alert_sent"] is None and m["status"] == "rendered"


def test_manifest_after_fallback_keeps_gemini_attempts_apart_from_chunks(env):
    env.outcome = failed_outcome("deadline")
    env.render()
    [m] = env.manifests()
    # `chunks` is only what produced the final audio: the OpenAI chunks.
    assert len(m["chunks"]) == N_OPENAI and m["chunk_count"] == N_OPENAI
    assert all(c["chars"] <= 4096 for c in m["chunks"])
    phase = m["gemini_phase"]
    assert phase["outcome"] == "failed" and phase["reason"] == "deadline"
    assert phase["detail"] == "the budget ran out" and phase["failed_chunk"] == 1
    assert len(phase["chunks"]) == 3  # the discarded Gemini attempts live here
    assert phase["audio_seconds_generated"] == pytest.approx(3.0)
    assert phase["audio_seconds_used"] == 0.0
    assert m["rendered_provider"] == "openai" and m["fallback_reason"] == "deadline"


def test_token_totals_are_null_when_any_count_is_unknown(env):
    unknown = attempt()
    unknown["asr"].update(status="started", input_tokens=None, output_tokens=None)

    def outcome(chunks):
        out = failed_outcome("deadline", chunks=2)
        out.chunk_records[1]["attempts"] = [unknown]
        return out

    env.outcome = outcome
    env.render()
    tokens = env.manifests()[0]["gemini_phase"]["tokens"]
    assert tokens["synth_prompt"] == 22 and tokens["synth_audio"] == 44
    assert tokens["asr_input"] is None and tokens["asr_output"] is None
    assert tokens["asr_thinking"] == 10


def test_a_completed_asr_call_with_no_thinking_count_totals_zero_not_null(env):
    def outcome(chunks):
        out = failed_outcome("deadline", chunks=2)
        for rec in out.chunk_records:
            rec["attempts"] = [attempt(asr_think=None)]  # model reported none
        return out

    env.outcome = outcome
    env.render()
    tokens = env.manifests()[0]["gemini_phase"]["tokens"]
    assert tokens["asr_thinking"] == 0
    assert tokens["asr_input"] == 66 and tokens["asr_output"] == 88
    # The raw per-attempt evidence is left as reported.
    raw = env.manifests()[0]["gemini_phase"]["chunks"][0]["attempts"][0]["asr"]
    assert raw["thinking_tokens"] is None


def test_an_omission_attempt_that_completed_also_counts_absent_thinking_as_zero(env):
    def outcome(chunks):
        out = failed_outcome("second_omission", chunks=1)
        a = attempt(asr_think=None, outcome="omission")
        a["asr"]["status"] = "omission"
        out.chunk_records[0]["attempts"] = [a]
        return out

    env.outcome = outcome
    env.render()
    assert env.manifests()[0]["gemini_phase"]["tokens"]["asr_thinking"] == 0


def test_a_killed_in_flight_asr_call_keeps_thinking_unknown(env):
    in_flight = attempt(asr_in=None, asr_out=None, asr_think=None)
    in_flight["asr"].update(status="started", elapsed_s=None)

    def outcome(chunks):
        out = failed_outcome("deadline", chunks=2)
        out.chunk_records[1]["attempts"] = [in_flight]
        return out

    env.outcome = outcome
    env.render()
    tokens = env.manifests()[0]["gemini_phase"]["tokens"]
    assert tokens["asr_thinking"] is None and tokens["asr_input"] is None
    assert tokens["synth_prompt"] == 22  # the synth calls all completed


def test_an_asr_call_that_errored_without_usage_keeps_thinking_unknown(env):
    errored = attempt(asr_in=None, asr_out=None, asr_think=None)
    errored["asr"].update(status="unavailable", elapsed_s=None)  # asr_error: no info

    def outcome(chunks):
        out = failed_outcome("asr_unavailable", chunks=1)
        out.chunk_records[0]["attempts"] = [errored]
        return out

    env.outcome = outcome
    env.render()
    assert env.manifests()[0]["gemini_phase"]["tokens"]["asr_thinking"] is None


def test_an_empty_transcript_unavailable_call_did_complete(env):
    empty = attempt(asr_think=None)
    empty["asr"].update(status="unavailable", reasons=["asr_empty"])  # info present

    def outcome(chunks):
        out = failed_outcome("asr_unavailable", chunks=1)
        out.chunk_records[0]["attempts"] = [empty]
        return out

    env.outcome = outcome
    env.render()
    assert env.manifests()[0]["gemini_phase"]["tokens"]["asr_thinking"] == 0


def test_a_chunk_with_no_progress_makes_every_total_unknown(env):
    def outcome(chunks):
        out = failed_outcome("child_no_result", chunks=2)
        out.chunk_records[1] = {"index": 1, "attempts": [], "progress": "missing"}
        return out

    env.outcome = outcome
    env.render()
    tokens = env.manifests()[0]["gemini_phase"]["tokens"]
    assert set(tokens.values()) == {None}


def test_openai_primary_manifest_has_null_gemini_fields(env):
    from pipeline.tts.config import openai_config

    env.render(openai_config(model="tts-1-hd", voice="onyx"))
    [m] = env.manifests()
    assert m["gemini_phase"] is None and m["fallback_reason"] is None
    assert m["alert_sent"] is None
    assert env.phase_calls == []


# --- misc contract -----------------------------------------------------------------


def test_renderer_version_is_unchanged():
    assert cache.RENDERER_VERSION == "2"


def test_an_exception_escaping_the_phase_costs_an_openai_episode_not_the_episode(env):
    env.outcome = RuntimeError("runner bug")
    result = env.render()
    assert result.provider == "openai" and result.rendered == FALLBACK
    assert result.fallback_reason == "runner_error"
    assert MARK not in env.encoded[0]
    (alert,) = env.alerts
    assert alert.endswith("runner_error -> OpenAI nova rendered")
    [m] = env.manifests()
    phase = m["gemini_phase"]
    assert (phase["outcome"], phase["reason"]) == ("failed", "runner_error")
    assert (
        "RuntimeError: runner bug" in phase["detail"] and "Traceback" in phase["detail"]
    )
    assert m["fallback_reason"] == "runner_error" and m["alert_sent"] is True
    assert env.cache_result()["fallback_reason"] == "runner_error"
    # Nothing is known about what the phase cost: unknown, not zero.
    assert set(phase["tokens"].values()) == {None}
    assert phase["audio_seconds_generated"] is None


def test_an_exception_escaping_the_phase_without_a_fallback_raises_from_it(env):
    env.outcome = RuntimeError("runner bug")
    with pytest.raises(render.TTSRenderError, match="runner_error") as info:
        env.render(CFG_NO_FALLBACK)
    assert isinstance(info.value.__cause__, RuntimeError)
    assert env.alerts == [] and env.openai.calls == []
    [m] = env.manifests()
    assert m["status"] == "failed" and m["gemini_phase"]["reason"] == "runner_error"


def test_system_exit_in_the_phase_propagates_but_leaves_a_failed_manifest(env):
    env.outcome = SystemExit(3)
    with pytest.raises(SystemExit):
        env.render()
    assert env.openai.calls == [] and env.alerts == []  # never a fallback
    [m] = env.manifests()
    assert m["status"] == "failed" and "SystemExit" in m["error"]


def test_keyboard_interrupt_in_the_phase_is_not_turned_into_a_fallback(env):
    env.outcome = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        env.render()
    assert env.openai.calls == [] and env.alerts == []
    assert env.manifests()[0]["status"] == "failed"


def test_empty_text_is_still_rejected_for_gemini(env):
    with pytest.raises(ValueError, match="empty"):
        render.render_episode(
            "  ", CFG, env.tmp / "o.mp3", feed_slug="x", episode_id="y"
        )
    assert env.phase_calls == []


def test_openai_render_imports_neither_asr_genai_nor_the_phase_module():
    code = (
        "import sys\n"
        "from pipeline import tts\n"
        "from pipeline.tts import render\n"
        "from pipeline.tts.config import openai_config\n"
        "import pathlib, shutil, tempfile\n"
        "d = pathlib.Path(tempfile.mkdtemp())\n"
        "render._provider_for = lambda leaf: type('P', (), {'max_chars': 4096,\n"
        "    'close': lambda s: None,\n"
        "    'synthesize': lambda s, t, c: b'\\x01\\x00' * 2400})()\n"
        "render.encode_mp3 = lambda pcm, out: out.write_bytes(b'ID3')\n"
        "cfg = openai_config(model='m', voice='nova')\n"
        "render.render_episode('Hello there.', cfg,\n"
        "    d / 'o.mp3', feed_slug='f', episode_id='e',\n"
        "    manifest_dir=d / 'm', cache_dir=d / 'c')\n"
        "shutil.rmtree(d, ignore_errors=True)\n"
        "bad = [m for m in ('pipeline.tts.asr', 'pipeline.tts.gemini_phase',\n"
        "    'pipeline.tts.verify', 'google.genai') if m in sys.modules]\n"
        "print('LOADED:' + ','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("LOADED:"), out.stdout


# --- review round: telemetry and alert can never cost the episode -----------------

MALFORMED = [
    None,
    "not a dict",
    {"index": 0, "attempts": [None, "str", 7, {"synth": "s", "asr": 5}]},
    {"index": 1, "attempts": "nope"},
    {"index": 2, "attempts": [{"synth": {"status": "ok", "pcm_bytes": "x"}}]},
]


def test_malformed_progress_on_an_ok_phase_still_ships_the_audio(env):
    def outcome(chunks):
        out = ok_outcome(chunks)
        return dataclasses.replace(out, chunk_records=list(MALFORMED))

    env.outcome = outcome
    result = env.render()
    assert result.provider == "gemini" and env.alerts == []
    assert env.encoded == [b"".join(gemini_pcm(i) for i in range(N_GEMINI))]
    [m] = env.manifests()
    assert m["status"] == "rendered"
    assert set(m["gemini_phase"]["tokens"].values()) == {None}  # unknown, not wrong
    assert m["gemini_phase"]["audio_seconds_generated"] is None
    assert len(m["chunks"]) == N_GEMINI


def test_malformed_progress_on_a_failed_phase_still_falls_back(env):
    def outcome(chunks):
        return dataclasses.replace(
            failed_outcome("deadline"), chunk_records=list(MALFORMED)
        )

    env.outcome = outcome
    result = env.render()
    assert result.provider == "openai" and result.fallback_reason == "deadline"
    assert len(env.alerts) == 1 and MARK not in env.encoded[0]


def test_a_telemetry_bug_degrades_to_a_minimal_record_on_an_ok_phase(env, monkeypatch):
    def boom(records):
        raise RuntimeError("telemetry bug")

    monkeypatch.setattr(render, "_phase_totals", boom)
    env.outcome = ok_outcome
    result = env.render()
    assert result.provider == "gemini"
    [m] = env.manifests()
    phase = m["gemini_phase"]
    assert (phase["outcome"], phase["reason"]) == ("ok", None)
    assert set(phase["tokens"].values()) == {None}
    assert "RuntimeError" in phase["telemetry_error"]
    assert m["status"] == "rendered"


def test_a_telemetry_bug_does_not_stop_the_fallback(env, monkeypatch):
    def boom(records):
        raise RuntimeError("telemetry bug")

    monkeypatch.setattr(render, "_phase_totals", boom)
    env.outcome = failed_outcome("deadline")
    result = env.render()
    assert result.fallback_reason == "deadline" and len(env.alerts) == 1
    assert "telemetry_error" in env.manifests()[0]["gemini_phase"]


def test_a_bug_building_shipped_chunk_records_still_ships_the_audio(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("records bug")

    monkeypatch.setattr(render, "_attempt_errors", boom)
    env.outcome = ok_outcome
    result = env.render()
    assert result.provider == "gemini"
    [m] = env.manifests()
    assert [c["index"] for c in m["chunks"]] == list(range(N_GEMINI))
    assert sum(c["audio_seconds"] for c in m["chunks"]) == pytest.approx(N_GEMINI)


class BadStr(Exception):
    def __str__(self):
        raise RuntimeError("no string for you")


def test_an_exception_that_cannot_be_stringified_does_not_break_the_alert(env):
    env.outcome = failed_outcome("deadline")
    env.openai.script = [BadStr()]
    with pytest.raises(BadStr):
        env.render()
    (alert,) = env.alerts
    assert alert.endswith("deadline -> OpenAI nova FAILED: BadStr")
    [m] = env.manifests()
    assert m["status"] == "failed" and m["error"].startswith("BadStr")
    assert m["alert_sent"] is True


def test_a_thread_that_cannot_start_means_alert_sent_false_not_a_crash(
    env, monkeypatch
):
    class NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(render.threading, "Thread", NoThread)
    env.outcome = failed_outcome("deadline")
    result = env.render()
    assert result.fallback_reason == "deadline" and env.alerts == []
    assert env.manifests()[0]["alert_sent"] is False


def test_fallback_failure_carries_a_note_naming_the_gemini_reason(env):
    env.outcome = failed_outcome("deadline")
    env.openai.script = [KeyError("OPENAI_API_KEY")]
    with pytest.raises(KeyError) as info:
        env.render()
    assert "after Gemini deadline" in info.value.__notes__


def test_a_cache_key_failure_still_writes_a_failed_manifest(env, monkeypatch):
    def boom(text, config):
        raise ImportError("google.genai is broken")

    monkeypatch.setattr(render, "cache_key", boom)
    with pytest.raises(ImportError):
        env.render()
    [m] = env.manifests()
    assert m["status"] == "failed" and m["cache_key"] is None
    assert "ImportError" in m["error"]
    assert env.phase_calls == [] and env.alerts == []


def test_audio_generated_is_unknown_when_any_chunk_progress_is_missing(env):
    def outcome(chunks):
        out = failed_outcome("child_no_result", chunks=2)
        out.chunk_records[1] = {"index": 1, "attempts": [], "progress": "missing"}
        return out

    env.outcome = outcome
    env.render()
    assert env.manifests()[0]["gemini_phase"]["audio_seconds_generated"] is None


def test_audio_used_is_zero_unless_the_phase_was_ok(env):
    def outcome(chunks):
        return dataclasses.replace(failed_outcome("deadline"), pcm_parts=[b"\x00\x00"])

    env.outcome = outcome
    env.render()
    assert env.manifests()[0]["gemini_phase"]["audio_seconds_used"] == 0.0


@pytest.mark.parametrize(
    "parts", [lambda n: [gemini_pcm(0)], lambda n: None, lambda n: [b""] * n]
)
def test_an_ok_phase_with_the_wrong_audio_is_treated_as_invalid_and_falls_back(
    env, parts
):
    def outcome(chunks):
        return dataclasses.replace(ok_outcome(chunks), pcm_parts=parts(len(chunks)))

    env.outcome = outcome
    result = env.render()
    assert result.provider == "openai" and result.fallback_reason == "invalid_result"
    assert MARK not in env.encoded[0]
    phase = env.manifests()[0]["gemini_phase"]
    assert (phase["outcome"], phase["reason"]) == ("failed", "invalid_result")
    assert phase["audio_seconds_used"] == 0.0


def test_the_runner_error_outcome_reports_spawn_time_as_unknown(env):
    env.outcome = RuntimeError("runner bug")
    env.render()
    phase = env.manifests()[0]["gemini_phase"]
    assert phase["spawn_s"] is None and phase["child_started_s"] is None


def test_encode_failure_after_an_ok_phase_is_loud_with_a_failed_manifest(
    env, monkeypatch
):
    def boom(pcm, out):
        raise RuntimeError("ffmpeg died")

    monkeypatch.setattr(render, "encode_mp3", boom)
    env.outcome = ok_outcome
    with pytest.raises(RuntimeError, match="ffmpeg died"):
        env.render()
    assert env.openai.calls == [] and env.alerts == []  # not a Gemini problem
    [m] = env.manifests()
    assert m["status"] == "failed" and m["gemini_phase"]["outcome"] == "ok"
    assert cache.lookup(env.tmp / "c", cache.cache_key(TEXT, CFG)) is None


def test_a_cache_store_failure_does_not_lose_the_audio(env, monkeypatch):
    monkeypatch.setattr(render, "store", lambda *a, **k: False)
    env.outcome = ok_outcome
    result = env.render()
    assert result.provider == "gemini" and (env.tmp / "out.mp3").exists()
    [m] = env.manifests()
    assert m["cache_stored"] is False and m["status"] == "rendered"


# --- the alert worker: one bounded, module-level thread ------------------------------


def _workers():
    return [
        t
        for t in threading.enumerate()
        if t.name == "tts-alert-worker" and t.is_alive()
    ]


def test_a_stuck_sender_never_grows_the_thread_count_and_later_alerts_are_dropped(
    env, monkeypatch, caplog
):
    release = threading.Event()
    entered = threading.Event()

    def stuck(text, severity="info"):
        entered.set()
        release.wait(30)
        return True

    monkeypatch.setattr("pipeline.alerts.send_alert", stuck)
    monkeypatch.setattr(render, "ALERT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(render, "ALERT_QUEUE_MAX", 2)
    env.outcome = failed_outcome("deadline")
    sent = []
    try:
        for i in range(5):
            env.render(cache_dir=None, episode_id=f"ep-{i}")
            if i == 0:
                assert entered.wait(5)  # the worker now holds alert 0, stuck
            sent.append(env.manifests()[-1]["alert_sent"])
        # alert 0 is in the worker; 1 and 2 fill the queue (2); 3 and 4 are dropped.
        assert sent == ["timeout", "timeout", "timeout", "dropped", "dropped"]
        assert len(_workers()) == 1
    finally:
        release.set()
    # The dropped alert's text is logged, not lost.
    assert "TTS fallback: fp-digest ep-3" in caplog.text


def test_the_worker_is_reused_across_alerts(env):
    env.outcome = failed_outcome("deadline")
    env.render(cache_dir=None, episode_id="a")
    first = render._alert_worker
    env.render(cache_dir=None, episode_id="b")
    assert render._alert_worker is first and len(_workers()) == 1
    assert (
        env.alerts == [a for a in env.alerts if a.startswith("TTS fallback: fp-digest")]
        and len(env.alerts) == 2
    )


def test_a_dead_worker_is_restarted(env):
    env.outcome = failed_outcome("deadline")
    env.render(cache_dir=None, episode_id="a")
    old = render._alert_worker
    render._alert_queue.put(render._STOP)  # make the worker exit, as if it died
    old.join(5)
    assert not old.is_alive()
    env.render(cache_dir=None, episode_id="b")
    assert render._alert_worker is not old and render._alert_worker.is_alive()
    assert env.manifests()[-1]["alert_sent"] is True


def test_a_slow_alert_that_completes_late_does_not_break_the_next(env, monkeypatch):
    release = threading.Event()
    calls = []

    def sender(text, severity="info"):
        calls.append(text)
        if len(calls) == 1:
            release.wait(30)
        return True

    monkeypatch.setattr("pipeline.alerts.send_alert", sender)
    monkeypatch.setattr(render, "ALERT_WAIT_SECONDS", 0.05)
    env.outcome = failed_outcome("deadline")
    try:
        env.render(cache_dir=None, episode_id="slow")
        assert env.manifests()[-1]["alert_sent"] == "timeout"
    finally:
        release.set()
    monkeypatch.setattr(render, "ALERT_WAIT_SECONDS", 5.0)
    env.render(cache_dir=None, episode_id="next")
    by_episode = {m["episode_id"]: m for m in env.manifests()}
    assert by_episode["slow"]["alert_sent"] == "timeout"
    assert by_episode["next"]["alert_sent"] is True
    assert len(calls) == 2  # the slow one was still sent, late; then the next


# --- the alert names what was skipped (second_omission only) ---------------------

OMISSION_DETAIL = (
    "omission (long_unmatched_span) recall 0.912, max net 24: "
    'script "the first twelve tokens of the largest flagged span here" '
    'heard "something else entirely"'
)
ALERT_HEAD = (
    "TTS fallback: fp-digest 2026-09-30-fp: Gemini gemini-3.8-flash-lite-tts/Kore "
)


def test_second_omission_alert_names_the_chunk_and_what_was_dropped(env):
    env.outcome = failed_outcome("second_omission", detail=OMISSION_DETAIL)
    env.render()
    assert env.alerts == [
        ALERT_HEAD + f"second_omission (chunk index 1: {OMISSION_DETAIL}) "
        "-> OpenAI nova rendered"
    ]
    # the manifest carries the same detail, untruncated
    m = env.manifests()[0]
    assert m["gemini_phase"]["detail"] == OMISSION_DETAIL
    assert m["gemini_phase"]["failed_chunk"] == 1


@pytest.mark.parametrize("reason", ["deadline", "fatal", "child_error", "exhausted"])
def test_other_reasons_keep_todays_alert_text_exactly(env, reason):
    # Their details are tracebacks or SDK errors; the manifest has them.
    env.outcome = failed_outcome(reason, detail="Traceback (most recent call last): x")
    env.render()
    assert env.alerts == [ALERT_HEAD + f"{reason} -> OpenAI nova rendered"]


def test_a_long_omission_detail_is_cut_to_320_characters(env):
    detail = "omission " + "x" * 1000
    env.outcome = failed_outcome("second_omission", detail=detail)
    env.render()
    (alert,) = env.alerts
    assert f"(chunk index 1: {detail[:320]})" in alert
    assert detail[:321] not in alert


def test_the_omission_detail_is_whitespace_collapsed(env):
    env.outcome = failed_outcome("second_omission", detail="omission\n  (a,b)\t recall")
    env.render()
    (alert,) = env.alerts
    assert "(chunk index 1: omission (a,b) recall)" in alert


def test_an_omission_without_a_chunk_or_detail_degrades_cleanly():
    kw = (render.GeminiConfig("m", "Kore"), "second_omission", FALLBACK, None)

    def text(**extra):
        return render._fallback_alert_text("f", "e", *kw, **extra)

    base = "TTS fallback: f e: Gemini m/Kore second_omission -> OpenAI nova rendered"
    assert text() == base
    assert text(detail="") == base
    assert text(detail="   ") == base
    assert text(detail="d") == base.replace("second_omission", "second_omission (d)")
    assert text(detail="d", failed_chunk=2) == base.replace(
        "second_omission", "second_omission (chunk index 2: d)"
    )


def test_a_detail_that_cannot_be_stringified_never_breaks_the_alert(env):
    class BadDetail:
        def __str__(self):
            raise RuntimeError("no string for you")

    sent = render._deliver_alert(
        "fp-digest",
        "2026-09-30-fp",
        GEMINI,
        "second_omission",
        FALLBACK,
        None,
        detail=BadDetail(),
        failed_chunk=1,
    )
    assert sent is True
    assert env.alerts == [ALERT_HEAD + "second_omission -> OpenAI nova rendered"]


def test_a_failed_fallback_still_names_the_dropped_passage(env):
    env.outcome = failed_outcome("second_omission", detail=OMISSION_DETAIL)
    env.openai.script = [TTSProviderError("401 bad key", retryable=False)]
    with pytest.raises(render.TTSRenderError):
        env.render()
    (alert,) = env.alerts
    assert (
        f"second_omission (chunk index 1: {OMISSION_DETAIL}) -> OpenAI nova FAILED: "
        in alert
    )


# --- the rejected audio of omission attempts is kept ---------------------------------

CLIP_PCM = b"\x09\x00" * 4_800  # distinguishable from the episode audio


def with_clips(outcome, *clips):
    """``outcome`` with ``(chunk, n, pcm)`` clips, and the matching attempts marked."""
    records = [
        dict(r, attempts=[dict(a) for a in r["attempts"]])
        for r in outcome.chunk_records
    ]
    for i, n, _ in clips:
        for a in records[i]["attempts"]:
            if a["n"] == n:
                a["outcome"] = "omission"
                a["omission_audio"] = gp.omission_name(i, n)
    return dataclasses.replace(outcome, chunk_records=records, omission_audio=clips)


def clip_files(env):
    return sorted((env.tmp / "m" / "fp-digest" / "omission-audio").glob("*.mp3"))


def test_clips_are_encoded_next_to_the_manifests_and_recorded_on_the_attempt(env):
    env.outcome = lambda chunks: with_clips(ok_outcome(chunks), (1, 1, CLIP_PCM))
    result = env.render()
    assert result.provider == "gemini"
    [clip] = clip_files(env)
    assert clip.name.startswith("2026-09-30-fp-") and clip.name.endswith(
        "-c0001-a1.mp3"
    )
    assert clip.read_bytes() == b"ID3" + CLIP_PCM[:10]  # what encode_mp3 produced
    attempt = env.manifests()[0]["gemini_phase"]["chunks"][1]["attempts"][0]
    assert attempt["omission_audio_file"] == str(clip) and clip.is_absolute()
    assert "omission_audio_error" not in env.manifests()[0]["gemini_phase"]


def test_the_episode_audio_is_encoded_before_any_clip(env):
    env.outcome = lambda chunks: with_clips(ok_outcome(chunks), (0, 1, CLIP_PCM))
    env.render()
    assert env.encoded[0] == b"".join(gemini_pcm(i) for i in range(N_GEMINI))
    assert env.encoded[1:] == [CLIP_PCM]


def test_clips_of_a_fallback_render_are_saved_after_the_openai_audio(env):
    env.outcome = lambda chunks: with_clips(
        failed_outcome("second_omission", chunks=3), (1, 1, CLIP_PCM), (1, 2, CLIP_PCM)
    )
    result = env.render()
    assert result.provider == "openai" and result.fallback_reason == "second_omission"
    assert env.encoded[0] == OPENAI_PCM * len(env.openai.calls)
    assert env.encoded[1:] == [CLIP_PCM, CLIP_PCM]
    assert [c.name.rsplit("-", 2)[1:] for c in clip_files(env)] == [
        ["c0001", "a1.mp3"],
        ["c0001", "a2.mp3"],
    ]
    attempts = env.manifests()[0]["gemini_phase"]["chunks"][1]["attempts"]
    assert "omission_audio_file" in attempts[0]


def test_clips_are_saved_even_when_the_fallback_fails_too(env):
    env.outcome = lambda chunks: with_clips(
        failed_outcome("second_omission", chunks=3), (1, 1, CLIP_PCM)
    )
    env.openai.script = [TTSProviderError("401 bad key", retryable=False)]
    with pytest.raises(render.TTSRenderError):
        env.render()
    assert len(clip_files(env)) == 1
    [m] = env.manifests()
    assert m["status"] == "failed"
    assert "omission_audio_file" in m["gemini_phase"]["chunks"][1]["attempts"][0]


def test_clips_are_saved_when_there_is_no_fallback_and_the_phase_failed(env):
    env.outcome = lambda chunks: with_clips(
        failed_outcome("second_omission", chunks=3), (1, 1, CLIP_PCM)
    )
    with pytest.raises(render.TTSRenderError, match="Gemini phase failed"):
        env.render(CFG_NO_FALLBACK)
    assert len(clip_files(env)) == 1


def test_a_keyboard_interrupt_saves_no_clips_and_still_propagates(env):
    env.outcome = lambda chunks: with_clips(
        failed_outcome("second_omission", chunks=3), (1, 1, CLIP_PCM)
    )
    env.openai.script = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        env.render()
    assert clip_files(env) == [] and CLIP_PCM not in env.encoded


def test_without_a_manifest_dir_nothing_is_kept(env):
    env.outcome = lambda chunks: with_clips(ok_outcome(chunks), (1, 1, CLIP_PCM))
    result = env.render(manifest_dir=None)
    assert result.provider == "gemini" and result.manifest_path is None
    assert CLIP_PCM not in env.encoded
    assert list(env.tmp.rglob("omission-audio")) == []
    assert list((env.tmp / "m").rglob("*.mp3")) == []


def test_a_clip_encode_failure_is_noted_and_the_episode_still_renders(env, monkeypatch):
    env.outcome = lambda chunks: with_clips(ok_outcome(chunks), (1, 1, CLIP_PCM))
    real = render.encode_mp3

    def encode(pcm, out, **kw):
        if pcm == CLIP_PCM:
            raise RuntimeError("ffmpeg exploded")
        real(pcm, out, **kw)

    monkeypatch.setattr(render, "encode_mp3", encode)
    result = env.render()
    assert result.provider == "gemini" and (env.tmp / "out.mp3").exists()
    [m] = env.manifests()
    assert m["status"] == "rendered"
    assert m["gemini_phase"]["omission_audio_error"].startswith("RuntimeError: ffmpeg")
    assert "omission_audio_file" not in m["gemini_phase"]["chunks"][1]["attempts"][0]
    assert env.cache_result()["verification"] == "passed"


def test_a_clip_encode_failure_does_not_break_a_fallback_render(env, monkeypatch):
    env.outcome = lambda chunks: with_clips(
        failed_outcome("second_omission", chunks=3), (1, 1, CLIP_PCM)
    )
    real = render.encode_mp3

    def encode(pcm, out, **kw):
        if pcm == CLIP_PCM:
            raise OSError("no space left on device")
        real(pcm, out, **kw)

    monkeypatch.setattr(render, "encode_mp3", encode)
    result = env.render()
    assert result.provider == "openai" and (env.tmp / "out.mp3").exists()
    [m] = env.manifests()
    assert m["status"] == "rendered" and m["alert_sent"] is True
    assert m["gemini_phase"]["omission_audio_error"].startswith("OSError: no space")


def test_one_bad_clip_does_not_cost_the_others(env, monkeypatch):
    other = b"\x07\x00" * 4_800
    env.outcome = lambda chunks: with_clips(
        ok_outcome(chunks), (0, 1, CLIP_PCM), (1, 1, other)
    )
    real = render.encode_mp3

    def encode(pcm, out, **kw):
        if pcm == CLIP_PCM:
            raise RuntimeError("bad clip")
        real(pcm, out, **kw)

    monkeypatch.setattr(render, "encode_mp3", encode)
    env.render()
    [clip] = clip_files(env)
    assert clip.name.endswith("-c0001-a1.mp3")
    phase = env.manifests()[0]["gemini_phase"]
    assert phase["omission_audio_error"].startswith("RuntimeError: bad clip")
    assert "omission_audio_file" in phase["chunks"][1]["attempts"][0]


def test_an_unusable_clip_dir_is_a_noted_error_not_a_crash(env):
    env.outcome = lambda chunks: with_clips(ok_outcome(chunks), (1, 1, CLIP_PCM))
    feed_dir = env.tmp / "m" / "fp-digest"
    feed_dir.mkdir(parents=True)
    (feed_dir / "omission-audio").write_text("a file where the dir should be")
    result = env.render()
    assert result.provider == "gemini"
    assert "omission_audio_error" in env.manifests()[0]["gemini_phase"]


def test_the_error_note_is_capped(env, monkeypatch):
    env.outcome = lambda chunks: with_clips(ok_outcome(chunks), (1, 1, CLIP_PCM))
    real = render.encode_mp3

    def encode(pcm, out, **kw):
        if pcm == CLIP_PCM:
            raise RuntimeError("x" * 2000)
        real(pcm, out, **kw)

    monkeypatch.setattr(render, "encode_mp3", encode)
    env.render()
    assert len(env.manifests()[0]["gemini_phase"]["omission_audio_error"]) <= 300


def test_a_phase_with_no_clips_adds_nothing_to_the_manifest(env):
    env.outcome = ok_outcome
    env.render()
    assert clip_files(env) == []
    assert not (env.tmp / "m" / "fp-digest" / "omission-audio").exists()
    assert "omission_audio_error" not in env.manifests()[0]["gemini_phase"]


def test_the_clip_attach_survives_a_degraded_phase_record(env, monkeypatch):
    # If the manifest's gemini_phase fell back to the minimal record (no chunks),
    # the clip is still saved and nothing raises.
    monkeypatch.setattr(
        render, "_phase_totals", lambda r: (_ for _ in ()).throw(RuntimeError("x"))
    )
    env.outcome = lambda chunks: with_clips(ok_outcome(chunks), (1, 1, CLIP_PCM))
    result = env.render()
    assert result.provider == "gemini" and len(clip_files(env)) == 1
    phase = env.manifests()[0]["gemini_phase"]
    assert phase["chunks"] == [] and "telemetry_error" in phase


def test_saving_clips_never_raises_whatever_the_record_looks_like(tmp_path):
    outcome = with_clips(ok_outcome(["a", "b"]), (1, 1, CLIP_PCM))
    for record in ({}, {"gemini_phase": None}, {"gemini_phase": "nope"}):
        render._save_omission_audio(outcome, tmp_path / "d", "ep", record)
    # and with nothing to do, it does nothing at all
    render._save_omission_audio(outcome, None, "ep", {})
    render._save_omission_audio(ok_outcome(["a"]), tmp_path / "e", "ep", {})
    assert not (tmp_path / "e").exists()


# --- review round: bounded clip saving, recorded paths, alert wording ---------------


def test_the_alert_detail_cut_is_320_characters():
    assert render.ALERT_DETAIL_CHARS == 320


def test_a_realistic_omission_summary_fits_the_alert_uncut(env):
    """Two 12-token excerpts plus the boilerplate must survive the cut, or the
    alert would end mid-quote exactly where it says what was dropped."""
    script = (
        "Treasury yields climbed sharply on Thursday after the Federal Reserve "
        "chair signaled that additional interest rate reductions remain "
        "possible later this year despite persistent inflationary pressures "
        "across housing, healthcare, and transportation services."
    )
    heard = (
        "Treasury yields climbed sharply on Thursday and then the host described "
        "an entirely different segment about gardening tips and weather"
    )
    verdict = verify_audio(
        b"",
        "audio/wav",
        script,
        transcriber=lambda a, m: Transcription(heard, "m", "0", "STOP", 0.0, 1, 1, 0),
    )
    assert verdict.status == "omission"
    summary = gp._omission_summary(verdict)
    assert 200 < len(summary) < render.ALERT_DETAIL_CHARS, len(summary)
    env.outcome = failed_outcome("second_omission", detail=summary)
    env.render()
    (alert,) = env.alerts
    assert f"(chunk index 1: {summary})" in alert  # not cut


def test_clip_encodes_are_individually_bounded_and_the_episode_encode_is_not(env):
    env.outcome = lambda chunks: with_clips(
        ok_outcome(chunks), (0, 1, CLIP_PCM), (1, 1, CLIP_PCM)
    )
    env.render()
    episode, *clips = env.encode_kwargs
    assert episode == {}  # the episode keeps encode_mp3's own default bound
    assert len(clips) == 2
    for kw in clips:
        assert 0 < kw["timeout"] <= render.OMISSION_CLIP_TIMEOUT_SECONDS == 30


def test_the_clip_budget_is_sixty_seconds_in_total():
    assert render.OMISSION_CLIPS_BUDGET_SECONDS == 60


@pytest.mark.parametrize("path", ["ok", "fallback"])
def test_a_clip_encode_that_times_out_is_noted_and_the_render_completes(
    env, monkeypatch, path
):
    import subprocess

    if path == "ok":
        env.outcome = lambda chunks: with_clips(ok_outcome(chunks), (1, 1, CLIP_PCM))
    else:
        env.outcome = lambda chunks: with_clips(
            failed_outcome("second_omission", chunks=3), (1, 1, CLIP_PCM)
        )
    real = render.encode_mp3

    def encode(pcm, out, **kw):
        if pcm == CLIP_PCM:
            raise subprocess.TimeoutExpired(["ffmpeg"], kw["timeout"])
        real(pcm, out, **kw)

    monkeypatch.setattr(render, "encode_mp3", encode)
    result = env.render()
    assert (env.tmp / "out.mp3").exists()
    assert result.provider == ("gemini" if path == "ok" else "openai")
    [m] = env.manifests()
    assert m["status"] == "rendered"
    assert m["gemini_phase"]["omission_audio_error"].startswith("TimeoutExpired")
    assert clip_files(env) == []


def test_the_total_budget_stops_further_clips_and_says_so(env, monkeypatch):
    import time as _time

    monkeypatch.setattr(render, "OMISSION_CLIPS_BUDGET_SECONDS", 0.05)
    env.outcome = lambda chunks: with_clips(
        ok_outcome(chunks), (0, 1, CLIP_PCM), (1, 1, CLIP_PCM), (2, 1, CLIP_PCM)
    )
    real = render.encode_mp3

    def slow(pcm, out, **kw):
        if pcm == CLIP_PCM:
            _time.sleep(0.08)  # the first clip alone spends the whole budget
        real(pcm, out, **kw)

    monkeypatch.setattr(render, "encode_mp3", slow)
    result = env.render()
    assert result.provider == "gemini"
    [clip] = clip_files(env)
    assert clip.name.endswith("-c0000-a1.mp3")
    note = env.manifests()[0]["gemini_phase"]["omission_audio_error"]
    assert "budget" in note and "2 clip(s) skipped" in note


def test_each_clip_timeout_shrinks_to_what_is_left_of_the_budget(env, monkeypatch):
    monkeypatch.setattr(render, "OMISSION_CLIPS_BUDGET_SECONDS", 5.0)
    env.outcome = lambda chunks: with_clips(ok_outcome(chunks), (0, 1, CLIP_PCM))
    env.render()
    assert 0 < env.encode_kwargs[1]["timeout"] <= 5.0


def test_saved_clip_paths_are_listed_on_the_phase_record(env):
    env.outcome = lambda chunks: with_clips(
        ok_outcome(chunks), (0, 1, CLIP_PCM), (1, 1, CLIP_PCM)
    )
    env.render()
    files = env.manifests()[0]["gemini_phase"]["omission_audio_files"]
    on_disk = [str(p) for p in clip_files(env)]
    assert [f["file"] for f in files] == on_disk
    assert [(f["chunk"], f["attempt"]) for f in files] == [(0, 1), (1, 1)]


def test_saved_clip_paths_survive_a_degraded_phase_record(env, monkeypatch):
    monkeypatch.setattr(
        render, "_phase_totals", lambda r: (_ for _ in ()).throw(RuntimeError("x"))
    )
    env.outcome = lambda chunks: with_clips(ok_outcome(chunks), (1, 1, CLIP_PCM))
    env.render()
    phase = env.manifests()[0]["gemini_phase"]
    assert phase["chunks"] == [] and "telemetry_error" in phase
    [f] = phase["omission_audio_files"]
    assert f["file"] == str(clip_files(env)[0]) and (f["chunk"], f["attempt"]) == (1, 1)


def test_no_clips_means_no_omission_audio_files_key(env):
    env.outcome = ok_outcome
    env.render()
    assert "omission_audio_files" not in env.manifests()[0]["gemini_phase"]
