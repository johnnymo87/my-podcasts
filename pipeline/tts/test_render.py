from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from pipeline.tts import cache, render
from pipeline.tts.config import (
    GeminiConfig,
    OpenAIConfig,
    RenderConfig,
    openai_config,
)
from pipeline.tts.providers import TTSProviderError


CFG = openai_config(model="tts-1-hd", voice="onyx")
TEXT = "First paragraph.\n\n" + ("Second paragraph sentence. " * 200)


class FakeProvider:
    max_chars = 4096

    def __init__(self, script=None):
        self.calls: list[str] = []
        self.script = list(script or [])  # exceptions to raise, in order
        self.closed = False

    def close(self):
        self.closed = True

    def synthesize(self, text, config):
        self.calls.append(text)
        if self.script:
            exc = self.script.pop(0)
            if exc is not None:
                raise exc
        return b"\x01\x00" * 24_000  # 1 s


@pytest.fixture
def harness(monkeypatch, tmp_path):
    provider = FakeProvider()
    encoded: list[bytes] = []
    monkeypatch.setattr(render, "_provider_for", lambda config: provider)
    monkeypatch.setattr(render, "_sleep", lambda s: None)

    def fake_encode(pcm, out):
        encoded.append(pcm)
        out.write_bytes(b"ID3" + pcm[:10])

    monkeypatch.setattr(render, "encode_mp3", fake_encode)
    return provider, encoded, tmp_path


def _completed_entries(tmp) -> list:
    """Completed-render entries in the cache dir (not the chunk spool, not tmp dirs)."""
    root = tmp / "c"
    if not root.exists():
        return []
    return [
        p
        for p in root.iterdir()
        if not p.name.startswith(".") and p.name != cache.SPOOL_DIRNAME
    ]


def _spool_files(tmp) -> list[str]:
    root = tmp / "c" / cache.SPOOL_DIRNAME
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def _run(tmp_path, **kw):
    kw.setdefault("manifest_dir", tmp_path / "m")
    kw.setdefault("cache_dir", tmp_path / "c")
    return render.render_episode(
        TEXT,
        CFG,
        tmp_path / "out.mp3",
        feed_slug="fp-digest",
        episode_id="2026-09-30-fp",
        **kw,
    )


def test_renders_all_chunks_in_order_and_encodes_once(harness) -> None:
    provider, encoded, tmp = harness
    result = _run(tmp)
    assert len(provider.calls) == result.chunks >= 2
    assert " ".join(provider.calls).split() == TEXT.split()
    assert len(encoded) == 1 and len(encoded[0]) == 48_000 * result.chunks
    assert result.provider == "openai" and not result.cached
    assert (tmp / "out.mp3").exists()


def test_manifest_records_render(harness) -> None:
    _, _, tmp = harness
    result = _run(tmp)
    data = json.loads(result.manifest_path.read_text())
    assert result.manifest_path.parent == tmp / "m" / "fp-digest"
    assert result.manifest_path.name.startswith("2026-09-30-fp-")
    assert data["status"] == "rendered"
    assert data["rendered_provider"] == "openai"
    assert data["config"]["primary"]["voice"] == "onyx"
    assert data["config"]["fallback"] is None
    assert data["rendered_config"] == {
        "provider": "openai",
        "model": "tts-1-hd",
        "voice": "onyx",
    }
    assert data["total_audio_seconds"] == pytest.approx(result.chunks * 1.0)
    assert [c["attempts"] for c in data["chunks"]] == [1] * result.chunks


def test_second_render_is_a_cache_hit_with_no_provider_calls(harness) -> None:
    provider, _, tmp = harness
    _run(tmp)
    first_calls = len(provider.calls)
    (tmp / "out.mp3").unlink()
    result = _run(tmp)
    assert result.cached and len(provider.calls) == first_calls
    assert (tmp / "out.mp3").exists()
    assert json.loads(result.manifest_path.read_text())["status"] == "cache_hit"


def test_retryable_error_is_retried(harness) -> None:
    provider, _, tmp = harness
    provider.script = [TTSProviderError("500", retryable=True), None]
    result = _run(tmp)
    data = json.loads(result.manifest_path.read_text())
    assert data["chunks"][0]["attempts"] == 2
    assert "500" in data["chunks"][0]["errors"][0]


