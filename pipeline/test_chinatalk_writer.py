from __future__ import annotations

from unittest.mock import patch

from pipeline.chinatalk_writer import (
    ReportOutput,
    _extract_script,
    _extract_summary,
    build_report_prompt,
    generate_report,
)


def test_build_prompt_includes_subject_and_body():
    prompt = build_report_prompt(
        body="Speaker A: Hello\nSpeaker B: Hi",
        subject="ChinaTalk: Talking with Foo",
    )
    assert "ChinaTalk: Talking with Foo" in prompt
    assert "Speaker A: Hello" in prompt
    assert "Speaker B: Hi" in prompt


def test_report_output_dataclass_fields():
    r = ReportOutput(script="text", summary="sum")
    assert r.script == "text"
    assert r.summary == "sum"


@patch("pipeline.chinatalk_writer.delete_session")
@patch("pipeline.chinatalk_writer.get_last_assistant_text")
@patch("pipeline.chinatalk_writer.get_messages")
@patch("pipeline.chinatalk_writer.wait_for_idle")
@patch("pipeline.chinatalk_writer.send_prompt_async")
@patch("pipeline.chinatalk_writer.create_session")
def test_generate_report_happy_path(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_chinatalk"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<summary>Brief on the conversation.</summary>\n\n"
        "<script>Hey, in today's ChinaTalk Jordan sat down with...</script>"
    )

    result = generate_report(body="Speaker: hi", subject="ChinaTalk: X")

    assert result.script.startswith("Hey, in today's ChinaTalk")
    assert result.summary == "Brief on the conversation."
    mock_create.assert_called_once()
    mock_send.assert_called_once()
    mock_wait.assert_called_once_with("ses_chinatalk", timeout=300)
    mock_delete.assert_called_once_with("ses_chinatalk")


@patch("pipeline.chinatalk_writer.delete_session")
@patch("pipeline.chinatalk_writer.wait_for_idle")
@patch("pipeline.chinatalk_writer.send_prompt_async")
@patch("pipeline.chinatalk_writer.create_session")
def test_generate_report_timeout_raises_and_deletes_session(
    mock_create, mock_send, mock_wait, mock_delete
):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False

    try:
        generate_report(body="x", subject="y")
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "300 seconds" in str(e)

    mock_delete.assert_called_once_with("ses_timeout")


@patch("pipeline.chinatalk_writer.delete_session")
@patch("pipeline.chinatalk_writer.get_last_assistant_text")
@patch("pipeline.chinatalk_writer.get_messages")
@patch("pipeline.chinatalk_writer.wait_for_idle")
@patch("pipeline.chinatalk_writer.send_prompt_async")
@patch("pipeline.chinatalk_writer.create_session")
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
    """Without <script> tags, full response passes through.

    The empty-script guard in generate_report handles real emptiness.
    """
    raw = "The briefing without any tags."
    assert _extract_script(raw) == raw


def test_extract_summary_with_tags():
    raw = "<summary>Brief.</summary>\n\nThe rest."
    assert _extract_summary(raw) == "Brief."


def test_extract_summary_no_tags_returns_empty():
    raw = "No summary tags here."
    assert _extract_summary(raw) == ""


@patch("pipeline.chinatalk_writer.delete_session")
@patch("pipeline.chinatalk_writer.get_last_assistant_text")
@patch("pipeline.chinatalk_writer.get_messages")
@patch("pipeline.chinatalk_writer.wait_for_idle")
@patch("pipeline.chinatalk_writer.send_prompt_async")
@patch("pipeline.chinatalk_writer.create_session")
def test_generate_report_no_script_tags_uses_full_text(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """When the model omits <script> tags, full response becomes the script."""
    mock_create.return_value = "ses_no_tags"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "The model just emitted prose without tags."

    result = generate_report(body="x", subject="y")

    assert result.script == "The model just emitted prose without tags."
    assert result.summary == ""
    mock_delete.assert_called_once_with("ses_no_tags")
