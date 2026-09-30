from __future__ import annotations

import shutil
import subprocess

import pytest

from pipeline.tts import encode


def test_encode_invokes_ffmpeg_with_pinned_format(monkeypatch, tmp_path) -> None:
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(encode.subprocess, "run", fake_run)
    out = tmp_path / "x.mp3"
    encode.encode_mp3(b"\x00\x00" * 10, out)
    cmd = seen["cmd"]
    assert cmd[0] == "ffmpeg"
    for flag, value in [("-f", "s16le"), ("-codec:a", "libmp3lame"), ("-b:a", "32k")]:
        assert cmd[cmd.index(flag) + 1] == value
    assert cmd.count("24000") == 2 and cmd.count("1") >= 2
    assert cmd[-1] == str(out)
    assert seen["kwargs"]["input"] == b"\x00\x00" * 10
    assert seen["kwargs"]["check"] is True
    assert seen["kwargs"]["timeout"] > 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_encode_real_ffmpeg_produces_24k_mono_32kbps(tmp_path) -> None:
    out = tmp_path / "tone.mp3"
    encode.encode_mp3(b"\x00\x10" * 24_000 * 2, out)  # 2 s of constant signal
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,sample_rate,channels",
            "-of",
            "csv=p=0",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert probe.stdout.strip() == "mp3,24000,1"
