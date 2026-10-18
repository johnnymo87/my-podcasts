"""``run_episode_to_audio``: the child's whole job (script, then TTS render into
the work dir), against a fake script stage and the ``fake_tts_render`` fixture.
No spawn, no network."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pipeline import podcast_child as pc
from pipeline import podcast_report
from pipeline.podcast_feed import parse_podcast_feed
from pipeline.podcast_jobs import podcast_identity
from pipeline.podcast_sources import get_podcast_source
from pipeline.script_processor import build_tts_text
from pipeline.tts import resolve_render_config


FIXTURE = Path(__file__).parent / "fixtures" / "argument_feed.xml"
GUID = "audioboom-01a13c3d-ae49-7326-a550-f82c9eff9be1"
SCRIPT = "## Heading\n\n**Bold** words here.\n"


@pytest.fixture
def source():
    return get_podcast_source("argument")


@pytest.fixture
def item():
    return next(
        i
        for i in parse_podcast_feed(FIXTURE.read_text(encoding="utf-8"))
        if i.guid == GUID
    )


@pytest.fixture
def ident(source, item):
    return podcast_identity(source, item)


class FakeScript:
    """Stands in for ``run_episode_to_script``: records the work dir's state
    when it is called, writes ``script.txt``."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.episode_present_at_call: bool | None = None

    def __call__(self, source, item, work_dir, *, on_stage=None, **kw):
        self.episode_present_at_call = (work_dir / "episode.mp3").exists()
        for name in ("claim", "download", "transcribe", "write"):
            on_stage(name)
            if self.fail is not None and name == "transcribe":
                raise self.fail
        script = work_dir / "script.txt"
        script.write_text(SCRIPT, encoding="utf-8")
        return podcast_report.DryRunResult(
            work_dir=work_dir,
            transcript_path=work_dir / "transcript.txt",
            script_path=script,
            summary_path=None,
            script_words=3,
            piece_count=8,
            pieces_from_checkpoint=0,
            transcript_reused=False,
            input_tokens=1000,
            output_tokens=200,
            download_s=0.0,
            transcribe_s=0.0,
            write_s=0.0,
        )


def _audio(source, item, work, ident, **kw):
    stages: list[str] = []
    res = podcast_report.run_episode_to_audio(
        source,
        item,
        work,
        display_title=ident.display_title,
        feed_slug=source.feed_slug,
        on_stage=stages.append,
        **kw,
    )
    return res, stages


def test_renders_exactly_what_publish_script_would(
    tmp_path, monkeypatch, fake_tts_render, source, item, ident
):
    work = tmp_path / "w"
    work.mkdir()
    script = FakeScript()
    monkeypatch.setattr(podcast_report, "run_episode_to_script", script)

    res, stages = _audio(source, item, work, ident)

    [call] = fake_tts_render
    # Same text, config and ids publish_script would use...
    assert call["text"] == build_tts_text(
        SCRIPT, feed_slug="yglesias", title=ident.display_title
    )
    assert call["text"].startswith("Report: The Argument: ")  # the spoken title
    assert call["config"] == resolve_render_config("yglesias")
    assert call["feed_slug"] == "yglesias"
    assert call["episode_id"] == ident.slug
    # ...into a tmp file that is then moved into place.
    assert call["out_mp3"] == work / "episode.mp3.tmp"
    assert (work / "episode.mp3").exists()
    assert not (work / "episode.mp3.tmp").exists()

    audio = (work / "episode.mp3").read_bytes()
    assert res.audio_path == work / "episode.mp3"
    assert res.audio_bytes == len(audio)
    assert res.audio_sha256 == hashlib.sha256(audio).hexdigest()
    assert res.tts_text_sha256 == hashlib.sha256(call["text"].encode()).hexdigest()
    assert (res.script_path, res.piece_count) == (work / "script.txt", 8)
    assert (res.input_tokens, res.output_tokens) == (1000, 200)
    assert stages == ["claim", "download", "transcribe", "write", "render"]


def test_stale_episode_mp3_is_removed_before_anything_else(
    tmp_path, monkeypatch, fake_tts_render, source, item, ident
):
    work = tmp_path / "w"
    work.mkdir()
    (work / "episode.mp3").write_bytes(b"OLD AUDIO")
    (work / "episode.mp3.tmp").write_bytes(b"OLD PARTIAL")
    script = FakeScript()
    monkeypatch.setattr(podcast_report, "run_episode_to_script", script)

    _audio(source, item, work, ident)

    assert script.episode_present_at_call is False
    assert (work / "episode.mp3").read_bytes() != b"OLD AUDIO"


def test_failed_script_stage_leaves_no_old_audio(
    tmp_path, monkeypatch, fake_tts_render, source, item, ident
):
    work = tmp_path / "w"
    work.mkdir()
    (work / "episode.mp3").write_bytes(b"OLD AUDIO")
    monkeypatch.setattr(
        podcast_report, "run_episode_to_script", FakeScript(fail=RuntimeError("asr"))
    )
    with pytest.raises(RuntimeError, match="asr"):
        _audio(source, item, work, ident)
    assert not (work / "episode.mp3").exists()
    assert fake_tts_render == []


