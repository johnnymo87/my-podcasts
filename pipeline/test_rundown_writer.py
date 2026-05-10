from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.rundown_writer import (
    WriterOutput,
    _extract_script,
    build_rundown_prompt,
    generate_rundown_script,
    parse_covered,
    parse_summary,
)


def test_build_prompt_basic():
    prompt = build_rundown_prompt(
        themes=["Tech", "Finance"],
        articles_by_theme={
            "Tech": ["Article about tech"],
            "Finance": ["Article about finance"],
        },
        date_str="2026-03-10",
    )
    assert "2026-03-10" in prompt
    assert "Tech" in prompt
    assert "Finance" in prompt
    assert "Article about tech" in prompt
    assert "Article about finance" in prompt
    assert "The Rundown" in prompt


def test_build_prompt_with_context():
    prompt = build_rundown_prompt(
        themes=["Tech"],
        articles_by_theme={"Tech": ["New article"]},
        date_str="2026-03-10",
        context_scripts=["Yesterday's script content"],
    )
    assert "PRIOR EPISODES" in prompt
    assert "Yesterday's script content" in prompt


def test_build_prompt_without_context():
    prompt = build_rundown_prompt(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
        context_scripts=None,
    )
    assert "PRIOR EPISODES" not in prompt


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_script(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    mock_create.return_value = "ses_123"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "Generated podcast script here"

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
        work_dir=tmp_path,
    )

    assert result.script == "Generated podcast script here"
    assert result.summary == ""
    mock_create.assert_called_once()
    mock_send.assert_called_once()
    mock_wait.assert_called_once_with("ses_123", timeout=900)
    mock_delete.assert_called_once_with("ses_123")


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_script_timeout_raises(
    mock_create, mock_send, mock_wait, mock_delete, tmp_path
):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False

    try:
        generate_rundown_script(
            themes=["Tech"],
            articles_by_theme={"Tech": ["Article"]},
            date_str="2026-03-10",
            work_dir=tmp_path,
        )
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "900 seconds" in str(e)

    mock_delete.assert_called_once_with("ses_timeout")


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_script_extracts_script_tags(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    mock_create.return_value = "ses_456"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "Let me think.\n\n<script>Hey, welcome to The Rundown.</script>"
    )

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
        work_dir=tmp_path,
    )

    assert result.script == "Hey, welcome to The Rundown."
    assert result.summary == ""


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_rundown_script_rejects_empty_output(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    mock_create.return_value = "ses_empty"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Today.</summary>\n\n<script>   </script>"

    try:
        generate_rundown_script(
            themes=["Tech"],
            articles_by_theme={"Tech": ["Article"]},
            date_str="2026-03-10",
            work_dir=tmp_path,
        )
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "empty script" in str(e)


def test_extract_script_with_tags():
    """<script> tags extract the spoken script."""
    raw = (
        "Let me analyze what's new.\n\n"
        "<summary>Today covers markets and AI.</summary>\n\n"
        "<script>Hey, welcome to The Rundown for Thursday.</script>"
    )
    assert _extract_script(raw) == "Hey, welcome to The Rundown for Thursday."


def test_extract_script_tags_with_reasoning():
    """Reasoning before <script> tags is stripped."""
    raw = (
        "I see 8 stories. Let me figure out what's new vs repeated.\n"
        "Stories 1-3 are new, 4-8 were covered yesterday.\n\n"
        "<script>\nHey, welcome. Three stories today.\n\n"
        "First up, markets moved.\n</script>"
    )
    assert _extract_script(raw) == (
        "Hey, welcome. Three stories today.\n\nFirst up, markets moved."
    )


def test_extract_script_no_tags_returns_raw():
    """Without <script> tags, the full text is returned as-is."""
    raw = "Hey, welcome to The Rundown for Monday."
    assert _extract_script(raw) == raw


def test_parse_summary_extracts_tags():
    text = "<summary>A brief summary.</summary>\n\nHey, welcome to The Rundown."
    result = parse_summary(text)
    assert result.summary == "A brief summary."
    assert result.script == "Hey, welcome to The Rundown."


def test_parse_summary_no_tags():
    text = "Hey, welcome to The Rundown."
    result = parse_summary(text)
    assert result.summary == ""
    assert result.script == "Hey, welcome to The Rundown."


def test_parse_summary_multiline():
    text = "<summary>\nLine one.\nLine two.\n</summary>\n\nThe script."
    result = parse_summary(text)
    assert result.summary == "Line one.\nLine two."
    assert result.script == "The script."


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_rundown_returns_writer_output_with_summary(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    """generate_rundown_script returns WriterOutput with summary when tags present."""
    mock_create.return_value = "ses_wout"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Today's summary.</summary>\n\nThe script text."

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
        work_dir=tmp_path,
    )
    assert isinstance(result, WriterOutput)
    assert result.summary == "Today's summary."
    assert result.script == "The script text."


def test_parse_covered_extracts_headlines():
    """<covered> tags extract a list of headlines."""
    text = (
        "<covered>\n"
        "- Deutsche Bank Flags $30 Billion Exposure to Private Credit\n"
        "- Sunday Robotics Dishwashing Robot\n"
        "- ChatGPT Practiced Law Badly\n"
        "</covered>"
    )
    result = parse_covered(text)
    assert result == [
        "Deutsche Bank Flags $30 Billion Exposure to Private Credit",
        "Sunday Robotics Dishwashing Robot",
        "ChatGPT Practiced Law Badly",
    ]


def test_parse_covered_no_tags_returns_empty():
    """Without <covered> tags, returns empty list."""
    text = "Hey, welcome to The Rundown."
    result = parse_covered(text)
    assert result == []


def test_parse_covered_strips_whitespace():
    """Headlines are stripped of leading/trailing whitespace and dashes."""
    text = "<covered>\n  - Some Headline  \n  Another Headline\n- Third One\n</covered>"
    result = parse_covered(text)
    assert result == ["Some Headline", "Another Headline", "Third One"]


def test_parse_covered_skips_empty_lines():
    """Empty lines inside <covered> are ignored."""
    text = "<covered>\n- Headline One\n\n- Headline Two\n\n</covered>"
    result = parse_covered(text)
    assert result == ["Headline One", "Headline Two"]


def test_writer_output_has_covered_headlines():
    """WriterOutput includes covered_headlines field."""
    wo = WriterOutput(
        script="The script.",
        summary="A summary.",
        covered_headlines=["Story A", "Story B"],
    )
    assert wo.covered_headlines == ["Story A", "Story B"]


def test_writer_output_covered_defaults_empty():
    """WriterOutput.covered_headlines defaults to empty list."""
    wo = WriterOutput(script="The script.", summary="A summary.")
    assert wo.covered_headlines == []


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_script_parses_covered_tags(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    """generate_rundown_script populates covered_headlines from <covered> tags."""
    mock_create.return_value = "ses_cov"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<summary>Markets and AI today.</summary>\n\n"
        "<covered>\n"
        "- Deutsche Bank Exposure\n"
        "- ChatGPT Lawsuit\n"
        "</covered>\n\n"
        "<script>Hey, welcome to The Rundown.</script>"
    )

    result = generate_rundown_script(
        themes=["Finance"],
        articles_by_theme={"Finance": ["Article"]},
        date_str="2026-03-12",
        work_dir=tmp_path,
    )

    assert result.script == "Hey, welcome to The Rundown."
    assert result.summary == "Markets and AI today."
    assert result.covered_headlines == ["Deutsche Bank Exposure", "ChatGPT Lawsuit"]


def test_generate_script_prompt_asks_for_covered_tags():
    """The Rundown prompt instructs the writer to emit <covered> tags."""
    prompt = build_rundown_prompt(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-12",
    )
    # The instruction is prepended in generate_rundown_script, not in the prompt
    # itself. Check the instruction text instead.
    from pipeline.rundown_writer import generate_rundown_script
    import inspect

    source = inspect.getsource(generate_rundown_script)
    assert "<covered>" in source


def test_rundown_editor_uses_coverage_ledger_over_scripts(monkeypatch):
    """When coverage_ledger is provided, scripts are not included in prompt."""
    from unittest.mock import patch, MagicMock
    from pipeline.things_happen_editor import (
        generate_rundown_research_plan,
        RundownResearchPlan,
    )

    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    mock_plan = RundownResearchPlan(
        themes=["Theme A"], directives=[], rotation_override=None
    )
    mock_response = MagicMock()
    mock_response.parsed = mock_plan

    with patch("pipeline.things_happen_editor.genai") as mock_genai:
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = mock_response

        generate_rundown_research_plan(
            ["Test headline\nContext: ..."],
            context_scripts=["Old script text here"],
            coverage_ledger="## COVERAGE LEDGER\n| Theme | Days |",
        )

        prompt_used = mock_client.models.generate_content.call_args[1]["contents"]
        assert "COVERAGE LEDGER" in prompt_used
        assert "Old script text here" not in prompt_used


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_persists_raw_output_before_parsing(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    """The raw assistant text is written to raw_writer_output.txt the moment
    it's available, before any parsing happens."""
    mock_create.return_value = "ses_persist"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<summary>Today's summary.</summary>\n\n<script>Today's script.</script>"
    )

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
        work_dir=tmp_path,
    )

    raw_path = tmp_path / "raw_writer_output.txt"
    assert raw_path.exists()
    assert raw_path.read_text(encoding="utf-8") == (
        "<summary>Today's summary.</summary>\n\n<script>Today's script.</script>"
    )
    assert result.script == "Today's script."
    assert result.summary == "Today's summary."


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_reuses_persisted_output_when_present(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    """If raw_writer_output.txt already exists, the model is not called."""
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(
        "<summary>Cached summary.</summary>\n\n<script>Cached script.</script>",
        encoding="utf-8",
    )

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
        work_dir=tmp_path,
    )

    assert result.script == "Cached script."
    assert result.summary == "Cached summary."
    mock_create.assert_not_called()
    mock_send.assert_not_called()
    mock_wait.assert_not_called()
    mock_messages.assert_not_called()
    mock_delete.assert_not_called()


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_deletes_persisted_output_on_parse_failure(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    """If a persisted file parses to an empty script, the file is deleted
    so the next retry regenerates."""
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(
        "<summary>Empty body.</summary>\n\n<script>   </script>",
        encoding="utf-8",
    )

    try:
        generate_rundown_script(
            themes=["Tech"],
            articles_by_theme={"Tech": ["Article"]},
            date_str="2026-03-10",
            work_dir=tmp_path,
        )
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "empty script" in str(e)

    assert not raw_path.exists()
    mock_create.assert_not_called()


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_does_not_persist_when_wait_for_idle_times_out(
    mock_create, mock_send, mock_wait, mock_delete, tmp_path
):
    """If wait_for_idle returns False, no raw_writer_output.txt is written."""
    mock_create.return_value = "ses_timeout_persist"
    mock_wait.return_value = False

    try:
        generate_rundown_script(
            themes=["Tech"],
            articles_by_theme={"Tech": ["Article"]},
            date_str="2026-03-10",
            work_dir=tmp_path,
        )
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "900 seconds" in str(e)

    raw_path = tmp_path / "raw_writer_output.txt"
    assert not raw_path.exists()
    mock_delete.assert_called_once_with("ses_timeout_persist")


def test_rundown_editor_falls_back_to_scripts(monkeypatch):
    """When no coverage_ledger, context_scripts are used."""
    from unittest.mock import patch, MagicMock
    from pipeline.things_happen_editor import (
        generate_rundown_research_plan,
        RundownResearchPlan,
    )

    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    mock_plan = RundownResearchPlan(
        themes=["Theme A"], directives=[], rotation_override=None
    )
    mock_response = MagicMock()
    mock_response.parsed = mock_plan

    with patch("pipeline.things_happen_editor.genai") as mock_genai:
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = mock_response

        generate_rundown_research_plan(
            ["Test headline\nContext: ..."],
            context_scripts=["Script from yesterday"],
            coverage_ledger=None,
        )

        prompt_used = mock_client.models.generate_content.call_args[1]["contents"]
        assert "Script from yesterday" in prompt_used
        assert "Previous episodes" in prompt_used
