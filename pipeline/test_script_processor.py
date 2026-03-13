from __future__ import annotations

from xml.etree import ElementTree as ET

from pipeline.db import Episode, StateStore
from pipeline.feed import generate_feed_xml


def test_episode_show_notes_html_stored_and_retrieved(tmp_path) -> None:
    """Episodes with show_notes_html can be stored and retrieved."""
    store = StateStore(tmp_path / "test.sqlite3")

    episode = Episode(
        id="ep-html",
        title="Test Episode",
        slug="test-episode",
        pub_date="Thu, 13 Mar 2026 12:00:00 +0000",
        r2_key="episodes/deep-dives/test-episode.mp3",
        feed_slug="deep-dives",
        category="Technology",
        source_tag=None,
        preset_name="Script",
        source_url=None,
        size_bytes=5000,
        duration_seconds=120,
        summary="A test summary.",
        show_notes_html="<h2>Show Notes</h2><p>Details here.</p>",
    )
    store.insert_episode(episode)

    episodes = store.list_episodes(feed_slug="deep-dives")
    assert len(episodes) == 1
    assert episodes[0].show_notes_html == "<h2>Show Notes</h2><p>Details here.</p>"
    assert episodes[0].summary == "A test summary."

    store.close()


def test_feed_uses_show_notes_html_when_set(tmp_path, monkeypatch) -> None:
    """Episodes with show_notes_html use it for content:encoded instead of articles_json."""
    monkeypatch.setenv("PODCAST_BASE_URL", "https://podcast.test")
    store = StateStore(tmp_path / "test.sqlite3")

    episode = Episode(
        id="ep-rich",
        title="Deep Dive Episode",
        slug="deep-dive-episode",
        pub_date="Thu, 13 Mar 2026 12:00:00 +0000",
        r2_key="episodes/deep-dives/deep-dive-episode.mp3",
        feed_slug="deep-dives",
        category="Technology",
        source_tag=None,
        preset_name="Script",
        source_url=None,
        size_bytes=5000,
        duration_seconds=120,
        summary="A summary for description tag.",
        show_notes_html="<h2>Full Notes</h2><p>Rich content.</p>",
    )
    store.insert_episode(episode)

    xml_bytes = generate_feed_xml(store, feed_slug="deep-dives")
    root = ET.fromstring(xml_bytes)

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    item = root.find(".//item")

    desc = item.find("description")
    assert desc is not None
    assert desc.text == "A summary for description tag."

    encoded = item.find("content:encoded", ns)
    assert encoded is not None
    assert "<h2>Full Notes</h2>" in encoded.text
    assert "<p>Rich content.</p>" in encoded.text

    store.close()


from pipeline.script_processor import strip_markdown_for_tts


def test_strip_markdown_headings() -> None:
    text = "# Title\n\n## ACT 1: THE DREAM\n\nSome text.\n\n### Story One\n\nMore text."
    result = strip_markdown_for_tts(text)
    assert "# " not in result
    assert "## " not in result
    assert "### " not in result
    assert "Some text." in result
    assert "More text." in result


def test_strip_markdown_bold_italic() -> None:
    text = "This is **bold** and *italic* and ***both***."
    result = strip_markdown_for_tts(text)
    assert "**" not in result
    assert result.strip() == "This is bold and italic and both."


def test_strip_horizontal_rules() -> None:
    text = "Before.\n\n---\n\nAfter."
    result = strip_markdown_for_tts(text)
    assert "---" not in result
    assert "Before." in result
    assert "After." in result


def test_strip_end_marker() -> None:
    text = "Final paragraph.\n\n*[END OF SCRIPT]*"
    result = strip_markdown_for_tts(text)
    assert "[END OF SCRIPT]" not in result
    assert "Final paragraph." in result


def test_strip_preserves_content() -> None:
    text = "Two point two billion dollars. That's the average cost."
    result = strip_markdown_for_tts(text)
    assert result.strip() == text


from pipeline.script_processor import extract_summary, render_show_notes_html


def test_extract_summary_from_show_notes() -> None:
    md = (
        "# Show Notes\n\n"
        "## Editorial Notes\n\nStuff.\n\n"
        "## Episode Summary\n\n"
        "AI is discovering drug candidates faster than ever. "
        "This episode explores who's doing it.\n\n"
        "---\n\n"
        "## Key Numbers\n\nMore stuff."
    )
    summary = extract_summary(md)
    assert "AI is discovering drug candidates faster than ever" in summary
    assert "Key Numbers" not in summary
    assert "Editorial Notes" not in summary


def test_extract_summary_returns_none_when_missing() -> None:
    md = "# Show Notes\n\n## Key Numbers\n\nStuff."
    summary = extract_summary(md)
    assert summary is None


