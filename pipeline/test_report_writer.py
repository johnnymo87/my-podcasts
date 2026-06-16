from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.report_writer import (
    ReportOutput,
    _extract_script,
    _extract_summary,
    build_report_prompt,
    generate_report,
)


def test_interview_style_prompt_mentions_interview():
    prompt = build_report_prompt(body="B", subject="S", style="interview")
    assert "interview" in prompt.lower()
    assert "S" in prompt and "B" in prompt


def test_paper_style_prompt_mentions_paper_framing():
    prompt = build_report_prompt(body="B", subject="S", style="paper")
    low = prompt.lower()
    assert "paper" in low or "research" in low
    assert "transcript" not in low


def test_byline_included_when_present():
    prompt = build_report_prompt(
        body="B", subject="S", style="paper", byline="Jane Doe, John Roe"
    )
    assert "Jane Doe, John Roe" in prompt


def test_byline_omitted_when_empty():
    prompt = build_report_prompt(body="B", subject="S", style="paper", byline="")
    assert "Authors" not in prompt


def test_unknown_style_raises():
    with pytest.raises(ValueError):
        build_report_prompt(body="B", subject="S", style="nope")


def test_extract_script_without_tags_returns_full_text():
    assert _extract_script("no tags here") == "no tags here"


def test_extract_summary_absent_returns_empty():
    assert _extract_summary("<script>x</script>") == ""


@patch("pipeline.report_writer.delete_session")
@patch("pipeline.report_writer.get_last_assistant_text")
@patch("pipeline.report_writer.get_messages")
@patch("pipeline.report_writer.wait_for_idle")
@patch("pipeline.report_writer.send_prompt_async")
@patch("pipeline.report_writer.create_session")
def test_generate_report_extracts_and_passes_paper_style(
    mock_create, mock_send, mock_wait, mock_msgs, mock_text, mock_del
):
    mock_create.return_value = "sess"
    mock_wait.return_value = True
    mock_msgs.return_value = []
    mock_text.return_value = (
        "<summary>Two sentence summary.</summary>"
        "<script>The full spoken script.</script>"
    )

    out = generate_report(
        body="body", subject="Subj", style="paper", byline="Jane Doe"
    )

    assert isinstance(out, ReportOutput)
    assert out.script == "The full spoken script."
    assert out.summary == "Two sentence summary."
    sent = mock_send.call_args.args[1]
    assert ("paper" in sent.lower()) or ("research" in sent.lower())
    assert "Jane Doe" in sent
    mock_del.assert_called_once()  # session cleaned up


@patch("pipeline.report_writer.delete_session")
@patch("pipeline.report_writer.get_last_assistant_text")
@patch("pipeline.report_writer.get_messages")
@patch("pipeline.report_writer.wait_for_idle")
@patch("pipeline.report_writer.send_prompt_async")
@patch("pipeline.report_writer.create_session")
def test_generate_report_rejects_empty_script(
    mock_create, mock_send, mock_wait, mock_msgs, mock_text, mock_del
):
    mock_create.return_value = "sess"
    mock_wait.return_value = True
    mock_msgs.return_value = []
    mock_text.return_value = "<summary>x</summary><script>   </script>"
    with pytest.raises(RuntimeError):
        generate_report(body="b", subject="s", style="interview")
    mock_del.assert_called_once()


@patch("pipeline.report_writer.delete_session")
@patch("pipeline.report_writer.wait_for_idle")
@patch("pipeline.report_writer.send_prompt_async")
@patch("pipeline.report_writer.create_session")
def test_generate_report_times_out_and_cleans_up(
    mock_create, mock_send, mock_wait, mock_del
):
    mock_create.return_value = "sess"
    mock_wait.return_value = False  # never idle
    with pytest.raises(RuntimeError):
        generate_report(body="b", subject="s")
    mock_del.assert_called_once_with("sess")