def test_non_retryable_error_fails_immediately_and_writes_manifest(harness) -> None:
    provider, _, tmp = harness
    provider.script = [TTSProviderError("401", retryable=False)]
    with pytest.raises(
        render.TTSRenderError, match=r"chunk 1/\d+ failed after 1 attempt"
    ):
        _run(tmp)
    assert len(provider.calls) == 1
    [manifest] = (tmp / "m" / "fp-digest").glob("*.json")
    assert json.loads(manifest.read_text())["status"] == "failed"


def test_retry_exhaustion_raises_after_three_attempts(harness) -> None:
    provider, _, tmp = harness
    provider.script = [TTSProviderError("503", retryable=True)] * 3
    with pytest.raises(render.TTSRenderError, match="after 3 attempt"):
        _run(tmp)
    assert len(provider.calls) == 3


def test_failed_render_is_not_cached(harness) -> None:
    provider, _, tmp = harness
    provider.script = [TTSProviderError("401", retryable=False)]
    with pytest.raises(render.TTSRenderError):
        _run(tmp)
    assert _completed_entries(tmp) == []


def test_manifest_and_cache_can_be_disabled(harness) -> None:
    _, _, tmp = harness
    result = _run(tmp, manifest_dir=None, cache_dir=None)
    assert result.manifest_path is None
    assert not (tmp / "m").exists() and not (tmp / "c").exists()


def test_unwritable_manifest_and_cache_do_not_fail_render(harness) -> None:
    _, _, tmp = harness
    blocker = tmp / "blocker"
    blocker.write_text("x")
    result = _run(tmp, manifest_dir=blocker, cache_dir=blocker)
    assert result.manifest_path is None
    assert (tmp / "out.mp3").exists()


def test_empty_text_rejected(harness) -> None:
    _, _, tmp = harness
    with pytest.raises(ValueError):
        render.render_episode(
            "  \n",
            CFG,
            tmp / "o.mp3",
            feed_slug="x",
            episode_id="y",
            manifest_dir=None,
            cache_dir=None,
        )


def test_provider_is_closed_after_success_and_failure(harness) -> None:
    provider, _, tmp = harness
    _run(tmp)
    assert provider.closed
    provider.closed = False
    provider.script = [TTSProviderError("401", retryable=False)]
    with pytest.raises(render.TTSRenderError):
        _run(tmp, cache_dir=None)
    assert provider.closed


def test_provider_not_created_on_cache_hit(harness, monkeypatch) -> None:
    _, _, tmp = harness
    _run(tmp)
    made: list[object] = []
    monkeypatch.setattr(render, "_provider_for", lambda config: made.append(1))
    assert _run(tmp).cached
    assert made == []


def test_cache_hit_reports_chunks_and_provider(harness) -> None:
    _, _, tmp = harness
    first = _run(tmp)
    hit = _run(tmp)
    assert hit.cached and hit.chunks == first.chunks
    assert hit.provider == "openai"
    data = json.loads(hit.manifest_path.read_text())
    assert data["cached"] is True
    assert data["total_audio_seconds"] == pytest.approx(first.chunks * 1.0)


def test_unexpected_error_propagates_unwrapped_with_failed_manifest(harness) -> None:
    provider, _, tmp = harness
    provider.script = [RuntimeError("no api key")]
    with pytest.raises(RuntimeError, match="no api key") as info:
        _run(tmp)
    assert not isinstance(info.value, render.TTSRenderError)
    assert len(provider.calls) == 1  # not retried
    [manifest] = (tmp / "m" / "fp-digest").glob("*.json")
    data = json.loads(manifest.read_text())
    assert data["status"] == "failed"
    assert "no api key" in data["error"]
    assert provider.closed


def test_encoder_failure_propagates_and_is_not_cached(harness, monkeypatch) -> None:
    _, _, tmp = harness

    def boom(pcm, out):
        raise RuntimeError("ffmpeg exited 1")

    monkeypatch.setattr(render, "encode_mp3", boom)
    with pytest.raises(RuntimeError, match="ffmpeg exited 1"):
        _run(tmp)
    [manifest] = (tmp / "m" / "fp-digest").glob("*.json")
    assert json.loads(manifest.read_text())["status"] == "failed"
    assert _completed_entries(tmp) == []


