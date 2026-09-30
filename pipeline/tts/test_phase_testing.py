"""The child-sandbox helpers used by the real-spawn tests."""

from __future__ import annotations

import pickle
import subprocess
import sys
import textwrap

import pytest

from pipeline.tts import _phase_testing as pt
from pipeline.tts.asr import pcm_to_wav
from pipeline.tts.providers import TTSProviderError


CFG = None  # the fakes ignore the config


def test_factories_pickle_by_reference():
    make_provider, make_transcriber = pt.factories("ok")
    a, b = pickle.loads(pickle.dumps((make_provider, make_transcriber)))
    assert a().synthesize_detailed(pt.fake_chunk_text(2), CFG).pcm == pt.fake_pcm(2)
    assert b(10.0).timeout_s == 10.0


def test_unknown_behavior_is_refused():
    with pytest.raises(ValueError):
        pt.factories("explode")


def test_fatal_behavior_hits_only_the_chosen_chunk():
    make_provider, _ = pt.factories("fatal_on_chunk", chunk=1)
    provider = make_provider()
    assert provider.synthesize_detailed(pt.fake_chunk_text(0), CFG).pcm
    with pytest.raises(TTSProviderError) as info:
        provider.synthesize_detailed(pt.fake_chunk_text(1), CFG)
    assert info.value.kind == "fatal"


def test_transcriber_hears_the_chunk_its_audio_came_from():
    _, make_transcriber = pt.factories("ok")
    asr = make_transcriber(5.0)
    for i in range(3):
        wav = pcm_to_wav(pt.fake_pcm(i))
        assert asr(wav, "audio/wav").text == pt.fake_chunk_text(i)


def test_omission_behavior_drops_most_of_the_chosen_chunk():
    _, make_transcriber = pt.factories("omission_on_chunk", chunk=1)
    asr = make_transcriber(5.0)
    full = asr(pcm_to_wav(pt.fake_pcm(0)), "audio/wav").text
    cut = asr(pcm_to_wav(pt.fake_pcm(1)), "audio/wav").text
    assert full == pt.fake_chunk_text(0)
    assert 0 < len(cut.split()) < len(pt.fake_chunk_text(1).split()) / 2


def test_blocking_behavior_leaves_a_marker_before_blocking(tmp_path, monkeypatch):
    class Blocked(Exception):
        pass

    def stop(_seconds):
        raise Blocked

    monkeypatch.setattr(pt.time, "sleep", stop)
    make_provider, _ = pt.factories("block_synth_forever", chunk=1, marker_dir=tmp_path)
    with pytest.raises(Blocked):
        make_provider().synthesize_detailed(pt.fake_chunk_text(1), CFG)
    assert (tmp_path / "entered-synth-1").exists()


def test_deny_network_blocks_ip_connects_and_sets_a_dummy_key():
    """Run in a subprocess: the patch is global and must not leak into the suite."""
    code = textwrap.dedent(
        """
        import os, socket
        from pipeline.tts import _phase_testing as pt
        os.environ.pop("GEMINI_API_KEY", None)
        pt.deny_network()
        assert os.environ["GEMINI_API_KEY"]
        for call in (
            lambda: socket.create_connection(("127.0.0.1", 9)),
            lambda: socket.socket().connect(("127.0.0.1", 9)),
            lambda: socket.socket().connect_ex(("127.0.0.1", 9)),
        ):
            try:
                call()
            except OSError as exc:
                assert "network denied" in str(exc), exc
            else:
                raise SystemExit("connect was not denied")
        print("denied")
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "denied"
