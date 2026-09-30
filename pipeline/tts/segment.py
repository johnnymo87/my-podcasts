"""Decode an episode mp3 and split long audio for transcription (tts-verify only).

Whole episodes run 12-23 minutes; ASR runs on ~5-minute pieces. Cuts land on
the quietest 100 ms window within +-search_s of each target, so a word is
rarely split. Segments are contiguous and non-overlapping: nothing dropped,
nothing duplicated. A word cut at a boundary is a documented limitation.
"""

from __future__ import annotations

import subprocess
from array import array
from pathlib import Path

from pipeline.tts.config import PCM_BYTES_PER_SECOND, PCM_SAMPLE_RATE


DECODE_TIMEOUT_SECONDS = 300
_WINDOW_S = 0.1


def decode_to_pcm_with_log(path: Path) -> tuple[bytes, str]:
    """Decode to 24 kHz mono s16le; returns ``(pcm, ffmpeg_stderr)``.

    ffmpeg's stderr on a *successful* decode (``-v error``: e.g. a damaged
    frame it skipped) is evidence about the audio, so it is kept, stripped and
    truncated.
    """
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(path),
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        str(PCM_SAMPLE_RATE),
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd, check=True, capture_output=True, timeout=DECODE_TIMEOUT_SECONDS
        )
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode(errors="replace")[-2000:]
        raise RuntimeError(f"ffmpeg decode of {path} failed: {tail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg decode of {path} timed out") from exc
    except OSError as exc:  # FileNotFoundError (no ffmpeg), PermissionError, ...
        raise RuntimeError(f"ffmpeg not runnable: {exc!r}") from exc
    warnings = (proc.stderr or b"").decode(errors="replace").strip()[-2000:]
    return proc.stdout, warnings


def decode_to_pcm(path: Path) -> bytes:
    return decode_to_pcm_with_log(path)[0]


def _quietest_cut(pcm: bytes, lo_s: float, hi_s: float) -> int:
    samples = array("h")
    lo = int(lo_s * PCM_SAMPLE_RATE)
    hi = int(hi_s * PCM_SAMPLE_RATE)
    samples.frombytes(pcm[lo * 2 : hi * 2])
    win = int(_WINDOW_S * PCM_SAMPLE_RATE)
    best_i, best_energy = 0, None
    for i in range(0, max(1, len(samples) - win + 1), win):
        energy = sum(abs(s) for s in samples[i : i + win])
        if best_energy is None or energy < best_energy:
            best_i, best_energy = i, energy
    return (lo + best_i + win // 2) * 2


def split_pcm(
    pcm: bytes, *, target_s: float = 300.0, search_s: float = 15.0
) -> list[tuple[int, int]]:
    """Return contiguous ``(start, end)`` byte ranges covering ``pcm``."""
    if search_s <= 0 or search_s >= target_s:
        raise ValueError("need 0 < search_s < target_s")
    total_s = len(pcm) / PCM_BYTES_PER_SECOND
    segments: list[tuple[int, int]] = []
    start = 0
    # 2 * search_s, not 1: a cut may land up to search_s past the target, and
    # the tail left behind must still be at least search_s long.
    while (total_s - start / PCM_BYTES_PER_SECOND) > target_s + 2 * search_s:
        base = start / PCM_BYTES_PER_SECOND + target_s
        cut = _quietest_cut(pcm, base - search_s, base + search_s)
        segments.append((start, cut))
        start = cut
    segments.append((start, len(pcm)))
    return segments