def test_failed_manifest_keeps_chunk_history(harness) -> None:
    provider, _, tmp = harness
    provider.script = [None, TTSProviderError("401", retryable=False)]
    with pytest.raises(render.TTSRenderError, match=r"chunk 2/\d+ failed"):
        _run(tmp)
    [manifest] = (tmp / "m" / "fp-digest").glob("*.json")
    data = json.loads(manifest.read_text())
    assert [c["attempts"] for c in data["chunks"]] == [1, 1]
    assert data["chunks"][1]["errors"] and "401" in data["chunks"][1]["errors"][0]
    assert data["total_audio_seconds"] == pytest.approx(1.0)


def test_backoff_uses_module_schedule(harness, monkeypatch) -> None:
    provider, _, tmp = harness
    slept: list[float] = []
    monkeypatch.setattr(render, "_sleep", slept.append)
    provider.script = [TTSProviderError("503", retryable=True)] * 2
    _run(tmp)
    assert slept == [2.0, 8.0]


def test_two_attempts_write_distinct_manifests(harness) -> None:
    _, _, tmp = harness
    _run(tmp)
    _run(tmp)
    assert len(list((tmp / "m" / "fp-digest").glob("*.json"))) == 2


def test_unknown_provider_fails_loudly() -> None:
    with pytest.raises(ValueError, match="provider"):
        render._provider_for(SimpleNamespace(provider="nope"))


def test_provider_for_builds_openai_provider() -> None:
    assert isinstance(
        render._provider_for(OpenAIConfig("tts-1-hd", "nova")), render.OpenAIProvider
    )


def _only_entry(tmp):
    [entry] = _completed_entries(tmp)
    return entry


def test_malformed_cache_stats_do_not_fail_hit(harness) -> None:
    _, _, tmp = harness
    _run(tmp)
    entry = _only_entry(tmp)
    res = json.loads((entry / "result.json").read_text())
    res["chunks"] = "many"
    (entry / "result.json").write_text(json.dumps(res))
    hit = _run(tmp)
    assert hit.cached and hit.chunks == 0 and hit.provider == "openai"


def test_result_json_records_provenance(harness) -> None:
    _, _, tmp = harness
    result = _run(tmp)
    res = json.loads((_only_entry(tmp) / "result.json").read_text())
    assert res["schema"] == 2
    assert res["provider"] == "openai"
    assert res["requested"] == dataclasses.asdict(CFG)
    assert res["rendered"] == dataclasses.asdict(CFG.primary)
    assert res["verification"] == "not_run_openai"
    assert res["fallback_reason"] is None
    assert res["renderer_version"] == "2"
    assert res["chunks"] == result.chunks
    assert res["total_audio_seconds"] == pytest.approx(result.chunks * 1.0)
    assert res["rendered_at"]


def test_rendered_leaf_on_render_and_cache_hit(harness) -> None:
    _, _, tmp = harness
    first = _run(tmp)
    assert first.rendered == OpenAIConfig("tts-1-hd", "onyx")
    assert first.config == CFG
    hit = _run(tmp)
    assert hit.cached and hit.rendered == OpenAIConfig("tts-1-hd", "onyx")
    assert hit.provider == "openai"


def _store_entry(tmp, rendered: OpenAIConfig, *, chunks: int = 3) -> None:
    (tmp / "out.mp3").write_bytes(b"ID3x")
    res = {
        "schema": 2,
        "provider": "openai",
        "requested": dataclasses.asdict(RenderConfig(rendered)),
        "rendered": dataclasses.asdict(rendered),
        "verification": "not_run_openai",
        "fallback_reason": None,
        "renderer_version": "2",
        "chunks": chunks,
        "total_audio_seconds": 7.5,
    }
    assert cache.store(tmp / "c", cache.cache_key(TEXT, CFG), tmp / "out.mp3", res)


def test_cache_hit_reports_provenance_from_the_entry(harness) -> None:
    provider, _, tmp = harness
    _store_entry(tmp, CFG.primary)
    hit = _run(tmp)
    assert hit.cached and hit.chunks == 3 and not provider.calls
    assert hit.provider == "openai" and hit.rendered == CFG.primary
    data = json.loads(hit.manifest_path.read_text())
    assert data["rendered_config"] == dataclasses.asdict(CFG.primary)


