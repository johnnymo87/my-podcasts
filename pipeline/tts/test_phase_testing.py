"""The child-sandbox helpers used by the real-spawn tests."""

from __future__ import annotations

import multiprocessing
import pickle

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


def test_fatal_and_block_others_fails_only_the_target_after_a_sibling_blocks(
    tmp_path, monkeypatch
):
    class Blocked(Exception):
        pass

    def stop(_seconds):
        raise Blocked

    make_provider, _ = pt.factories(
        "fatal_and_block_others", chunk=0, marker_dir=tmp_path
    )
    provider = make_provider()
    monkeypatch.setattr(pt.time, "sleep", stop)
    with pytest.raises(Blocked):  # chunk 1 blocks, leaving its marker
        provider.synthesize_detailed(pt.fake_chunk_text(1), CFG)
    assert (tmp_path / "entered-synth-1").read_text().isdigit()
    with pytest.raises(TTSProviderError) as info:  # now chunk 0 may fail
        provider.synthesize_detailed(pt.fake_chunk_text(0), CFG)
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


def test_deny_network_refuses_to_run_in_the_test_process():
    with pytest.raises(RuntimeError, match="spawned child"):
        pt.deny_network()


def test_exit_on_build_refuses_to_run_in_the_test_process():
    make_provider, _ = pt.factories("exit_on_build")
    with pytest.raises(RuntimeError, match="spawned child"):
        make_provider()


def test_deny_network_blocks_ip_connects_in_a_spawned_child():
    """A real spawn: the patch is global and must never reach the suite."""
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    proc = ctx.Process(target=pt._selftest_deny_network, args=(results,))
    proc.start()
    try:
        outcome = results.get(timeout=60)
    finally:
        proc.join(10)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
    assert outcome["key_set"] is True
    for name in ("create_connection", "connect", "connect_ex"):
        assert "network denied" in outcome[name], outcome
