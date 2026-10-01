from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time

import pytest

from pipeline.tts import cache
from pipeline.tts.config import (
    GeminiConfig,
    OpenAIConfig,
    RenderConfig,
    openai_config,
)


CFG = openai_config(model="tts-1-hd", voice="nova")
GEMINI = GeminiConfig(model="gemini-3.8-flash-lite-tts", voice="Kore")


def _result(rendered=None, **over) -> dict:
    """A provenance-valid result.json body."""
    rendered = rendered or OpenAIConfig("tts-1-hd", "nova")
    base = {
        "schema": 2,
        "provider": rendered.provider,
        "requested": dataclasses.asdict(RenderConfig(rendered)),
        "rendered": dataclasses.asdict(rendered),
        "verification": "passed" if rendered.provider == "gemini" else "not_run_openai",
        "fallback_reason": None,
        "renderer_version": cache.RENDERER_VERSION,
        "chunks": 1,
        "total_audio_seconds": 1.0,
    }
    base.update(over)
    return base


def _mp3(tmp_path, data=b"ID3fake"):
    p = tmp_path / "in.mp3"
    p.write_bytes(data)
    return p


def test_key_is_stable_and_sensitive(monkeypatch) -> None:
    k = cache.cache_key("hello", CFG)
    assert k == cache.cache_key("hello", CFG)
    assert k != cache.cache_key("hello!", CFG)
    assert k != cache.cache_key("hello", openai_config(model="tts-1-hd", voice="ash"))
    monkeypatch.setattr(cache, "RENDERER_VERSION", "999")
    assert k != cache.cache_key("hello", CFG)


def test_store_then_lookup_round_trips(tmp_path) -> None:
    d = tmp_path / "c"
    res = _result()
    assert cache.store(d, "k1", _mp3(tmp_path), res)
    hit = cache.lookup(d, "k1")
    assert hit is not None
    assert hit.audio.read_bytes() == b"ID3fake"
    assert hit.result == res
    assert hit.rendered == OpenAIConfig("tts-1-hd", "nova")


def test_lookup_misses(tmp_path) -> None:
    d = tmp_path / "c"
    assert cache.lookup(d, "absent") is None
    (d / "half").mkdir(parents=True)
    (d / "half" / "audio.mp3").write_bytes(b"x")  # no result.json: half-written
    assert cache.lookup(d, "half") is None
    (d / "badjson").mkdir()
    (d / "badjson" / "audio.mp3").write_bytes(b"x")
    (d / "badjson" / "result.json").write_text("{not json")
    assert cache.lookup(d, "badjson") is None
    (d / "empty").mkdir()
    (d / "empty" / "audio.mp3").write_bytes(b"")
    (d / "empty" / "result.json").write_text("{}")
    assert cache.lookup(d, "empty") is None


def test_store_existing_key_is_success_and_keeps_first(tmp_path) -> None:
    d = tmp_path / "c"
    assert cache.store(d, "k", _mp3(tmp_path, b"first"), _result(chunks=1))
    assert cache.store(d, "k", _mp3(tmp_path, b"second"), _result(chunks=2))
    assert cache.lookup(d, "k").audio.read_bytes() == b"first"
    assert not [p for p in d.iterdir() if p.name.startswith(".tmp-")]


def test_store_failure_returns_false_and_warns(tmp_path, caplog) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    assert cache.store(blocker, "k", _mp3(tmp_path), {}) is False
    assert "reduced retry protection" in caplog.text


def test_lookup_never_raises_on_unreadable_root(tmp_path) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    assert cache.lookup(blocker, "k") is None


def test_prune_removes_old_entries_only(tmp_path) -> None:
    d = tmp_path / "c"
    cache.store(d, "old", _mp3(tmp_path), {})
    cache.store(d, "new", _mp3(tmp_path), {})
    old_time = time.time() - 20 * 86400
    os.utime(d / "old", (old_time, old_time))
    cache.prune(d, max_age_days=14)
    assert not (d / "old").exists()
    assert (d / "new").exists()
    json.loads((d / "new" / "result.json").read_text())


def test_store_replaces_corrupt_existing_entry(tmp_path) -> None:
    d = tmp_path / "c"
    for name, files in {
        "half": {"audio.mp3": b"x"},  # no result.json
        "empty": {"audio.mp3": b"", "result.json": b"{}"},
        "badjson": {"audio.mp3": b"x", "result.json": b"{nope"},
    }.items():
        (d / name).mkdir(parents=True)
        for fname, data in files.items():
            (d / name / fname).write_bytes(data)
        assert cache.lookup(d, name) is None
        assert cache.store(d, name, _mp3(tmp_path, b"fresh"), _result())
        hit = cache.lookup(d, name)
        assert hit is not None and hit.audio.read_bytes() == b"fresh"
    assert not [p for p in d.iterdir() if p.name.startswith(".tmp-")]