def test_entry_rendered_by_a_different_leaf_is_a_miss(harness, caplog) -> None:
    provider, _, tmp = harness
    _store_entry(tmp, OpenAIConfig("tts-1-hd", "echo"))  # request asks for onyx
    with caplog.at_level("WARNING", logger="pipeline.tts.render"):
        result = _run(tmp)
    assert not result.cached and provider.calls
    assert result.rendered == CFG.primary
    assert "re-rendering" in caplog.text
    # The re-render replaced the mismatched entry with a matching one.
    assert _run(tmp).cached


@pytest.mark.parametrize(
    "rendered,expected",
    [
        (OpenAIConfig("tts-1-hd", "echo"), True),  # the fallback
        (GeminiConfig("g", "Kore"), True),  # the primary
        (OpenAIConfig("tts-1-hd", "onyx"), False),
        (GeminiConfig("g", "Puck"), False),
    ],
)
def test_hit_matches_request_primary_or_fallback(rendered, expected) -> None:
    cfg = RenderConfig(GeminiConfig("g", "Kore"), OpenAIConfig("tts-1-hd", "echo"))
    assert render._hit_matches_request(rendered, cfg) is expected


def test_hit_matches_request_without_fallback() -> None:
    assert render._hit_matches_request(CFG.primary, CFG)
    assert not render._hit_matches_request(OpenAIConfig("tts-1", "onyx"), CFG)


def test_entry_without_provenance_is_a_miss_and_is_overwritten(harness) -> None:
    provider, _, tmp = harness
    (tmp / "out.mp3").write_bytes(b"ID3x")
    assert cache.store(
        tmp / "c",
        cache.cache_key(TEXT, CFG),
        tmp / "out.mp3",
        {"provider": "openai", "chunks": 1, "total_audio_seconds": 1.0},
    )
    result = _run(tmp)
    assert not result.cached and provider.calls
    assert cache.lookup(tmp / "c", cache.cache_key(TEXT, CFG)) is not None


def test_failed_manifest_has_no_rendered_config(harness) -> None:
    provider, _, tmp = harness
    provider.script = [TTSProviderError("401", retryable=False)]
    with pytest.raises(render.TTSRenderError):
        _run(tmp)
    [manifest] = (tmp / "m" / "fp-digest").glob("*.json")
    data = json.loads(manifest.read_text())
    assert data["rendered_config"] is None
    assert data["config"]["primary"]["voice"] == "onyx"


def test_cache_hit_copy_failure_is_atomic_and_falls_back(harness, monkeypatch) -> None:
    provider, _, tmp = harness
    _run(tmp)
    first_calls = len(provider.calls)
    out = tmp / "out.mp3"
    out.write_bytes(b"previous good file")

    real_copy = render.shutil.copyfile

    def partial_then_fail(src, dst, *a, **kw):
        if str(src).startswith(str(tmp / "c")):
            with open(dst, "wb") as f:
                f.write(b"PARTIAL")
            raise OSError("disk full")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(render.shutil, "copyfile", partial_then_fail)
    # Re-render path must not be poisoned by the failed copy.
    result = _run(tmp)
    # The first render completed, so its spool was cleared: this is a real
    # re-render (new provider purchases), not a replay, and not PARTIAL.
    assert not result.cached and len(provider.calls) > first_calls
    assert out.read_bytes().startswith(b"ID3")
    assert sorted(p.name for p in tmp.iterdir() if p.name.startswith("out.mp3")) == [
        "out.mp3"
    ]


def test_cache_hit_copy_failure_never_exposes_partial_file(
    harness, monkeypatch
) -> None:
    provider, encoded, tmp = harness
    _run(tmp)
    out = tmp / "out.mp3"
    out.write_bytes(b"previous good file")
    seen: list[bytes] = []

    def partial_then_fail(src, dst, *a, **kw):
        with open(dst, "wb") as f:
            f.write(b"PARTIAL")
        raise OSError("disk full")

    def spy_encode(pcm, dest):
        seen.append(out.read_bytes())  # state of out_mp3 when re-render begins
        dest.write_bytes(b"ID3re")

    monkeypatch.setattr(render.shutil, "copyfile", partial_then_fail)
    monkeypatch.setattr(render, "encode_mp3", spy_encode)
    result = _run(tmp)
    assert not result.cached
    assert seen == [b"previous good file"]


