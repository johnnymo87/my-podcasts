"""Encode the joined PCM once, matching today's published format exactly:
mp3, 24 kHz, mono, 32 kbps (measured on published episodes 2026-09-30)."""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

from pipeline.tts.config import PCM_SAMPLE_RATE


ENCODE_TIMEOUT_SECONDS = 600
_STDERR_TAIL_CHARS = 2000


def encode_mp3(pcm: bytes, out_mp3: Path) -> None:
    """Encode ``pcm`` to ``out_mp3`` atomically.

    ffmpeg writes a temp sibling that is renamed into place only on success, so
    a failed or killed encode never leaves a truncated file at ``out_mp3`` (a
    later reuse would otherwise ship it) and never clobbers a previous good one.
    """
    rate = str(PCM_SAMPLE_RATE)
    tmp = out_mp3.with_name(out_mp3.name + ".tmp")
    try:
        try:
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
                    # Explicit: the ".tmp" suffix gives ffmpeg nothing to infer from.
                    "-f",
                    "mp3",
                    str(tmp),
                ],
                input=pcm,
                check=True,
                capture_output=True,
                timeout=ENCODE_TIMEOUT_SECONDS,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(
                f"ffmpeg exited {e.returncode}: {stderr[-_STDERR_TAIL_CHARS:]}"
            ) from e
        os.replace(tmp, out_mp3)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
