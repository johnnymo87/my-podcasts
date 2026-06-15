from __future__ import annotations

from unittest.mock import patch

from pipeline.substack_writer import (
    ReportOutput,
    _extract_script,
    _extract_summary,
    build_report_prompt,
    generate_report,
)


def test_build_prompt_includes_subject_and_body():
    prompt = build_report_prompt(
        body="Dwarkesh Patel\nWelcome.\nDavid Reich\nThanks.",
        subject="Why the Bronze Age was an inflection point",
    )
    assert "Why the Bronze Age was an inflection point" in prompt
    assert "Welcome." in prompt
    assert "Thanks." in prompt


def test_report_output_dataclass_fields():
    r = ReportOutput(script="text", summary="sum")
    assert r.script == "text"
    assert r.summary == "sum"


@patch("pipeline.substack_writer.delete_session")
@patch("pipeline.substack_writer.get_last_assistant_text")
@patch("pipeline.substack_writer.get_messages")
@patch("pipeline.substack_writer.wait_for_idle")
@patch("pipeline.substack_writer.send_prompt_async")
@patch("pipeline.substack_writer.create_session")
def test_generate_report_happy_path(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_sub"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<summary>Brief on the interview.</summary>\n\n"
        "<script>On this episode, Dwarkesh spoke with David Reich...</script>"
    )

    result = generate_report(body="Dwarkesh Patel\nhi", subject="X")

    assert result.script.startswith("On this episode")
    assert result.summary == "Brief on the interview."
    mock_create.assert_called_once()
    mock_send.assert_called_once()
    mock_wait.assert_called_once_with("ses_sub", timeout=900)
    mock_delete.assert_called_once_with("ses_sub")


@patch("pipeline.substack_writer.delete_session")
@patch("pipeline.substack_writer.wait_for_idle")
@patch("pipeline.substack_writer.send_prompt_async")
@patch("pipeline.substack_writer.create_session")
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


@patch("pipeline.substack_writer.delete_session")
@patch("pipeline.substack_writer.get_last_assistant_text")
@patch("pipeline.substack_writer.get_messages")
@patch("pipeline.substack_writer.wait_for_idle")
@patch("pipeline.substack_writer.send_prompt_async")
@patch("pipeline.substack_writer.create_session")
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
    assert _extract_script("Reasoning.\n<script>The briefing.</script>") == (
        "The briefing."
    )


def test_extract_script_no_tags_returns_full_text():
    assert _extract_script("No tags here.") == "No tags here."


def test_extract_summary_with_tags():
    assert _extract_summary("<summary>Brief.</summary>\nRest.") == "Brief."


def test_extract_summary_no_tags_returns_empty():
    assert _extract_summary("No summary.") == ""