def test_retry_logs_warning_with_chunk_attempt_error_and_delay(harness, caplog) -> None:
    provider, _, tmp = harness
    provider.script = [TTSProviderError("boom503", retryable=True), None]
    with caplog.at_level("WARNING", logger="pipeline.tts.render"):
        _run(tmp)
    [rec] = [r for r in caplog.records if r.levelname == "WARNING"]
    msg = rec.getMessage()
    assert "chunk 1/" in msg and "attempt 1" in msg
    assert "boom503" in msg and "2.0" in msg


def test_backoff_schedule_must_match_attempts() -> None:
    render._check_retry_schedule(3, (2.0, 8.0))
    with pytest.raises(RuntimeError, match="BACKOFF_SECONDS"):
        render._check_retry_schedule(4, (2.0, 8.0))


def test_manifest_has_chunk_count_finished_at_and_out_bytes(harness) -> None:
    _, _, tmp = harness
    rendered = _run(tmp)
    data = json.loads(rendered.manifest_path.read_text())
    assert data["chunk_count"] == rendered.chunks == len(data["chunks"])
    assert data["finished_at"] >= data["started_at"]
    assert data["out_bytes"] == (tmp / "out.mp3").stat().st_size

    hit = _run(tmp)
    hdata = json.loads(hit.manifest_path.read_text())
    assert hdata["chunk_count"] == hit.chunks
    assert hdata["finished_at"] and hdata["out_bytes"] == data["out_bytes"]


def test_failed_manifest_has_chunk_count_and_finished_at(harness) -> None:
    provider, _, tmp = harness
    provider.script = [TTSProviderError("401", retryable=False)]
    with pytest.raises(render.TTSRenderError):
        _run(tmp)
    [manifest] = (tmp / "m" / "fp-digest").glob("*.json")
    data = json.loads(manifest.read_text())
    assert data["chunk_count"] >= 2 and data["finished_at"]
    assert data["out_bytes"] is None


def test_default_dirs_resolve_at_call_time(harness, monkeypatch) -> None:
    from pipeline.tts import cache, manifest

    _, _, tmp = harness
    monkeypatch.setattr(manifest, "DEFAULT_MANIFEST_DIR", tmp / "dm")
    monkeypatch.setattr(cache, "DEFAULT_CACHE_DIR", tmp / "dc")
    result = render.render_episode(
        TEXT, CFG, tmp / "out.mp3", feed_slug="fp-digest", episode_id="ep"
    )
    assert result.manifest_path is not None
    assert result.manifest_path.is_relative_to(tmp / "dm")
    assert any((tmp / "dc").iterdir())


def test_default_dirs_are_isolated_from_persist_by_fixture(harness) -> None:
    from pipeline.tts import cache, manifest

    assert not str(manifest.DEFAULT_MANIFEST_DIR).startswith("/persist")
    assert not str(cache.DEFAULT_CACHE_DIR).startswith("/persist")


# --- per-chunk PCM spool (my-podcasts-9p3.10) --------------------------------

TEXT3 = "\n\n".join(
    f"Paragraph {i}. " + "Sentence number one here. " * 100 for i in range(3)
)


class CountingProvider(FakeProvider):
    """Each synthesize call returns audio no other call returns, so a reused
    chunk is distinguishable from a re-bought one."""

    def synthesize(self, text, config):
        super().synthesize(text, config)
        return bytes([len(self.calls), 0]) * 24_000


def _run3(tmp_path, cfg=CFG, **kw):
    kw.setdefault("manifest_dir", tmp_path / "m")
    kw.setdefault("cache_dir", tmp_path / "c")
    return render.render_episode(
        TEXT3, cfg, tmp_path / "out.mp3", feed_slug="fp-digest", episode_id="e", **kw
    )


@pytest.fixture
def spool_env(monkeypatch, tmp_path):
    provider = CountingProvider()
    encoded: list[bytes] = []
    monkeypatch.setattr(render, "_provider_for", lambda config: provider)
    monkeypatch.setattr(render, "_sleep", lambda s: None)

    def fake_encode(pcm, out):
        encoded.append(pcm)
        out.write_bytes(b"ID3" + pcm[:10])

    monkeypatch.setattr(render, "encode_mp3", fake_encode)
    return provider, encoded, tmp_path


