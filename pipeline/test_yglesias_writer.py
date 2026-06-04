from __future__ import annotations

from unittest.mock import patch

from pipeline.yglesias_writer import (
    ReportOutput,
    _extract_script,
    _extract_summary,
    build_report_prompt,
    generate_report,
)


def test_build_prompt_includes_subject_and_body():
    prompt = build_report_prompt(
        body="Jerusalem Demsas: Hello\nKelsey Piper: Hi",
        subject="Can AI Cure Cancer?",
    )
    assert "Can AI Cure Cancer?" in prompt
    assert "Jerusalem Demsas: Hello" in prompt
    assert "Kelsey Piper: Hi" in prompt


def test_report_output_dataclass_fields():
    r = ReportOutput(script="text", summary="sum")
    assert r.script == "text"
    assert r.summary == "sum"


@patch("pipeline.yglesias_writer.delete_session")
@patch("pipeline.yglesias_writer.get_last_assistant_text")
@patch("pipeline.yglesias_writer.get_messages")
@patch("pipeline.yglesias_writer.wait_for_idle")
@patch("pipeline.yglesias_writer.send_prompt_async")
@patch("pipeline.yglesias_writer.create_session")
def test_generate_report_happy_path(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_yglesias"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<summary>Brief on the debate.</summary>\n\n"
        "<script>On The Argument this week, Jerusalem and Kelsey...</script>"
    )

    result = generate_report(body="Jerusalem Demsas: hi", subject="X")

    assert result.script.startswith("On The Argument this week")
    assert result.summary == "Brief on the debate."
    mock_create.assert_called_once()
    mock_send.assert_called_once()
    mock_wait.assert_called_once_with("ses_yglesias", timeout=900)
    mock_delete.assert_called_once_with("ses_yglesias")


@patch("pipeline.yglesias_writer.delete_session")
@patch("pipeline.yglesias_writer.wait_for_idle")
@patch("pipeline.yglesias_writer.send_prompt_async")
@patch("pipeline.yglesias_writer.create_session")
def test_generate_report_timeout_raises_and_deletes_session(
    mock_create, mock_send, mock_wait, mock_delete
):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False

    try:
        generate_report(body="x", subject="y")
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "900 seconds" in str(e)

    mock_delete.assert_called_once_with("ses_timeout")


@patch("pipeline.yglesias_writer.delete_session")
@patch("pipeline.yglesias_writer.get_last_assistant_text")
@patch("pipeline.yglesias_writer.get_messages")
@patch("pipeline.yglesias_writer.wait_for_idle")
@patch("pipeline.yglesias_writer.send_prompt_async")
@patch("pipeline.yglesias_writer.create_session")
def test_generate_report_rejects_empty_script(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_empty"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Anything.</summary>\n\n<script>   </script>"

    try:
        generate_report(body="x", subject="y")
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "empty script" in str(e)

    mock_delete.assert_called_once_with("ses_empty")


def test_extract_script_with_tags():
    raw = "Reasoning here.\n\n<script>The briefing text.</script>"
    assert _extract_script(raw) == "The briefing text."


def test_extract_script_no_tags_returns_full_text():
    raw = "The briefing without any tags."
    assert _extract_script(raw) == raw


def test_extract_summary_with_tags():
    raw = "<summary>Brief.</summary>\n\nThe rest."
    assert _extract_summary(raw) == "Brief."


def test_extract_summary_no_tags_returns_empty():
    raw = "No summary tags here."
    assert _extract_summary(raw) == ""


@patch("pipeline.yglesias_writer.delete_session")
@patch("pipeline.yglesias_writer.get_last_assistant_text")
@patch("pipeline.yglesias_writer.get_messages")
@patch("pipeline.yglesias_writer.wait_for_idle")
@patch("pipeline.yglesias_writer.send_prompt_async")
@patch("pipeline.yglesias_writer.create_session")
def test_generate_report_no_script_tags_uses_full_text(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_no_tags"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "The model just emitted prose without tags."

    result = generate_report(body="x", subject="y")

    assert result.script == "The model just emitted prose without tags."
    assert result.summary == ""
    mock_delete.assert_called_once_with("ses_no_tags")
