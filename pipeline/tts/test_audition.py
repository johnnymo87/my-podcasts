"""Tests for ``pipeline.tts.audition``. Everything runs offline with a fake renderer."""

from __future__ import annotations

import hashlib
import itertools
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from pipeline.tts import audition
from pipeline.tts.audition import (
    AuditionRefused,
    Variant,
    baseline_leaf,
    build_variants,
    excerpt,
    run_audition,
    variant_filename,
)
from pipeline.tts.config import (
    FEED_VOICES,
    GeminiConfig,
    OpenAIConfig,
    RenderConfig,
)
from pipeline.tts.render import RenderResult, TTSRenderError


FLASH = "gemini-3.8-flash-tts"
LITE = "gemini-3.8-flash-lite-tts"
STYLE = "calm, measured news anchor"

# The real shape of one chunk record in a manifest's gemini_phase.
CHUNK = {
    "index": 0,
    "schema": 1,
    "attempts": [
        {
            "n": 1,
            "outcome": "verified",
            "synth": {
                "status": "ok",
                "audio_tokens": 5172,
                "prompt_tokens": 513,
                "elapsed_s": 32.3,
                "finish_reason": "STOP",
                "pcm_bytes": 7756800,
                "error": None,
                "kind": None,
            },
            "asr": {
                "status": "pass",
                "recall": 0.9929,
                "reasons": ["ok"],
                "input_tokens": 4077,
                "output_tokens": 515,
                "thinking_tokens": 1814,
                "elapsed_s": 9.8,
                "detail": "",
            },
        }
    ],
}
TOKENS = {
    "synth_prompt": 513,
    "synth_audio": 5172,
    "asr_input": 4077,
    "asr_output": 515,
    "asr_thinking": 1814,
}


def gemini_phase(chunks=None) -> dict:
    return {
        "outcome": "ok",
        "chunks": [CHUNK] if chunks is None else chunks,
        "tokens": TOKENS,
    }


class FakeRender:
    """Stands in for ``render_episode``. ``actions`` maps a voice to a behaviour:
    ``"render_error"``, ``"generic_error"``, ``"interrupt"``, ``"mismatch"``."""

    def __init__(self, actions: dict[str, str] | None = None, phase=None) -> None:
        self.actions = actions or {}
        self.calls: list[dict] = []
        self.phase = phase
        self._n = itertools.count()

    def __call__(
        self, text, config, out_mp3, *, feed_slug, episode_id, manifest_dir, **kw
    ):
        leaf = config.primary
        self.calls.append(
            {
                "text": text,
                "config": config,
                "out_mp3": out_mp3,
                "feed_slug": feed_slug,
                "episode_id": episode_id,
                "manifest_dir": manifest_dir,
                **kw,
            }
        )
        action = self.actions.get(leaf.voice)
        out_mp3.write_bytes(b"partial-audio")  # a real renderer writes early
        record = {
            "status": "failed" if action else "rendered",
            "total_audio_seconds": 0.0 if action else 12.5,
            "gemini_phase": self.phase if leaf.provider == "gemini" else None,
        }
        mpath = manifest_dir / feed_slug / f"{episode_id}-{next(self._n):03d}.json"
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps(record))
        if action == "render_error":
            raise TTSRenderError("Gemini phase failed: fatal")
        if action == "generic_error":
            raise RuntimeError("boom " + "x" * 600)
        if action == "interrupt":
            raise KeyboardInterrupt
        rendered = leaf
        if action == "mismatch":
            rendered = OpenAIConfig(model="tts-1-hd", voice="nova")
        return RenderResult(
            provider=rendered.provider,
            config=config,
            rendered=rendered,
            cached=False,
            chunks=3,
            manifest_path=mpath,
        )


def openai_variant(voice="nova") -> Variant:
    return Variant(OpenAIConfig(model="tts-1-hd", voice=voice))


def gemini_variant(voice="Kore", model=FLASH, style=STYLE) -> Variant:
    return Variant(GeminiConfig(model=model, voice=voice, style=style))


def audition_run(tmp_path, variants, fake, *, text="Hello there.", **kw):
    lines: list[str] = []
    summary = run_audition(
        text,
        variants,
        tmp_path / "out",
        feed_slug=kw.pop("feed_slug", "the-rundown"),
        style=kw.pop("style", STYLE),
        render=fake,
        echo=lines.append,
        **kw,
    )
    return summary, lines


# --- variants -------------------------------------------------------------