def _chunk_texts():
    from pipeline.tts.chunker import chunk_text

    chunks = chunk_text(TEXT3, ceiling=4096)
    assert len(chunks) == 3
    return chunks


def test_retry_after_partial_failure_resynthesizes_only_failed_and_later_chunks(
    spool_env,
) -> None:
    provider, encoded, tmp = spool_env
    chunks = _chunk_texts()
    provider.script = [None, TTSProviderError("400", retryable=False)]  # chunk 2 fails
    with pytest.raises(render.TTSRenderError, match=r"chunk 2/3"):
        _run3(tmp)
    assert provider.calls == chunks[:2]
    [failed] = (tmp / "m" / "fp-digest").glob("*.json")
    failed_chunks = json.loads(failed.read_text())["chunks"]
    assert [c["spooled"] for c in failed_chunks] == [False, False]

    provider.calls.clear()
    result = _run3(tmp)
    assert provider.calls == chunks[1:]  # chunk 1 came from the spool
    # Chunk 1's audio is the FIRST run's call-1 audio, not a fresh purchase.
    pcm = [bytes([n, 0]) * 24_000 for n in (1, 1, 2)]
    assert encoded == [pcm[0] + pcm[1] + pcm[2]]
    data = json.loads(result.manifest_path.read_text())
    assert [c["spooled"] for c in data["chunks"]] == [True, False, False]
    assert [c["attempts"] for c in data["chunks"]] == [0, 1, 1]
    assert data["total_audio_seconds"] == pytest.approx(3.0)


def _partial_run(tmp, provider, *, fail_at: int = 2, **kw) -> None:
    """A render that buys chunks ``0..fail_at-1`` then dies on chunk ``fail_at``."""
    provider.script = [None] * fail_at + [TTSProviderError("400", retryable=False)]
    with pytest.raises(render.TTSRenderError):
        _run3(tmp, **kw)
    provider.script = []
    provider.calls.clear()


def test_successful_render_leaves_no_spool_files(spool_env) -> None:
    _, _, tmp = spool_env
    _run3(tmp)
    assert _spool_files(tmp) == []
    assert len(_completed_entries(tmp)) == 1


def test_failed_render_keeps_its_spool_files(spool_env) -> None:
    provider, _, tmp = spool_env
    provider.script = [None, TTSProviderError("400", retryable=False)]
    with pytest.raises(render.TTSRenderError):
        _run3(tmp)
    assert len(_spool_files(tmp)) == 2  # chunk 1 only: .pcm + .json


def test_render_whose_completed_store_fails_keeps_its_spool_files(
    spool_env, monkeypatch
) -> None:
    provider, _, tmp = spool_env
    monkeypatch.setattr(render, "store", lambda *a, **k: False)
    result = _run3(tmp)
    assert result.chunks == 3 and _completed_entries(tmp) == []
    assert len(_spool_files(tmp)) == 6  # 3 chunks x (.pcm + .json)
    # ... and that is what makes the next attempt cheap.
    provider.calls.clear()
    _run3(tmp)
    assert provider.calls == []


def test_retry_that_succeeds_consumes_and_clears_the_spool(spool_env) -> None:
    provider, _, tmp = spool_env
    _partial_run(tmp, provider, fail_at=2)
    assert len(_spool_files(tmp)) == 4
    result = _run3(tmp)
    assert not result.cached
    assert _spool_files(tmp) == []
    assert len(_completed_entries(tmp)) == 1


def test_spool_cleanup_failure_never_fails_the_render(spool_env, monkeypatch) -> None:
    provider, _, tmp = spool_env

    def boom(*a, **k):
        raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(render, "spool_discard", boom)
    result = _run3(tmp)
    assert result.chunks == 3 and len(_completed_entries(tmp)) == 1


