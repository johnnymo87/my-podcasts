"""Changing output here requires bumping cache.RENDERER_VERSION.

Encode the joined PCM once, matching today's published format exactly:
mp3, 24 kHz, mono, 32 kbps (measured on published episodes 2026-09-30)."""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path

from pipeline.tts.config import PCM_SAMPLE_RATE


ENCODE_TIMEOUT_SECONDS = 600
_STDERR_TAIL_CHARS = 2000


def encode_mp3(
    pcm: bytes, out_mp3: Path, timeout: float = ENCODE_TIMEOUT_SECONDS
) -> None:
    """Encode ``pcm`` to ``out_mp3`` atomically.

    ffmpeg writes a temp sibling that is renamed into place only on success, so
    a failed or killed encode never leaves a truncated file at ``out_mp3`` (a
    later reuse would otherwise ship it) and never clobbers a previous good one.
    ``timeout`` bounds the ffmpeg run (``subprocess.TimeoutExpired`` on expiry);
    the default is the episode-sized ``ENCODE_TIMEOUT_SECONDS``.
    """
    rate = str(PCM_SAMPLE_RATE)
    # Unique per call: two encodes targeting one out_mp3 must not share a temp.
    fd, tmp_name = tempfile.mkstemp(
        dir=out_mp3.parent, prefix=out_mp3.name + ".", suffix=".tmp"
    )
    os.close(fd)  # ffmpeg -y overwrites the empty placeholder
    tmp = Path(tmp_name)
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
                timeout=timeout,
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