def test_failed_render_leaves_no_episode_mp3(
    tmp_path, monkeypatch, source, item, ident
):
    work = tmp_path / "w"
    work.mkdir()
    (work / "episode.mp3").write_bytes(b"OLD AUDIO")  # from a previous attempt
    monkeypatch.setattr(podcast_report, "run_episode_to_script", FakeScript())

    def broken_render(text, config, out_mp3, **kw):
        Path(out_mp3).write_bytes(b"half")  # a partial file at the tmp path
        raise RuntimeError("openai down")

    monkeypatch.setattr("pipeline.tts.render_episode", broken_render)

    with pytest.raises(RuntimeError, match="openai down"):
        _audio(source, item, work, ident)

    assert not (work / "episode.mp3").exists()
    assert not (work / "episode.mp3.tmp").exists()


def test_failed_render_through_the_child_body_is_ok_false_stage_render(
    tmp_path, monkeypatch, source, item, ident
):
    work = tmp_path / "w"
    work.mkdir()
    (work / "episode.mp3").write_bytes(b"OLD AUDIO")
    persisted = {**podcast_report.item_metadata(source, item)}
    persisted.update(display_title=ident.display_title, feed_slug=source.feed_slug)
    (work / pc.JOB_NAME).write_text(json.dumps(persisted), encoding="utf-8")
    monkeypatch.setattr(podcast_report, "run_episode_to_script", FakeScript())

    def broken_render(text, config, out_mp3, **kw):
        raise RuntimeError("openai down")

    monkeypatch.setattr("pipeline.tts.render_episode", broken_render)

    pc._child_body("argument", work, podcast_report.run_episode_to_audio)

    res = json.loads((work / pc.RESULT_NAME).read_text())
    assert (res["ok"], res["stage"], res["reason"]) == (False, "render", "RuntimeError")
    assert "openai down" in res["detail"]
    assert not (work / "episode.mp3").exists()


def test_child_body_ok_result_carries_the_audio_fields(
    tmp_path, monkeypatch, fake_tts_render, source, item, ident
):
    work = tmp_path / "w"
    work.mkdir()
    persisted = {**podcast_report.item_metadata(source, item)}
    persisted.update(display_title=ident.display_title, feed_slug=source.feed_slug)
    (work / pc.JOB_NAME).write_text(json.dumps(persisted), encoding="utf-8")
    monkeypatch.setattr(podcast_report, "run_episode_to_script", FakeScript())

    pc._child_body("argument", work, podcast_report.run_episode_to_audio)

    res = json.loads((work / pc.RESULT_NAME).read_text())
    audio = (work / "episode.mp3").read_bytes()
    assert res["ok"] is True
    assert res["audio_sha256"] == hashlib.sha256(audio).hexdigest()
    assert res["audio_bytes"] == len(audio)
    assert (
        res["tts_text_sha256"]
        == hashlib.sha256(fake_tts_render[0]["text"].encode()).hexdigest()
    )
    assert res["script_sha256"] == hashlib.sha256(SCRIPT.encode()).hexdigest()


def test_empty_render_output_is_refused(tmp_path, monkeypatch, source, item, ident):
    work = tmp_path / "w"
    work.mkdir()
    monkeypatch.setattr(podcast_report, "run_episode_to_script", FakeScript())
    monkeypatch.setattr(
        "pipeline.tts.render_episode",
        lambda text, config, out_mp3, **kw: Path(out_mp3).write_bytes(b""),
    )
    with pytest.raises(RuntimeError, match="empty"):
        _audio(source, item, work, ident)
    assert not (work / "episode.mp3").exists()


@pytest.mark.parametrize("missing", ["display_title", "feed_slug"])
def test_job_without_display_title_or_feed_slug_is_bad_job(
    tmp_path, source, item, ident, missing
):
    work = tmp_path / "w"
    work.mkdir()
    job = {**podcast_report.item_metadata(source, item)}
    job.update(display_title=ident.display_title, feed_slug=source.feed_slug)
    del job[missing]
    (work / pc.JOB_NAME).write_text(json.dumps(job), encoding="utf-8")

    pc._child_body("argument", work, lambda *a, **k: 1 / 0)

    res = json.loads((work / pc.RESULT_NAME).read_text())
    assert (res["stage"], res["reason"]) == ("job", "bad_job")
    assert missing in res["detail"]


def test_job_with_a_foreign_feed_slug_is_bad_job(tmp_path, source, item, ident):
    work = tmp_path / "w"
    work.mkdir()
    job = {**podcast_report.item_metadata(source, item)}
    job.update(display_title=ident.display_title, feed_slug="levine")
    (work / pc.JOB_NAME).write_text(json.dumps(job), encoding="utf-8")
    pc._child_body("argument", work, lambda *a, **k: 1 / 0)
    res = json.loads((work / pc.RESULT_NAME).read_text())
    assert (res["stage"], res["reason"]) == ("job", "bad_job")


def test_the_feed_renders_with_openai_because_the_child_is_a_daemon_process():
    """The child is a daemonic ``multiprocessing`` process, and a daemonic
    process may not start children. A Gemini primary renders in a spawned child
    of its own (``tts.gemini_phase``), so flipping ``yglesias`` to Gemini would
    make every podcast render fail. If you mean to do that, the render must move
    out of the daemon child first (and this test with it)."""
    assert resolve_render_config("yglesias").primary.provider == "openai"
