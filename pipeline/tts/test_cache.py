from __future__ import annotations

import json
import os
import time

from pipeline.tts import cache
from pipeline.tts.config import openai_config


CFG = openai_config(model="tts-1-hd", voice="nova")


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
    assert cache.store(d, "k1", _mp3(tmp_path), {"provider": "openai"})
    hit = cache.lookup(d, "k1")
    assert hit is not None
    assert hit.audio.read_bytes() == b"ID3fake"
    assert hit.result == {"provider": "openai"}


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
    assert cache.store(d, "k", _mp3(tmp_path, b"first"), {"n": 1})
    assert cache.store(d, "k", _mp3(tmp_path, b"second"), {"n": 2})
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