def test_prune_survives_one_entry_stat_failure(tmp_path, monkeypatch) -> None:
    d = tmp_path / "c"
    names = ["a", "b", "c", "d"]
    old_time = time.time() - 20 * 86400
    for n in names:
        cache.store(d, n, _mp3(tmp_path), {})
        os.utime(d / n, (old_time, old_time))
    real_is_dir = type(d).is_dir
    failed: list[str] = []

    def flaky_once(self):
        if self.parent == d and not failed:
            failed.append(self.name)
            raise FileNotFoundError("raced away")
        return real_is_dir(self)

    monkeypatch.setattr(type(d), "is_dir", flaky_once)
    cache.prune(d, max_age_days=14)
    monkeypatch.undo()
    [raced] = failed
    assert (d / raced).exists()  # skipped, not fatal
    assert [n for n in names if n != raced and (d / n).exists()] == []


def test_key_changes_with_fallback() -> None:
    a = RenderConfig(GEMINI, None)
    b = RenderConfig(GEMINI, OpenAIConfig("tts-1-hd", "echo"))
    c = RenderConfig(GEMINI, OpenAIConfig("tts-1-hd", "onyx"))
    keys = {cache.cache_key("t", x) for x in (a, b, c)}
    assert len(keys) == 3


def test_gemini_key_changes_with_verifier_policy(monkeypatch) -> None:
    from pipeline.tts import verify

    cfg = RenderConfig(GEMINI, OpenAIConfig("tts-1-hd", "echo"))
    k = cache.cache_key("t", cfg)
    monkeypatch.setattr(verify, "VERIFIER_POLICY", "other-policy")
    assert cache.cache_key("t", cfg) != k


def test_gemini_key_moves_with_the_asr_thinking_setting(monkeypatch) -> None:
    """Renders verified under the old implicit thinking-default are not reused."""
    from pipeline.tts import asr, verify

    cfg = RenderConfig(GEMINI, OpenAIConfig("tts-1-hd", "echo"))
    assert verify.VERIFIER_POLICY.endswith("|thinking-low")
    k = cache.cache_key("t", cfg)
    old = f"verifier-v{verify.VERIFIER_VERSION}|{asr.policy_for(thinking='default')}"
    monkeypatch.setattr(verify, "VERIFIER_POLICY", old)
    assert cache.cache_key("t", cfg) != k


def test_openai_key_ignores_verifier_policy(monkeypatch) -> None:
    from pipeline.tts import verify

    k = cache.cache_key("t", CFG)
    monkeypatch.setattr(verify, "VERIFIER_POLICY", "other-policy")
    assert cache.cache_key("t", CFG) == k


def test_renderer_version_is_2() -> None:
    assert cache.RENDERER_VERSION == "2"


def test_openai_cache_key_does_not_import_google_genai() -> None:
    code = (
        "import sys, pipeline.tts\n"
        "from pipeline.tts.cache import cache_key\n"
        "cache_key('x', pipeline.tts.openai_config(model='tts-1-hd', voice='nova'))\n"
        "assert 'google.genai' not in sys.modules, 'google.genai was imported'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("schema"),
        lambda r: r.update(schema=1),
        lambda r: r.pop("rendered"),
        lambda r: r.update(rendered={"provider": "nope", "model": "m", "voice": "v"}),
        lambda r: r.update(provider="gemini"),  # disagrees with rendered (openai)
        lambda r: r.update(verification="passed"),  # openai rendered, "passed"
        lambda r: r.pop("verification"),
        lambda r: r.update(verification="failed"),
    ],
)
def test_lookup_misses_on_bad_provenance(tmp_path, mutate) -> None:
    d = tmp_path / "c"
    res = _result()
    mutate(res)
    assert cache.store(d, "k", _mp3(tmp_path), res)
    assert cache.lookup(d, "k") is None


def test_lookup_misses_on_gemini_not_verified(tmp_path) -> None:
    d = tmp_path / "c"
    res = _result(GEMINI, verification="not_run_openai")
    assert cache.store(d, "k", _mp3(tmp_path), res)
    assert cache.lookup(d, "k") is None


def test_lookup_hits_on_verified_gemini(tmp_path) -> None:
    d = tmp_path / "c"
    assert cache.store(d, "k", _mp3(tmp_path), _result(GEMINI))
    hit = cache.lookup(d, "k")
    assert hit is not None and hit.rendered == GEMINI


def test_store_replaces_entry_with_invalid_provenance(tmp_path) -> None:
    d = tmp_path / "c"
    assert cache.store(d, "k", _mp3(tmp_path, b"old"), {"provider": "openai"})
    assert cache.lookup(d, "k") is None
    assert cache.store(d, "k", _mp3(tmp_path, b"new"), _result())
    hit = cache.lookup(d, "k")
    assert hit is not None and hit.audio.read_bytes() == b"new"
