import subprocess
from array import array
from unittest.mock import patch

import pytest

from pipeline.tts import segment
from pipeline.tts.segment import (
    DECODE_TIMEOUT_SECONDS,
    decode_to_pcm,
    decode_to_pcm_with_log,
    split_pcm,
)


RATE = 24_000


def tone(seconds: float, amp: int = 8000) -> bytes:
    n = int(seconds * RATE)
    return array("h", [amp if i % 2 else -amp for i in range(n)]).tobytes()


def silence(seconds: float) -> bytes:
    return bytes(int(seconds * RATE) * 2)


def test_short_audio_is_one_segment():
    pcm = tone(100)
    assert split_pcm(pcm) == [(0, len(pcm))]


def test_long_audio_cuts_in_the_quiet_gap_near_target():
    # loud 0-293 s, silent 293-294 s, loud to 700 s
    pcm = tone(293) + silence(1) + tone(406)
    segs = split_pcm(pcm, target_s=300, search_s=15)
    assert segs[0][0] == 0 and segs[-1][1] == len(pcm)
    for (_, end), (start, _) in zip(segs, segs[1:], strict=False):
        assert end == start  # contiguous, no overlap, nothing dropped
    first_cut_s = segs[0][1] / (RATE * 2)
    assert 293 <= first_cut_s <= 294
    assert all(a % 2 == 0 and b % 2 == 0 for a, b in segs)


def test_no_quiet_point_still_cuts_within_window():
    pcm = tone(650)
    segs = split_pcm(pcm, target_s=300, search_s=15)
    assert len(segs) == 3
    assert 285 <= segs[0][1] / (RATE * 2) <= 315


def test_split_rejects_bad_args():
    with pytest.raises(ValueError):
        split_pcm(tone(1), target_s=10, search_s=10)


def test_decode_invokes_bounded_ffmpeg(tmp_path):
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(b"x")
    with patch.object(segment.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=b"\x00\x00", stderr=b""
        )
        assert decode_to_pcm(mp3) == b"\x00\x00"
    args, kwargs = run.call_args
    cmd = args[0]
    assert cmd[0] == "ffmpeg" and str(mp3) in cmd
    assert "-nostdin" in cmd
    assert cmd[cmd.index("-ar") + 1] == "24000" and cmd[cmd.index("-ac") + 1] == "1"
    assert kwargs["timeout"] == DECODE_TIMEOUT_SECONDS


def test_decode_failure_raises(tmp_path):
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(b"x")
    with patch.object(segment.subprocess, "run") as run:
        run.side_effect = subprocess.CalledProcessError(1, "ffmpeg", stderr=b"bad")
        with pytest.raises(RuntimeError, match="bad"):
            decode_to_pcm(mp3)


def test_decode_missing_ffmpeg_raises_runtime_error(tmp_path):
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(b"x")
    with patch.object(segment.subprocess, "run") as run:
        run.side_effect = FileNotFoundError(2, "No such file or directory", "ffmpeg")
        with pytest.raises(RuntimeError, match="ffmpeg not runnable"):
            decode_to_pcm(mp3)


def test_decode_with_log_keeps_stderr_on_success(tmp_path):
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(b"x")
    with patch.object(segment.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=b"\x00\x00", stderr=b"  Header missing\n"
        )
        assert decode_to_pcm_with_log(mp3) == (b"\x00\x00", "Header missing")
        assert decode_to_pcm(mp3) == b"\x00\x00"


def test_short_tail_is_never_cut_off():
    # 316 s with a quiet gap right at 315 s: the old loop cut there and left a
    # ~1 s tail. The tail must stay >= search_s, so this is one segment.
    pcm = tone(315) + silence(0.5) + tone(0.5)
    assert split_pcm(pcm, target_s=300, search_s=15) == [(0, len(pcm))]


def test_tail_is_at_least_search_s_when_cutting():
    pcm = tone(331)
    segs = split_pcm(pcm, target_s=300, search_s=15)
    assert len(segs) == 2
    tail_s = (segs[-1][1] - segs[-1][0]) / (RATE * 2)
    assert tail_s >= 15