def test_build_variants_order_and_dedupe():
    variants = build_variants(
        "the-rundown",
        models=[FLASH, LITE, FLASH],
        voices=["Kore", "Puck", "Kore"],
        style=STYLE,
        include_openai=True,
    )
    assert [v.requested for v in variants] == [
        OpenAIConfig(model="tts-1-hd", voice="nova"),
        GeminiConfig(FLASH, "Kore", STYLE),
        GeminiConfig(FLASH, "Puck", STYLE),
        GeminiConfig(LITE, "Kore", STYLE),
        GeminiConfig(LITE, "Puck", STYLE),
    ]


def test_build_variants_no_openai_and_empty_style():
    variants = build_variants(
        "the-rundown", models=[FLASH], voices=["Kore"], style="", include_openai=False
    )
    assert [v.requested for v in variants] == [GeminiConfig(FLASH, "Kore", "")]


def test_baseline_openai_primary():
    assert baseline_leaf("fp-digest") == OpenAIConfig("tts-1-hd", "onyx")


def test_baseline_gemini_primary_uses_fallback(monkeypatch):
    monkeypatch.setitem(
        FEED_VOICES,
        "levine",
        RenderConfig(GeminiConfig(FLASH, "Kore"), OpenAIConfig("tts-1-hd", "sage")),
    )
    assert baseline_leaf("levine") == OpenAIConfig("tts-1-hd", "sage")


def test_baseline_gemini_primary_without_fallback_uses_default(monkeypatch):
    monkeypatch.setitem(
        FEED_VOICES, "levine", RenderConfig(GeminiConfig(FLASH, "Kore"))
    )
    assert baseline_leaf("levine") == OpenAIConfig("tts-1-hd", "nova")


def test_build_variants_unknown_feed_is_value_error():
    with pytest.raises(ValueError, match="nope"):
        build_variants(
            "nope", models=[FLASH], voices=["Kore"], style="", include_openai=True
        )


def test_openai_voice_in_voices_is_value_error():
    with pytest.raises(ValueError, match="OpenAI voice"):
        build_variants(
            "the-rundown",
            models=[FLASH],
            voices=["Kore", "nova"],
            style="",
            include_openai=True,
        )


def test_build_variants_nothing_to_render():
    with pytest.raises(ValueError, match="nothing to render"):
        build_variants(
            "the-rundown", models=[], voices=["Kore"], style="", include_openai=False
        )


# --- excerpt --------------------------------------------------------------


def test_excerpt_none_is_unchanged():
    assert excerpt("a\n\nb ", None) == "a\n\nb "


def test_excerpt_short_text_unchanged():
    assert excerpt("a\n\nb", 100) == "a\n\nb"


def test_excerpt_cuts_at_last_boundary_at_or_before_n():
    text = "aaaa\n\nbbbb\n\ncccc"
    assert excerpt(text, 10) == "aaaa\n\nbbbb"  # boundary at index 10: exactly N
    assert excerpt(text, 11) == "aaaa\n\nbbbb"
    assert excerpt(text, 9) == "aaaa"  # the later boundary is past N


def test_excerpt_no_boundary_raises():
    with pytest.raises(ValueError, match="paragraph"):
        excerpt("aaaa\n\nbbbb", 3)
    with pytest.raises(ValueError, match="paragraph"):
        excerpt("one long paragraph with no break", 10)


def test_excerpt_strips_trailing_whitespace():
    assert excerpt("aaaa  \n\n\n\nbbbb", 9) == "aaaa"


# --- filenames ------------------------------------------------------------


def test_variant_filename_uses_leaf_fields():
    assert (
        variant_filename("the-rundown", GeminiConfig(FLASH, "Kore"))
        == f"the-rundown--gemini--{FLASH}--Kore.mp3"
    )
    assert (
        variant_filename("the-rundown", OpenAIConfig("tts-1-hd", "nova"))
        == "the-rundown--openai--tts-1-hd--nova.mp3"
    )


def test_variant_filename_sanitizes():
    name = variant_filename("the-rundown", GeminiConfig("m/../x y", "Ko re\u00e9"))
    assert name == "the-rundown--gemini--m_.._x_y--Ko_re_.mp3"
    assert "/" not in name


# --- run_audition ---------------------------------------------------------


