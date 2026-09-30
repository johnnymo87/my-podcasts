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
from pipeline.tts.config import GeminiConfig, OpenAIConfig, RenderConfig
from pipeline.tts.providers import GeminiProvider, OpenAIProvider, TTSProviderError


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

        def fake_encode(pcm, out):
            self.encoded.append(pcm)
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
        return render.render_episode(
            TEXT,
            config,
            self.tmp / "out.mp3",
            feed_slug="fp-digest",
            episode_id="2026-09-30-fp",
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
    return Env(monkeypatch, tmp_path)


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
        "second_omission -> OpenAI nova FAILED: "
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
