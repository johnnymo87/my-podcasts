from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import podcast_writer as pw


GOOD = "<summary>S</summary><script>" + "word " * 600 + "</script>"


class _Fetch:
    """Stand-in for report_engine.fetch_report_text (keyword-only ``label``)."""

    def __init__(self, reply: str = GOOD) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def __call__(self, instruction: str, *, label: str) -> str:
        self.calls.append((instruction, label))
        return self.reply


def _write(tmp_path: Path, **kw: str):
    args = {"transcript": "T", "title": "t", "description": "d"} | kw
    return pw.write_report(work_dir=tmp_path, **args)


def test_generates_and_persists_then_reuses(tmp_path, monkeypatch) -> None:
    fetch = _Fetch()
    monkeypatch.setattr(pw, "fetch_report_text", fetch)
    out = _write(tmp_path)
    assert out.script.startswith("word")
    assert out.summary == "S"
    assert len(list(tmp_path.glob("raw_writer_output-*.txt"))) == 1
    again = _write(tmp_path)
    assert again == out
    assert len(fetch.calls) == 1  # reused, no second model call


def test_instruction_carries_transcript_title_and_description(
    tmp_path, monkeypatch
) -> None:
    fetch = _Fetch()
    monkeypatch.setattr(pw, "fetch_report_text", fetch)
    _write(tmp_path, transcript="UNIQUE-TRANSCRIPT", title="TTL", description="DSC")
    instruction, label = fetch.calls[0]
    assert "UNIQUE-TRANSCRIPT" in instruction
    assert "TTL" in instruction and "DSC" in instruction
    assert label == "argument"


@pytest.mark.parametrize(
    "change",
    [
        {"transcript": "T2"},
        {"title": "t2"},
        {"description": "d2"},
    ],
)
def test_changed_input_does_not_reuse(tmp_path, monkeypatch, change) -> None:
    fetch = _Fetch()
    monkeypatch.setattr(pw, "fetch_report_text", fetch)
    _write(tmp_path)
    _write(tmp_path, **change)
    assert len(fetch.calls) == 2
    assert len(list(tmp_path.glob("raw_writer_output-*.txt"))) == 2


def test_new_prompt_version_does_not_reuse(tmp_path, monkeypatch) -> None:
    fetch = _Fetch()
    monkeypatch.setattr(pw, "fetch_report_text", fetch)
    _write(tmp_path)
    monkeypatch.setattr(pw, "AUDIO_REPORT_PROMPT_VERSION", "next")
    _write(tmp_path)
    assert len(fetch.calls) == 2


def test_field_boundaries_are_not_ambiguous(tmp_path, monkeypatch) -> None:
    fetch = _Fetch()
    monkeypatch.setattr(pw, "fetch_report_text", fetch)
    _write(tmp_path, title="ab", description="c")
    _write(tmp_path, title="a", description="bc")
    assert len(fetch.calls) == 2


def test_refusal_unlinks_raw_and_reraises(tmp_path, monkeypatch) -> None:
    fetch = _Fetch("no tags here " * 400)
    monkeypatch.setattr(pw, "fetch_report_text", fetch)
    with pytest.raises(RuntimeError):
        _write(tmp_path)
    assert list(tmp_path.glob("raw_writer_output-*.txt")) == []
    # next retry regenerates instead of looping on the broken file
    fetch.reply = GOOD
    assert _write(tmp_path).script.startswith("word")
    assert len(fetch.calls) == 2


def test_short_script_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        pw, "fetch_report_text", _Fetch("<summary>S</summary><script>tiny</script>")
    )
    with pytest.raises(RuntimeError, match="too short"):
        _write(tmp_path)
    assert list(tmp_path.glob("raw_writer_output-*.txt")) == []


def test_fetch_failure_persists_nothing(tmp_path, monkeypatch) -> None:
    def boom(instruction: str, *, label: str) -> str:
        raise RuntimeError("opencode timed out")

    monkeypatch.setattr(pw, "fetch_report_text", boom)
    with pytest.raises(RuntimeError, match="timed out"):
        _write(tmp_path)
    assert list(tmp_path.iterdir()) == []
