from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.rundown_writer import (
    WriterOutput,
    _strip_preamble,
    build_rundown_prompt,
    generate_rundown_script,
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
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_123"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "Generated podcast script here"

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
    )

    assert result.script == "Generated podcast script here"
    assert result.summary == ""
    mock_create.assert_called_once()
    mock_send.assert_called_once()
    mock_wait.assert_called_once_with("ses_123", timeout=120)
    mock_delete.assert_called_once_with("ses_123")


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_script_timeout_raises(mock_create, mock_send, mock_wait, mock_delete):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False

    try:
        generate_rundown_script(
            themes=["Tech"],
            articles_by_theme={"Tech": ["Article"]},
            date_str="2026-03-10",
        )
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "120 seconds" in str(e)

    mock_delete.assert_called_once_with("ses_timeout")


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_script_strips_preamble(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_456"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "Let me write the script.\n\n---\n\nHey, welcome to The Rundown."
    )

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
    )

    assert result.script == "Hey, welcome to The Rundown."
    assert result.summary == ""


def test_strip_preamble_removes_meta_text():
    raw = "Now I have enough context.\n\n---\n\nHey, welcome to The Rundown."
    assert _strip_preamble(raw) == "Hey, welcome to The Rundown."


def test_strip_preamble_preserves_clean_script():
    raw = "Hey, welcome to The Rundown for Monday."
    assert _strip_preamble(raw) == raw


def test_strip_preamble_ignores_late_separator():
    """--- appearing after line 10 is part of the script, not preamble."""
    lines = ["Line " + str(i) for i in range(12)]
    lines.append("---")
    lines.append("After separator")
    raw = "\n".join(lines)
    assert _strip_preamble(raw) == raw


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
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
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
    )
    assert isinstance(result, WriterOutput)
    assert result.summary == "Today's summary."
    assert result.script == "The script text."


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