def test_render_call_contract(tmp_path):
    fake = FakeRender()
    summary, _ = audition_run(tmp_path, [openai_variant(), gemini_variant()], fake)
    out = tmp_path / "out"
    assert len(fake.calls) == 2
    for call in fake.calls:
        assert call["cache_dir"] is None
        assert call["notify_fallback"] is False
        assert call["config"].fallback is None
        assert call["manifest_dir"] == out / "manifests"
        assert call["episode_id"] == summary["episode_id"]
        assert call["episode_id"].startswith("audition-")
        assert call["feed_slug"] == "the-rundown"
        assert call["out_mp3"].parent == out
        assert call["out_mp3"].suffix == ".mp3"


def test_success_outputs_and_summary(tmp_path):
    fake = FakeRender(phase=gemini_phase())
    text = "Hello there."
    summary, lines = audition_run(
        tmp_path, [openai_variant(), gemini_variant()], fake, text=text
    )
    out = tmp_path / "out"
    assert (out / "script.txt").read_text() == text
    assert sorted(p.name for p in out.glob("*.mp3")) == [
        f"the-rundown--gemini--{FLASH}--Kore.mp3",
        "the-rundown--openai--tts-1-hd--nova.mp3",
    ]
    assert not list(out.glob(".partial-*"))
    on_disk = json.loads((out / "summary.json").read_text())
    assert on_disk == summary
    assert summary["schema"] == 1
    assert summary["feed"] == "the-rundown"
    assert summary["style"] == STYLE
    assert summary["script_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert summary["script_chars"] == len(text)
    assert summary["started_at"]

    oa, gm = summary["variants"]
    assert oa["status"] == "ok" and oa["error"] is None
    assert oa["file"] == "the-rundown--openai--tts-1-hd--nova.mp3"
    assert oa["label"] == "the-rundown--openai--tts-1-hd--nova"
    assert oa["requested"] == asdict(openai_variant().requested)
    assert oa["rendered"] == oa["requested"]
    assert oa["audio_seconds"] == 12.5
    assert oa["chunks"] == 3
    assert oa["verify"] is None and oa["tokens"] is None
    assert oa["wall_seconds"] >= 0
    assert (out / oa["manifest"]).is_file()
    assert not Path(oa["manifest"]).is_absolute()

    assert gm["requested"]["style"] == STYLE
    assert gm["verify"] == [
        {"index": 0, "attempts": 1, "verdict": "pass", "recall": 0.9929}
    ]
    assert gm["tokens"] == TOKENS
    assert len(lines) == 2
    assert lines[0].startswith("OK      the-rundown--openai--tts-1-hd--nova.mp3  00:12")
    assert "verify: pass" in lines[1]


def test_label_comes_from_rendered_and_mismatch_fails(tmp_path):
    fake = FakeRender(actions={"Kore": "mismatch"})
    summary, lines = audition_run(tmp_path, [gemini_variant("Kore")], fake)
    out = tmp_path / "out"
    (v,) = summary["variants"]
    assert v["status"] == "failed"
    assert "rendered_mismatch" in v["error"]
    assert v["file"] is None and v["label"] is None
    assert v["rendered"]["provider"] == "openai"
    assert not list(out.glob("*.mp3")) and not list(out.glob(".partial-*"))
    assert lines[0].startswith(f"FAILED  gemini/{FLASH}/Kore  ")


def test_render_error_and_generic_error_do_not_stop_the_run(tmp_path):
    fake = FakeRender(actions={"Kore": "render_error", "Puck": "generic_error"})
    variants = [gemini_variant("Kore"), gemini_variant("Puck"), openai_variant()]
    summary, lines = audition_run(tmp_path, variants, fake)
    out = tmp_path / "out"
    kore, puck, oa = summary["variants"]
    assert kore["status"] == "failed"
    assert kore["error"] == "Gemini phase failed: fatal"
    assert puck["status"] == "failed"
    assert puck["error"].startswith("RuntimeError: boom ")
    assert len(puck["error"]) <= 500
    assert oa["status"] == "ok"
    assert [p.name for p in out.glob("*.mp3")] == [oa["file"]]
    assert not list(out.glob(".partial-*"))
    assert [ln.split()[0] for ln in lines] == ["FAILED", "FAILED", "OK"]
    # A failed variant still points at its manifest (found by before/after diff).
    assert kore["manifest"] is not None and (out / kore["manifest"]).is_file()
    assert kore["audio_seconds"] is None and kore["chunks"] is None


def test_keyboard_interrupt_propagates_and_leaves_no_partial(tmp_path):
    fake = FakeRender(actions={"Puck": "interrupt"})
    variants = [gemini_variant("Kore"), gemini_variant("Puck"), openai_variant()]
    with pytest.raises(KeyboardInterrupt):
        audition_run(tmp_path, variants, fake)
    out = tmp_path / "out"
    assert len(fake.calls) == 2
    assert not list(out.glob(".partial-*"))
    # the summary written after the first variant survives the interrupt
    on_disk = json.loads((out / "summary.json").read_text())
    assert [v["status"] for v in on_disk["variants"]] == ["ok"]


def test_summary_is_rewritten_after_each_variant(tmp_path):
    out = tmp_path / "out"
    seen: list[int] = []

    class Spy(FakeRender):
        def __call__(self, *a, **kw):
            if self.calls:
                seen.append(
                    len(json.loads((out / "summary.json").read_text())["variants"])
                )
            return super().__call__(*a, **kw)

    audition_run(tmp_path, [gemini_variant("Kore"), gemini_variant("Puck")], Spy())
    assert seen == [1]


def test_colliding_target_names_are_refused(tmp_path):
    fake = FakeRender()
    with pytest.raises(AuditionRefused, match="same file"):
        audition_run(tmp_path, [gemini_variant("a b"), gemini_variant("a_b")], fake)
    assert fake.calls == []


# --- manifest reading -----------------------------------------------------


def test_verify_uses_outcome_when_asr_never_ran_and_last_attempt(tmp_path):
    chunks = [
        {
            "index": 0,
            "attempts": [
                {
                    "n": 1,
                    "outcome": "omission",
                    "asr": {"status": "omission", "recall": 0.4},
                },
                {
                    "n": 2,
                    "outcome": "verified",
                    "asr": {"status": "pass", "recall": 0.99},
                },
            ],
        },
        {"index": 1, "attempts": [{"n": 1, "outcome": "synth_failed", "synth": {}}]},
    ]
    summary, _ = audition_run(
        tmp_path, [gemini_variant()], FakeRender(phase=gemini_phase(chunks))
    )
    assert summary["variants"][0]["verify"] == [
        {"index": 0, "attempts": 2, "verdict": "pass", "recall": 0.99},
        {"index": 1, "attempts": 1, "verdict": "synth_failed", "recall": None},
    ]


@pytest.mark.parametrize(
    "phase",
    [
        {"chunks": "nope", "tokens": "x"},
        {"chunks": [42], "tokens": None},
        {"chunks": [{"index": 0, "attempts": "x"}], "tokens": None},
        {"chunks": [{"index": 0, "attempts": []}], "tokens": None},
        "not a dict",
    ],
)
def test_malformed_manifest_yields_null_fields(tmp_path, phase):
    summary, _ = audition_run(tmp_path, [gemini_variant()], FakeRender(phase=phase))
    v = summary["variants"][0]
    assert v["status"] == "ok"
    assert v["verify"] is None
    assert v["tokens"] is None


def test_unreadable_manifest_yields_null_fields(tmp_path):
    class Corrupt(FakeRender):
        def __call__(self, *a, **kw):
            result = super().__call__(*a, **kw)
            result.manifest_path.write_text("{not json")
            return result

    summary, _ = audition_run(tmp_path, [gemini_variant()], Corrupt())
    v = summary["variants"][0]
    assert v["status"] == "ok"
    assert v["verify"] is None and v["tokens"] is None and v["audio_seconds"] is None


# --- isolation ------------------------------------------------------------


def test_module_does_not_import_call_site_modules():
    code = (
        "import sys, pipeline.tts.audition;"
        "bad = [m for m in ('pipeline.r2', 'pipeline.db', 'pipeline.feed', "
        "'pipeline.alerts') if m in sys.modules];"
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_default_render_is_looked_up_at_call_time(tmp_path, monkeypatch):
    fake = FakeRender()
    monkeypatch.setattr("pipeline.tts.render.render_episode", fake)
    run_audition(
        "Hello there.",
        [openai_variant()],
        tmp_path / "out",
        feed_slug="the-rundown",
        style="",
        echo=lambda _l: None,
    )
    assert len(fake.calls) == 1
    assert audition.__name__ == "pipeline.tts.audition"


# --- review findings -------------------------------------------------------


def test_refusal_is_a_value_error_subclass():
    assert issubclass(AuditionRefused, ValueError)


def test_summary_complete_flag_and_planned(tmp_path):
    out = tmp_path / "out"
    seen: list[dict] = []

    class Spy(FakeRender):
        def __call__(self, *a, **kw):
            seen.append(json.loads((out / "summary.json").read_text()))
            return super().__call__(*a, **kw)

    variants = [openai_variant(), gemini_variant()]
    summary = audition_run(tmp_path, variants, Spy())[0]
    planned = [asdict(v.requested) for v in variants]
    assert seen[0]["complete"] is False and "finished_at" not in seen[0]
    assert seen[0]["planned"] == planned
    assert summary["complete"] is True
    assert summary["finished_at"]
    assert summary["planned"] == planned
    assert json.loads((out / "summary.json").read_text()) == summary


def test_interrupted_run_leaves_complete_false(tmp_path):
    fake = FakeRender(actions={"Puck": "interrupt"})
    with pytest.raises(KeyboardInterrupt):
        audition_run(tmp_path, [gemini_variant("Kore"), gemini_variant("Puck")], fake)
    on_disk = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert on_disk["complete"] is False
    assert len(on_disk["planned"]) == 2
    assert "finished_at" not in on_disk


def test_gemini_without_verify_prints_question_mark_openai_prints_na(tmp_path):
    fake = FakeRender(phase={"chunks": "garbage", "tokens": None})
    _, lines = audition_run(tmp_path, [openai_variant(), gemini_variant()], fake)
    assert lines[0].endswith("verify: n/a")
    assert lines[1].endswith("verify: ?")


def test_failed_line_and_entry_carry_phase_detail(tmp_path):
    detail = "child exited 1\n" + "x" * 400
    phase = {"chunks": [], "tokens": None, "detail": detail}
    fake = FakeRender(actions={"Kore": "render_error"}, phase=phase)
    summary, lines = audition_run(tmp_path, [gemini_variant("Kore")], fake)
    entry = summary["variants"][0]
    assert entry["detail"] == detail
    line = lines[0]
    assert line.startswith(f"FAILED  gemini/{FLASH}/Kore  Gemini phase failed: fatal")
    assert "child exited 1 xxx" in line  # whitespace collapsed
    tail = line.split("detail: ", 1)[1]
    assert len(tail.rstrip(")")) <= 200


def test_detail_is_null_without_one(tmp_path):
    summary, lines = audition_run(tmp_path, [openai_variant()], FakeRender())
    assert summary["variants"][0]["detail"] is None
    assert "detail" not in lines[0]


# --- fresh out-dir (no in-place reruns) --------------------------------------


@pytest.mark.parametrize("populated", [False, True])
def test_existing_out_dir_is_refused_and_left_untouched(tmp_path, populated):
    out = tmp_path / "out"
    out.mkdir()
    if populated:
        (out / "earlier.mp3").write_bytes(b"old")
        (out / "script.txt").write_text("old script")
        (out / "summary.json").write_text("{}")
    before = sorted(p.name for p in out.iterdir())
    fake = FakeRender()
    with pytest.raises(AuditionRefused, match="already exists.*fresh directory"):
        audition_run(tmp_path, [openai_variant()], fake)
    assert fake.calls == []
    assert sorted(p.name for p in out.iterdir()) == before
    if populated:
        assert (out / "earlier.mp3").read_bytes() == b"old"
        assert (out / "script.txt").read_text() == "old script"


def test_second_run_into_the_same_path_is_refused(tmp_path):
    first, _ = audition_run(tmp_path, [openai_variant()], FakeRender())
    out = tmp_path / "out"
    snapshot = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
    fake = FakeRender()
    with pytest.raises(AuditionRefused, match="already exists"):
        audition_run(tmp_path, [gemini_variant()], fake, text="a different script")
    assert fake.calls == []
    assert {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()} == snapshot
    assert json.loads((out / "summary.json").read_text()) == first


def test_parent_directories_are_created(tmp_path):
    out = tmp_path / "a" / "b" / "out"
    run_audition(
        "Hello there.",
        [openai_variant()],
        out,
        feed_slug="the-rundown",
        style="",
        render=FakeRender(),
        echo=lambda _l: None,
    )
    assert (out / "summary.json").is_file()


def test_refusals_that_need_no_directory_happen_before_the_mkdir(tmp_path):
    out = tmp_path / "never"
    with pytest.raises(AuditionRefused, match="same file"):
        run_audition(
            "x y.",
            [gemini_variant("a b"), gemini_variant("a_b")],
            out,
            feed_slug="the-rundown",
            style="",
            render=FakeRender(),
            echo=lambda _l: None,
        )
    assert not out.exists()