def test_render_show_notes_html() -> None:
    md = (
        "## Key Numbers\n\n"
        "- **$2.23 billion**: Average cost\n"
        "- **~12%**: Phase I approval rate\n\n"
        "## Companies\n\n"
        "### Nova In Silico\n"
        "- Website: [novainsilico.ai](https://www.novainsilico.ai)\n"
    )
    html = render_show_notes_html(md)
    assert "<h2>" in html
    assert "Key Numbers" in html
    assert "<strong>" in html
    assert 'href="https://www.novainsilico.ai"' in html


def test_render_show_notes_handles_tables() -> None:
    md = (
        "| Drug | What happened |\n"
        "|------|---------------|\n"
        "| Vioxx | Heart attacks |\n"
    )
    html = render_show_notes_html(md)
    assert "<table>" in html
    assert "Vioxx" in html


import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from pipeline.script_processor import publish_script


def test_publish_script_end_to_end(tmp_path, monkeypatch) -> None:
    """Full publish flow: read script, TTS, upload, insert episode, regen feed."""
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    script_file = tmp_path / "script.md"
    script_file.write_text(
        "## ACT 1\n\nHere is the script content.\n\n---\n\n*[END OF SCRIPT]*",
        encoding="utf-8",
    )

    show_notes_file = tmp_path / "notes.md"
    show_notes_file.write_text(
        "## Episode Summary\n\nA great episode about testing.\n\n---\n\n"
        "## Key Numbers\n\n- **42**: The answer.\n",
        encoding="utf-8",
    )

    # Mock ttsjoin and ffprobe
    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="180.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    # Mock feed regeneration
    feed_regen_called = []
    monkeypatch.setattr(
        "pipeline.script_processor.regenerate_and_upload_feed",
        lambda s, r: feed_regen_called.append(True),
    )

    result = publish_script(
        script_file=script_file,
        title="Test Episode",
        feed_slug="deep-dives",
        store=store,
        r2_client=r2_client,
        show_notes_file=show_notes_file,
        voice="nova",
        category="Technology",
        date_str="2026-03-13",
    )

    # Episode should be inserted
    episodes = store.list_episodes(feed_slug="deep-dives")
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.title == "Test Episode"
    assert ep.feed_slug == "deep-dives"
    assert ep.category == "Technology"
    assert ep.summary == "A great episode about testing."
    assert ep.show_notes_html is not None
    assert "<strong>" in ep.show_notes_html
    assert ep.duration_seconds == 180

    # MP3 should be uploaded
    r2_client.upload_file.assert_called_once()
    upload_key = r2_client.upload_file.call_args[0][1]
    assert upload_key.startswith("episodes/deep-dives/")
    assert "2026-03-13" in upload_key

    # Feed should be regenerated
    assert feed_regen_called

    # Result should contain key info
    assert result.r2_key == upload_key
    assert result.title == "Test Episode"

    store.close()


def test_publish_script_without_show_notes(tmp_path, monkeypatch) -> None:
    """Publish works without show notes file."""
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    script_file = tmp_path / "script.md"
    script_file.write_text("Just a plain script.", encoding="utf-8")

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="60.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        "pipeline.script_processor.regenerate_and_upload_feed",
        lambda s, r: None,
    )

    publish_script(
        script_file=script_file,
        title="No Notes Episode",
        feed_slug="general",
        store=store,
        r2_client=r2_client,
        voice="ash",
        category="News",
        date_str="2026-03-13",
    )

    episodes = store.list_episodes(feed_slug="general")
    assert len(episodes) == 1
    assert episodes[0].summary is None
    assert episodes[0].show_notes_html is None

    store.close()


def test_publish_script_tts_receives_stripped_text(tmp_path, monkeypatch) -> None:
    """Verify TTS input file contains stripped text, not raw markdown."""
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    script_file = tmp_path / "script.md"
    script_file.write_text(
        "## Heading\n\nThis is **bold** text.\n\n---\n\n*[END OF SCRIPT]*",
        encoding="utf-8",
    )

    tts_input_text = []

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            input_file = cmd[cmd.index("--input-file") + 1]
            tts_input_text.append(Path(input_file).read_text(encoding="utf-8"))
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="30.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        "pipeline.script_processor.regenerate_and_upload_feed",
        lambda s, r: None,
    )

    publish_script(
        script_file=script_file,
        title="Strip Test",
        feed_slug="test",
        store=store,
        r2_client=r2_client,
        date_str="2026-03-13",
    )

    assert len(tts_input_text) == 1
    assert "##" not in tts_input_text[0]
    assert "**" not in tts_input_text[0]
    assert "---" not in tts_input_text[0]
    assert "[END OF SCRIPT]" not in tts_input_text[0]
    assert "This is bold text." in tts_input_text[0]

    store.close()
