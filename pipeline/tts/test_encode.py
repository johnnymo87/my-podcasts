from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.tts import encode


def test_encode_invokes_ffmpeg_with_pinned_format(monkeypatch, tmp_path) -> None:
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        Path(cmd[-1]).write_bytes(b"mp3")  # ffmpeg writes to the temp path
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(encode.subprocess, "run", fake_run)
    out = tmp_path / "x.mp3"
    encode.encode_mp3(b"\x00\x00" * 10, out)
    cmd = seen["cmd"]
    assert cmd[0] == "ffmpeg"
    for flag, value in [("-f", "s16le"), ("-codec:a", "libmp3lame"), ("-b:a", "32k")]:
        assert cmd[cmd.index(flag) + 1] == value
    assert cmd.count("-ar") == 2 and cmd.count("-ac") == 2
    for i, arg in enumerate(cmd):
        if arg == "-ar":
            assert cmd[i + 1] == "24000"
        if arg == "-ac":
            assert cmd[i + 1] == "1"
    # Output goes to a temp sibling, then is atomically renamed into place.
    assert cmd[-1] != str(out)
    assert Path(cmd[-1]).parent == out.parent
    assert cmd[cmd.index("-f", cmd.index("-i")) + 1] == "mp3"
    assert out.read_bytes() == b"mp3"
    assert list(tmp_path.iterdir()) == [out]
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
            "stream=codec_name,sample_rate,channels,bit_rate",
            "-of",
            "csv=p=0",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    codec, rate, channels, bit_rate = probe.stdout.strip().split(",")
    assert (codec, rate, channels) == ("mp3", "24000", "1")
    assert 31_000 <= int(bit_rate) <= 33_000


def test_real_subprocess_failure_raises_runtime_error_with_stderr(
    monkeypatch, tmp_path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ffmpeg"
    fake.write_text("#!/bin/sh\ncat >/dev/null\necho 'No such thing' >&2\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with pytest.raises(RuntimeError, match=r"ffmpeg exited 3: No such thing"):
        encode.encode_mp3(b"\x00\x00" * 100, out_dir / "x.mp3")
    assert list(out_dir.iterdir()) == []


def test_missing_output_dir_raises_oserror_not_runtime_error(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        encode.encode_mp3(b"\x00\x00", tmp_path / "no-such-dir" / "x.mp3")


def test_concurrent_encodes_use_distinct_temp_files(monkeypatch, tmp_path) -> None:
    temps = []

    def fake_run(cmd, **kwargs):
        temps.append(cmd[-1])
        Path(cmd[-1]).write_bytes(b"mp3")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(encode.subprocess, "run", fake_run)
    out = tmp_path / "x.mp3"
    encode.encode_mp3(b"\x00\x00", out)
    encode.encode_mp3(b"\x00\x00", out)
    assert all(t.endswith(".tmp") and Path(t).name.startswith("x.mp3.") for t in temps)
    assert list(tmp_path.iterdir()) == [out]


def test_failed_encode_leaves_no_output_or_temp_file(monkeypatch, tmp_path) -> None:
    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"partial")  # ffmpeg got partway
        raise subprocess.CalledProcessError(1, cmd, stderr=b"boom \xff")

    monkeypatch.setattr(encode.subprocess, "run", fake_run)
    out = tmp_path / "x.mp3"
    with pytest.raises(RuntimeError, match="ffmpeg exited 1: boom"):
        encode.encode_mp3(b"\x00\x00", out)
    assert list(tmp_path.iterdir()) == []


def test_failed_encode_does_not_clobber_existing_output(monkeypatch, tmp_path) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr=b"")

    monkeypatch.setattr(encode.subprocess, "run", fake_run)
    out = tmp_path / "x.mp3"
    out.write_bytes(b"previous good render")
    with pytest.raises(RuntimeError):
        encode.encode_mp3(b"\x00\x00", out)
    assert out.read_bytes() == b"previous good render"
    assert list(tmp_path.iterdir()) == [out]


def test_stderr_tail_is_capped(monkeypatch, tmp_path) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr=b"x" * 10_000 + b"END")

    monkeypatch.setattr(encode.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        encode.encode_mp3(b"\x00\x00", tmp_path / "x.mp3")
    assert str(exc.value).endswith("END")
    assert len(str(exc.value)) < 2_100


def test_encode_timeout_defaults_to_the_module_bound_and_can_be_overridden(
    monkeypatch, tmp_path
) -> None:
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs["timeout"])
        Path(cmd[-1]).write_bytes(b"mp3")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(encode.subprocess, "run", fake_run)
    encode.encode_mp3(b"\x00\x00", tmp_path / "a.mp3")
    encode.encode_mp3(b"\x00\x00", tmp_path / "b.mp3", timeout=30)
    assert encode.ENCODE_TIMEOUT_SECONDS == 600
    assert seen == [600, 30]


def test_a_timed_out_encode_raises_and_leaves_no_temp_file(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(encode.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        encode.encode_mp3(b"\x00\x00", tmp_path / "x.mp3", timeout=1)
    assert list(tmp_path.iterdir()) == []
