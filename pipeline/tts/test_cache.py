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


# --- per-chunk OpenAI PCM spool ----------------------------------------------

LEAF = OpenAIConfig("tts-1-hd", "nova")
PCM = b"\x01\x00" * 100


def _spool_files(d):
    return sorted(p.name for p in (d / "chunks").iterdir())


def test_chunk_key_is_stable_and_sensitive(monkeypatch) -> None:
    k = cache.chunk_key(LEAF, "hello")
    assert k == cache.chunk_key(LEAF, "hello")
    assert len(k) == 64 and int(k, 16) >= 0
    assert k != cache.chunk_key(LEAF, "hello!")
    assert k != cache.chunk_key(OpenAIConfig("tts-1-hd", "ash"), "hello")
    assert k != cache.chunk_key(OpenAIConfig("tts-1", "nova"), "hello")
    monkeypatch.setattr(cache, "RENDERER_VERSION", "999")
    assert k != cache.chunk_key(LEAF, "hello")


def test_spool_round_trips_under_chunks_dir(tmp_path) -> None:
    d = tmp_path / "c"
    key = cache.chunk_key(LEAF, "hello")
    assert cache.spool_lookup(d, key) is None
    assert cache.spool_store(d, key, LEAF, PCM) is True
    assert cache.spool_lookup(d, key) == PCM
    assert _spool_files(d) == [f"{key}.json", f"{key}.pcm"]
    side = json.loads((d / "chunks" / f"{key}.json").read_text())
    assert side["bytes"] == len(PCM)
    assert side["leaf"] == dataclasses.asdict(LEAF)
    assert "sha256" in side and "created" in side


def test_spool_misses_on_every_kind_of_damage(tmp_path) -> None:
    d = tmp_path / "c"

    def fresh(name):
        key = cache.chunk_key(LEAF, name)
        assert cache.spool_store(d, key, LEAF, PCM)
        return key, d / "chunks" / f"{key}.pcm", d / "chunks" / f"{key}.json"

    key, pcm, _ = fresh("truncated")
    pcm.write_bytes(PCM[:-2])
    assert cache.spool_lookup(d, key) is None

    key, pcm, _ = fresh("flipped")
    pcm.write_bytes(b"\x02" + PCM[1:])  # same length, different content
    assert cache.spool_lookup(d, key) is None

    key, pcm, _ = fresh("odd")
    pcm.write_bytes(PCM + b"\x00")
    assert cache.spool_lookup(d, key) is None

    key, pcm, _ = fresh("badsidecar")
    (d / "chunks" / f"{key}.json").write_text("{not json")
    assert cache.spool_lookup(d, key) is None

    key, pcm, side = fresh("nosidecar")
    side.unlink()
    assert cache.spool_lookup(d, key) is None

    key, pcm, side = fresh("nopcm")
    pcm.unlink()
    assert cache.spool_lookup(d, key) is None

    key, pcm, _ = fresh("empty")
    pcm.write_bytes(b"")
    assert cache.spool_lookup(d, key) is None


def test_damaged_spool_entry_is_deleted_and_can_be_rewritten(tmp_path) -> None:
    d = tmp_path / "c"
    key = cache.chunk_key(LEAF, "x")
    cache.spool_store(d, key, LEAF, PCM)
    (d / "chunks" / f"{key}.pcm").write_bytes(PCM[:-2])
    assert cache.spool_lookup(d, key) is None
    assert _spool_files(d) == []
    assert cache.spool_store(d, key, LEAF, PCM)
    assert cache.spool_lookup(d, key) == PCM


def test_spool_refuses_to_store_unusable_pcm(tmp_path, caplog) -> None:
    d = tmp_path / "c"
    key = cache.chunk_key(LEAF, "x")
    assert cache.spool_store(d, key, LEAF, b"") is False
    assert cache.spool_store(d, key, LEAF, b"\x00\x00\x00") is False
    assert cache.spool_lookup(d, key) is None
    assert "refusing to spool" in caplog.text