def test_manifest_chunk_records_name_their_spool_key(spool_env) -> None:
    provider, _, tmp = spool_env
    chunks = _chunk_texts()
    provider.script = [None, TTSProviderError("400", retryable=False)]
    with pytest.raises(render.TTSRenderError):
        _run3(tmp)
    [failed] = (tmp / "m" / "fp-digest").glob("*.json")
    recs = json.loads(failed.read_text())["chunks"]
    # The failing chunk is named too, so its (absent) files can be looked for.
    assert [r["spool_key"] for r in recs] == [
        cache.chunk_key(CFG.primary, c) for c in chunks[:2]
    ]
    key0 = recs[0]["spool_key"]
    assert (tmp / "c" / "chunks" / f"{key0}.pcm").is_file()
    assert (tmp / "c" / "chunks" / f"{key0}.json").is_file()


def test_spool_key_is_none_without_a_cache_dir(spool_env) -> None:
    _, _, tmp = spool_env
    result = _run3(tmp, cache_dir=None)
    data = json.loads(result.manifest_path.read_text())
    assert [c["spool_key"] for c in data["chunks"]] == [None] * 3


def test_a_render_that_fails_at_encode_keeps_every_chunk_for_the_retry(
    spool_env, monkeypatch
) -> None:
    provider, encoded, tmp = spool_env
    real_encode = render.encode_mp3

    def boom(pcm, out):
        raise RuntimeError("ffmpeg exited 1")

    monkeypatch.setattr(render, "encode_mp3", boom)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        _run3(tmp)
    assert len(_spool_files(tmp)) == 6
    monkeypatch.setattr(render, "encode_mp3", real_encode)
    provider.calls.clear()
    result = _run3(tmp)
    assert not result.cached and provider.calls == []  # fully spooled: no purchases
    assert _spool_files(tmp) == []


def test_same_text_with_a_different_voice_or_model_is_a_miss(spool_env) -> None:
    provider, _, tmp = spool_env
    _partial_run(tmp, provider, fail_at=2)
    _run3(tmp, cfg=openai_config(model="tts-1-hd", voice="ash"))
    assert len(provider.calls) == 3
    provider.calls.clear()
    _run3(tmp, cfg=openai_config(model="tts-1", voice="onyx"))
    assert len(provider.calls) == 3
    # Neither render touched the first config's leftovers.
    assert len(_spool_files(tmp)) == 4


@pytest.mark.parametrize("damage", ["truncate", "sidecar", "delete_sidecar"])
def test_damaged_spool_entry_is_a_miss_and_is_resynthesized(spool_env, damage) -> None:
    provider, _, tmp = spool_env
    chunks = _chunk_texts()
    _partial_run(tmp, provider, fail_at=2)  # spool holds chunks 1 and 2
    key = cache.chunk_key(CFG.primary, chunks[1])
    pcm_path = tmp / "c" / "chunks" / f"{key}.pcm"
    side_path = tmp / "c" / "chunks" / f"{key}.json"
    if damage == "truncate":
        pcm_path.write_bytes(pcm_path.read_bytes()[:-2])
    elif damage == "sidecar":
        side_path.write_text("{garbage")
    else:
        side_path.unlink()
    _run3(tmp)
    assert provider.calls == chunks[1:]  # chunk 1 reused, damaged chunk 2 re-bought


def test_spool_write_failure_never_fails_the_render(spool_env, monkeypatch, caplog):
    provider, encoded, tmp = spool_env

    def boom(path, data):
        raise PermissionError("read-only")

    monkeypatch.setattr(cache, "_write_atomic", boom)
    result = _run3(tmp)
    assert (tmp / "out.mp3").exists() and len(encoded) == 1
    assert len(provider.calls) == 3 and result.chunks == 3
    assert "spool store failed" in caplog.text


def test_spool_read_failure_never_fails_the_render(spool_env, monkeypatch):
    provider, _, tmp = spool_env

    def boom(*a, **k):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(render, "spool_lookup", boom)
    result = _run3(tmp)
    assert result.chunks == 3 and len(provider.calls) == 3


def test_no_cache_dir_means_no_spool_is_touched(spool_env, monkeypatch) -> None:
    provider, _, tmp = spool_env

    def forbidden(*a, **k):
        raise AssertionError("spool touched with cache_dir=None")

    monkeypatch.setattr(render, "spool_lookup", forbidden)
    monkeypatch.setattr(render, "spool_store", forbidden)
    result = _run3(tmp, cache_dir=None)
    assert result.chunks == 3
    data = json.loads(result.manifest_path.read_text())
    assert [c["spooled"] for c in data["chunks"]] == [False] * 3
    assert not (tmp / "c").exists()
