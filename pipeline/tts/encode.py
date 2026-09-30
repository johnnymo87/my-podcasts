"""Encode the joined PCM once, matching today's published format exactly:
mp3, 24 kHz, mono, 32 kbps (measured on published episodes 2026-09-30)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline.tts.providers import PCM_SAMPLE_RATE


ENCODE_TIMEOUT_SECONDS = 600


def encode_mp3(pcm: bytes, out_mp3: Path) -> None:
    rate = str(PCM_SAMPLE_RATE)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "s16le",
            "-ar",
            rate,
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "32k",
            "-ar",
            rate,
            "-ac",
            "1",
            str(out_mp3),
        ],
        input=pcm,
        check=True,
        capture_output=True,
        timeout=ENCODE_TIMEOUT_SECONDS,
    )