def test_spool_store_failure_returns_false_warns_and_leaves_no_tmp(
    tmp_path, monkeypatch, caplog
) -> None:
    d = tmp_path / "c"
    key = cache.chunk_key(LEAF, "x")

    def boom(src, dst):
        raise PermissionError("read-only")

    with monkeypatch.context() as m:
        m.setattr(cache.os, "replace", boom)
        assert cache.spool_store(d, key, LEAF, PCM) is False
    assert "spool" in caplog.text
    assert cache.spool_lookup(d, key) is None
    assert _spool_files(d) == []


def test_spool_never_raises_on_unusable_root(tmp_path) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    key = cache.chunk_key(LEAF, "x")
    assert cache.spool_store(blocker, key, LEAF, PCM) is False
    assert cache.spool_lookup(blocker, key) is None


def test_prune_never_removes_the_chunks_dir_wholesale(tmp_path) -> None:
    d = tmp_path / "c"
    old = cache.chunk_key(LEAF, "old")
    new = cache.chunk_key(LEAF, "new")
    cache.spool_store(d, old, LEAF, PCM)
    cache.spool_store(d, new, LEAF, PCM)
    # the directory itself is ancient (its mtime alone must not condemn it)
    old_time = time.time() - 20 * 86400
    for suffix in ("pcm", "json"):
        os.utime(d / "chunks" / f"{old}.{suffix}", (old_time, old_time))
    os.utime(d / "chunks", (old_time, old_time))
    cache.prune(d, max_age_days=14)
    assert (d / "chunks").is_dir()
    assert cache.spool_lookup(d, old) is None
    assert not (d / "chunks" / f"{old}.pcm").exists()
    assert not (d / "chunks" / f"{old}.json").exists()
    assert cache.spool_lookup(d, new) == PCM


def test_prune_removes_stale_spool_tmp_files(tmp_path) -> None:
    d = tmp_path / "c"
    (d / "chunks").mkdir(parents=True)
    stale = d / "chunks" / ".tmp-abc.pcm"
    stale.write_bytes(b"x")
    old_time = time.time() - 20 * 86400
    os.utime(stale, (old_time, old_time))
    cache.prune(d, max_age_days=14)
    assert not stale.exists()


def test_prune_still_removes_old_completed_entries_next_to_a_spool(tmp_path) -> None:
    d = tmp_path / "c"
    cache.store(d, "old", _mp3(tmp_path), {})
    cache.spool_store(d, cache.chunk_key(LEAF, "x"), LEAF, PCM)
    old_time = time.time() - 20 * 86400
    os.utime(d / "old", (old_time, old_time))
    cache.prune(d, max_age_days=14)
    assert not (d / "old").exists()
    assert len(_spool_files(d)) == 2


def test_completed_lookup_is_not_confused_by_the_spool_dir(tmp_path) -> None:
    d = tmp_path / "c"
    cache.spool_store(d, cache.chunk_key(LEAF, "x"), LEAF, PCM)
    assert cache.lookup(d, "chunks") is None


def test_spool_discard_removes_both_files_of_each_key_only(tmp_path) -> None:
    d = tmp_path / "c"
    a, b, keep = (cache.chunk_key(LEAF, t) for t in ("a", "b", "keep"))
    for k in (a, b, keep):
        cache.spool_store(d, k, LEAF, PCM)
    cache.spool_discard(d, [a, b, a])  # a repeated key is fine
    assert _spool_files(d) == [f"{keep}.json", f"{keep}.pcm"]


def test_spool_discard_tolerates_missing_files_and_unusable_roots(tmp_path) -> None:
    d = tmp_path / "c"
    cache.spool_discard(d, [cache.chunk_key(LEAF, "never-stored")])  # no dir at all
    cache.spool_discard(d, [])
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    cache.spool_discard(blocker, ["k"])  # must not raise


def test_spool_discard_never_raises_when_unlink_fails(tmp_path, monkeypatch) -> None:
    d = tmp_path / "c"
    key = cache.chunk_key(LEAF, "x")
    cache.spool_store(d, key, LEAF, PCM)

    def boom(self, *a, **k):
        raise PermissionError("nope")

    with monkeypatch.context() as m:
        m.setattr(type(d), "unlink", boom)
        cache.spool_discard(d, [key])
