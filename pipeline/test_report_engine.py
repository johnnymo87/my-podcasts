from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.report_engine import (
    ReportOutput,
    extract_script,
    extract_summary,
    run_report_prompt,
)


# --- extraction ---


def test_extract_script_with_tags():
    raw = "Reasoning here.\n\n<script>The briefing text.</script>"
    assert extract_script(raw) == "The briefing text."


def test_extract_script_picks_longest_block():
    """Regression for the 2026-08-18 incident (commit 39589e3)."""
    raw = "<script>...</script>\n\n<script>" + "The real script. " * 50 + "</script>"
    assert extract_script(raw).startswith("The real script.")
    assert len(extract_script(raw)) > 500


def test_extract_script_unclosed_final_block_is_recovered():
    """my-podcasts-ne0, composed with the longest-block case.

    NOTE: the original spec for this test asserted ``"scrip" not in
    extract_script(raw)``. That is unsatisfiable by construction -- the
    fixture body is "The real script. " repeated, and the English word
    "script" itself contains "scrip" as a substring, independent of any
    implementation. The real regression concern is the mangled closing-tag
    artifact (``</scrip>``) leaking into the narrated output, so that is what
    this asserts instead.
    """
    real = "The real script. " * 50
    raw = f"<script>...</script>\n\n<script>{real}</scrip>"
    assert extract_script(raw).startswith("The real script.")
    assert "</scrip>" not in extract_script(raw)


def test_extract_script_no_tags_falls_back_without_summary_prose():
    raw = "<summary>Brief.</summary>\n\nThe briefing without script tags."
    assert extract_script(raw) == "The briefing without script tags."


def test_extract_summary_with_tags():
    raw = "<summary>Brief.</summary>\n\nThe rest."
    assert extract_summary(raw) == "Brief."


def test_extract_summary_no_tags_returns_empty():
    assert extract_summary("No summary tags here.") == ""


# --- session mechanics ---


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_happy_path(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_1"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Brief.</summary>\n<script>Spoken words.</script>"

    result = run_report_prompt("INSTRUCTION", label="chinatalk")

    assert result == ReportOutput(script="Spoken words.", summary="Brief.")
    mock_send.assert_called_once_with("ses_1", "INSTRUCTION")
    mock_wait.assert_called_once_with("ses_1", timeout=900)
    mock_delete.assert_called_once_with("ses_1")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_timeout_raises_and_cleans_up(
    mock_create, mock_send, mock_wait, mock_delete
):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False

    with pytest.raises(RuntimeError, match="silver .*900 seconds"):
        run_report_prompt("INSTRUCTION", label="silver")

    mock_delete.assert_called_once_with("ses_timeout")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_empty_script_raises(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_empty"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Anything.</summary>\n\n<script>   </script>"

    with pytest.raises(RuntimeError, match="empty script"):
        run_report_prompt("INSTRUCTION", label="yglesias")

    mock_delete.assert_called_once_with("ses_empty")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_min_chars_rejects_short_script(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """min_chars is opt-in: emptiness is the wrong test on a publish path."""
    mock_create.return_value = "ses_short"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<script>Too short to be an episode.</script>"

    with pytest.raises(RuntimeError, match="too short to be real"):
        run_report_prompt("INSTRUCTION", label="silver", min_chars=500)

    mock_delete.assert_called_once_with("ses_short")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_default_has_no_length_floor(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """Default min_chars=0 preserves report_writer's existing behavior."""
    mock_create.return_value = "ses_default"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<script>Short but allowed.</script>"

    result = run_report_prompt("INSTRUCTION", label="paper")

    assert result.script == "Short but allowed."
