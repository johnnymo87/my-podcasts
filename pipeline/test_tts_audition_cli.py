"""Tests for the `tts-audition` CLI command. Everything runs offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pipeline.__main__ import cli
from pipeline.tts.render import RenderResult, TTSRenderError


LITE = "gemini-3.8-flash-lite-tts"
SCRIPT = "First paragraph of the script.\n\nSecond paragraph of the script.\n\nThird."


class FakeRender:
    def __init__(self, fail_voices=()):
        self.fail_voices = set(fail_voices)
        self.calls = []

    def __call__(self, text, config, out_mp3, **kwargs):
        self.calls.append((text, config, kwargs))
        leaf = config.primary
        out_mp3.write_bytes(b"audio")
        if leaf.voice in self.fail_voices:
            raise TTSRenderError("Gemini phase failed: fatal")
        return RenderResult(
            provider=leaf.provider,
            config=config,
            rendered=leaf,
            cached=False,
            chunks=2,
            manifest_path=None,
        )


@pytest.fixture
def fake_render(monkeypatch):
    fake = FakeRender()
    monkeypatch.setattr("pipeline.tts.render.render_episode", fake)
    return fake


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    # The suite deletes real keys; these dummies are never used by a real client
    # because render_episode is replaced in every test that gets past preflight.
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key")


@pytest.fixture
def script_file(tmp_path: Path) -> Path:
    p = tmp_path / "script.txt"
    p.write_text(SCRIPT)
    return p


def run(script_file, out_dir, *extra, feed="the-rundown"):
    return CliRunner().invoke(
        cli,
        [
            "tts-audition",
            "--feed",
            feed,
            "--script",
            str(script_file),
            "--out-dir",
            str(out_dir),
            *extra,
        ],
    )


def test_happy_path(fake_render, script_file, tmp_path):
    out = tmp_path / "out"
    result = run(script_file, out, "--voices", "Kore,Puck", "--models", LITE)
    assert result.exit_code == 0, result.output
    names = sorted(p.name for p in out.glob("*.mp3"))
    assert names == [
        f"the-rundown--gemini--{LITE}--Kore.mp3",
        f"the-rundown--gemini--{LITE}--Puck.mp3",
        "the-rundown--openai--tts-1-hd--nova.mp3",
    ]
    lines = result.output.splitlines()
    assert sum(ln.startswith("OK      ") for ln in lines) == 3
    assert lines[-1] == f"tts-audition: 3 ok, 0 FAILED; files in {out}"
    assert (out / "script.txt").read_text() == SCRIPT
    summary = json.loads((out / "summary.json").read_text())
    assert summary["style"] == "calm, measured news anchor"
    assert len(fake_render.calls) == 3
    # default style reaches the Gemini leaf
    gemini_leaf = fake_render.calls[1][1].primary
    assert gemini_leaf.style == "calm, measured news anchor"


def test_no_openai_and_empty_style(fake_render, script_file, tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    out = tmp_path / "out"
    result = run(
        script_file,
        out,
        "--voices",
        "Kore",
        "--models",
        LITE,
        "--style",
        "",
        "--no-openai",
    )
    assert result.exit_code == 0, result.output
    assert [p.name for p in out.glob("*.mp3")] == [
        f"the-rundown--gemini--{LITE}--Kore.mp3"
    ]
    assert fake_render.calls[0][1].primary.style == ""


def test_one_failure_exits_1(script_file, tmp_path, monkeypatch):
    fake = FakeRender(fail_voices={"Puck"})
    monkeypatch.setattr("pipeline.tts.render.render_episode", fake)
    out = tmp_path / "out"
    result = run(script_file, out, "--voices", "Kore,Puck", "--models", LITE)
    assert result.exit_code == 1
    assert f"FAILED  gemini/{LITE}/Puck  Gemini phase failed: fatal" in result.output
    assert result.output.splitlines()[-1] == (
        f"tts-audition: 2 ok, 1 FAILED; files in {out}"
    )
    assert not (out / f"the-rundown--gemini--{LITE}--Puck.mp3").exists()
    assert not list(out.glob(".partial-*"))


def test_missing_gemini_key_is_usage_error(
    fake_render, script_file, tmp_path, monkeypatch
):
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    result = run(script_file, tmp_path / "out", "--voices", "Kore")
    assert result.exit_code == 2
    assert "GEMINI_API_KEY" in result.output
    assert "sk-super-secret-value" not in result.output
    assert fake_render.calls == []
    assert not (tmp_path / "out").exists()


def test_missing_openai_key_only_matters_for_the_baseline(
    fake_render, script_file, tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY")
    result = run(script_file, tmp_path / "out", "--voices", "Kore")
    assert result.exit_code == 2
    assert "OPENAI_API_KEY" in result.output
    assert "dummy-gemini-key" not in result.output
    assert fake_render.calls == []


def test_unknown_feed(fake_render, script_file, tmp_path):
    result = run(script_file, tmp_path / "out", "--voices", "Kore", feed="nope")
    assert result.exit_code == 2
    assert "nope" in result.output
    assert fake_render.calls == []


def test_openai_voice_in_voices(fake_render, script_file, tmp_path):
    result = run(script_file, tmp_path / "out", "--voices", "Kore,nova")
    assert result.exit_code == 2
    assert "OpenAI voice" in result.output
    assert fake_render.calls == []


def test_voices_is_required(fake_render, script_file, tmp_path):
    result = run(script_file, tmp_path / "out")
    assert result.exit_code == 2
    assert "--voices" in result.output


def test_max_chars_without_boundary_is_refused(fake_render, tmp_path):
    # A long unbroken paragraph is refused, never cut mid-sentence.
    long_script = tmp_path / "long.txt"
    long_script.write_text("word " * 100)
    result = run(
        long_script, tmp_path / "out", "--voices", "Kore", "--max-chars", "200"
    )
    assert result.exit_code == 2
    assert "paragraph" in result.output
    assert not (tmp_path / "out").exists()
    assert fake_render.calls == []


def test_max_chars_longer_than_script_renders_it_whole(
    fake_render, script_file, tmp_path
):
    out = tmp_path / "out"
    result = run(script_file, out, "--voices", "Kore", "--max-chars", "200")
    assert result.exit_code == 0, result.output
    assert (out / "script.txt").read_text() == SCRIPT


def test_max_chars_cuts_at_paragraph_boundary(fake_render, tmp_path):
    paragraphs = ["a" * 150, "b" * 150, "c" * 150]
    script = tmp_path / "s.txt"
    script.write_text("\n\n".join(paragraphs))
    out = tmp_path / "out"
    result = run(script, out, "--voices", "Kore", "--max-chars", "400")
    assert result.exit_code == 0, result.output
    expected = "\n\n".join(paragraphs[:2])
    assert (out / "script.txt").read_text() == expected
    assert {call[0] for call in fake_render.calls} == {expected}


def test_max_chars_minimum(fake_render, script_file, tmp_path):
    result = run(script_file, tmp_path / "out", "--voices", "Kore", "--max-chars", "50")
    assert result.exit_code == 2
    assert "--max-chars" in result.output


def test_existing_out_dir_is_usage_error(fake_render, script_file, tmp_path):
    out = tmp_path / "out"
    first = run(script_file, out, "--voices", "Kore", "--no-openai")
    assert first.exit_code == 0, first.output
    n_calls = len(fake_render.calls)
    snapshot = sorted(p.name for p in out.iterdir())
    second = run(script_file, out, "--voices", "Kore", "--no-openai")
    assert second.exit_code == 2
    assert "already exists" in second.output
    assert len(fake_render.calls) == n_calls
    assert sorted(p.name for p in out.iterdir()) == snapshot


def test_empty_existing_out_dir_is_usage_error(fake_render, script_file, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    result = run(script_file, out, "--voices", "Kore")
    assert result.exit_code == 2
    assert "already exists" in result.output
    assert fake_render.calls == []
    assert list(out.iterdir()) == []


def test_force_option_is_gone(fake_render, script_file, tmp_path):
    result = run(script_file, tmp_path / "out", "--voices", "Kore", "--force")
    assert result.exit_code == 2
    assert "--force" in result.output
    assert fake_render.calls == []


def test_blank_script_is_usage_error(fake_render, tmp_path):
    blank = tmp_path / "blank.txt"
    blank.write_text("  \n")
    result = run(blank, tmp_path / "out", "--voices", "Kore")
    assert result.exit_code == 2
    assert fake_render.calls == []


def test_help_documents_the_defaults():
    from pipeline.tts.audition import DEFAULT_MODELS, DEFAULT_STYLE

    result = CliRunner().invoke(cli, ["tts-audition", "--help"])
    assert result.exit_code == 0
    # click wraps help text, sometimes mid-token: compare without whitespace.
    flat = "".join(result.output.split())
    assert "".join(DEFAULT_STYLE.split()) in flat
    assert ",".join(DEFAULT_MODELS) in flat


def test_empty_models_is_usage_error(fake_render, script_file, tmp_path):
    result = run(script_file, tmp_path / "out", "--voices", "Kore", "--models", " , ")
    assert result.exit_code == 2
    assert "--models" in result.output
    assert fake_render.calls == []


def test_unexpected_crash_exits_4_with_traceback(
    fake_render, script_file, tmp_path, monkeypatch
):
    def boom(*args, **kwargs):
        raise ValueError("disk on fire")  # a ValueError that is NOT a refusal

    monkeypatch.setattr("pipeline.tts.audition.run_audition", boom)
    result = run(script_file, tmp_path / "out", "--voices", "Kore")
    assert result.exit_code == 4
    assert "Traceback" in result.output
    assert "disk on fire" in result.output
    assert "unexpected error" in result.output


def test_docstring_documents_exit_codes():
    doc = cli.commands["tts-audition"].help
    assert "4" in doc and "unexpected" in doc
