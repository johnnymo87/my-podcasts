"""Test-only helpers for the Gemini phase child process.

An importable, non-test module (and so free of pytest) because a spawned child
cannot see the parent's monkeypatches: everything it needs must be importable by
name and picklable. The parent hands the child ``factories(...)`` -- a pair of
``functools.partial`` objects over module-level functions here -- plus
``deny_network`` as the child bootstrap, and the child builds its fake provider
and transcriber from them.

The fakes never touch the network. Audio is synthetic: chunk ``i``'s PCM is
``fake_pcm(i)`` and its ASR transcript is exactly ``fake_chunk_text(i)`` (so the
real verifier passes it), which lets a test check that chunk files come back in
order and untouched.

Behaviors (``factories(behavior, chunk=...)``):

``ok``                      every chunk synthesizes and verifies
``fatal_on_chunk``          synth of ``chunk`` raises a fatal ``TTSProviderError``
``block_synth_forever``     synth of ``chunk`` writes ``entered-synth-<i>`` into
                            ``marker_dir`` then sleeps 3600 s
``block_asr_forever``       same, in the transcriber (``entered-asr-<i>``)
``omission_on_chunk``       the transcript of ``chunk`` covers only its first words
``exit_on_build``           building the provider calls ``os._exit(1)`` at once
"""

from __future__ import annotations

import functools
import io
import json
import os
import re
import socket
import time
import wave
from pathlib import Path

from pipeline.tts.asr import Transcription
from pipeline.tts.providers import Synthesis, TTSProviderError


BEHAVIORS = frozenset(
    {
        "ok",
        "fatal_on_chunk",
        "block_synth_forever",
        "block_asr_forever",
        "omission_on_chunk",
        "exit_on_build",
    }
)
_BLOCK_SECONDS = 3600.0
_WORDS_PER_CHUNK = 60
_OMISSION_KEEP_WORDS = 8
_CHUNK_RE = re.compile(r"^Chunk (\d+)\b")


def deny_network() -> None:
    """Child bootstrap: make any outbound IP connect raise, and set a dummy key.

    Unix-domain connects are left alone (they are local, and some stdlib
    machinery uses them). The key is a dummy so a code path that checks for one
    does not fail before it reaches the fake.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _is_ip(sock: socket.socket) -> bool:
        return sock.family in (socket.AF_INET, socket.AF_INET6)

    def connect(self, address):
        if _is_ip(self):
            raise OSError("network denied in test child")
        return real_connect(self, address)

    def connect_ex(self, address):
        if _is_ip(self):
            raise OSError("network denied in test child")
        return real_connect_ex(self, address)

    def create_connection(*args, **kwargs):
        raise OSError("network denied in test child")

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.create_connection = create_connection
    os.environ["GEMINI_API_KEY"] = "dummy-key-for-offline-tests"


def fake_chunk_text(i: int) -> str:
    """Chunk ``i``'s script text: distinct words, long enough to verify."""
    words = [f"alpha{i}x{j}" for j in range(_WORDS_PER_CHUNK)]
    return f"Chunk {i} " + " ".join(words) + "."


def make_chunks(n: int) -> list[str]:
    return [fake_chunk_text(i) for i in range(n)]


def fake_pcm(i: int) -> bytes:
    """0.1 s of 24 kHz mono s16 PCM whose every sample is ``i + 1``."""
    return (i + 1).to_bytes(2, "little") * 2400


def _chunk_index(text: str) -> int:
    m = _CHUNK_RE.match(text)
    if m is None:
        raise ValueError(f"not a _phase_testing chunk: {text[:40]!r}")
    return int(m.group(1))


def _pcm_index(wav_bytes: bytes) -> int:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        first = w.readframes(1)
    return int.from_bytes(first, "little") - 1


def _mark(spec: dict, name: str) -> None:
    marker_dir = spec.get("marker_dir")
    if marker_dir:
        Path(marker_dir, name).write_text("entered")


def _block() -> None:
    time.sleep(_BLOCK_SECONDS)


class FakeProvider:
    def __init__(self, spec: dict) -> None:
        self._spec = spec

    def synthesize_detailed(self, text, cfg, *, timeout=None) -> Synthesis:
        i = _chunk_index(text)
        behavior, target = self._spec["behavior"], self._spec.get("chunk")
        if behavior == "fatal_on_chunk" and i == target:
            raise TTSProviderError(f"fake fatal on chunk {i}", kind="fatal")
        if behavior == "block_synth_forever" and i == target:
            _mark(self._spec, f"entered-synth-{i}")
            _block()
        return Synthesis(
            pcm=fake_pcm(i),
            finish_reason="STOP",
            prompt_tokens=10,
            audio_tokens=100,
            elapsed_s=0.0,
        )

    def close(self) -> None:
        pass


class FakeTranscriber:
    def __init__(self, spec: dict, timeout_s: float) -> None:
        self._spec = spec
        self.timeout_s = timeout_s

    def __call__(self, audio: bytes, mime_type: str) -> Transcription:
        i = _pcm_index(audio)
        behavior, target = self._spec["behavior"], self._spec.get("chunk")
        if behavior == "block_asr_forever" and i == target:
            _mark(self._spec, f"entered-asr-{i}")
            _block()
        text = fake_chunk_text(i)
        if behavior == "omission_on_chunk" and i == target:
            text = " ".join(text.split()[:_OMISSION_KEEP_WORDS])
        return Transcription(
            text=text,
            model="fake-asr",
            prompt_version="0",
            finish_reason="STOP",
            elapsed_s=0.0,
            input_tokens=20,
            output_tokens=30,
            thinking_tokens=0,
        )

    def close(self) -> None:
        pass


def make_fake_provider(spec_json: str) -> FakeProvider:
    spec = json.loads(spec_json)
    if spec["behavior"] == "exit_on_build":
        os._exit(1)
    return FakeProvider(spec)


def make_fake_transcriber(spec_json: str, timeout_s: float) -> FakeTranscriber:
    return FakeTranscriber(json.loads(spec_json), timeout_s)


def factories(
    behavior: str = "ok",
    *,
    chunk: int | None = None,
    marker_dir: str | os.PathLike[str] | None = None,
) -> tuple:
    """The ``(make_provider, make_transcriber)`` pair for a behavior.

    Both are ``functools.partial`` over module-level functions, so they pickle
    by reference and cross the spawn boundary.
    """
    if behavior not in BEHAVIORS:
        raise ValueError(f"unknown behavior {behavior!r}")
    spec = json.dumps(
        {
            "behavior": behavior,
            "chunk": chunk,
            "marker_dir": None if marker_dir is None else os.fspath(marker_dir),
        }
    )
    return (
        functools.partial(make_fake_provider, spec),
        functools.partial(make_fake_transcriber, spec),
    )
