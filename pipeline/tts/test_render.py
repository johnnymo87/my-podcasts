from __future__ import annotations

import json

import pytest

from pipeline.tts import render
from pipeline.tts.config import openai_config
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
    assert data["config"]["openai_voice"] == "onyx"
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
    assert not (tmp / "c").exists() or not any(
        p for p in (tmp / "c").iterdir() if not p.name.startswith(".")
    )


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
    assert not (tmp / "c").exists() or not any(
        p for p in (tmp / "c").iterdir() if not p.name.startswith(".")
    )


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


def test_unknown_provider_fails_loudly(tmp_path) -> None:
    from dataclasses import replace

    with pytest.raises(ValueError, match="provider"):
        render.render_episode(
            TEXT,
            replace(CFG, provider="nope"),  # type: ignore[arg-type]
            tmp_path / "o.mp3",
            feed_slug="x",
            episode_id="y",
            manifest_dir=None,
            cache_dir=None,
        )


def test_malformed_cache_result_does_not_fail_hit(harness) -> None:
    _, _, tmp = harness
    _run(tmp)
    [entry] = [p for p in (tmp / "c").iterdir() if not p.name.startswith(".")]
    (entry / "result.json").write_text('{"chunks": "many", "provider": "openai"}')
    hit = _run(tmp)
    assert hit.cached and hit.chunks == 0 and hit.provider == "openai"
