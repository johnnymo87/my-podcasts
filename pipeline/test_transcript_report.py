from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline import chinatalk_writer, yglesias_writer
from pipeline.report_engine import ReportOutput
from pipeline.transcript_report import (
    TRANSCRIPT_FEEDS,
    build_report_prompt,
    generate_report,
    maybe_rewrite_transcript,
)


_BODY = "Alice: hello\nBob: hi\n"
_SUBJECT = "Some Subject"


def test_chinatalk_prompt_is_byte_identical_to_the_retired_module():
    assert build_report_prompt(
        body=_BODY, subject=_SUBJECT, feed_slug="chinatalk"
    ) == chinatalk_writer.build_report_prompt(body=_BODY, subject=_SUBJECT)


def test_yglesias_prompt_is_byte_identical_to_the_retired_module():
    assert build_report_prompt(
        body=_BODY, subject=_SUBJECT, feed_slug="yglesias"
    ) == yglesias_writer.build_report_prompt(body=_BODY, subject=_SUBJECT)


# --- gate + writer, parametrized over the registered feeds ---


_TRANSCRIPT = "".join(f"Alice: line {i}\nBob: line {i}\n" for i in range(5))
_LONG_SCRIPT = "A spoken briefing sentence. " * 40  # > _MIN_SCRIPT_CHARS


def test_unregistered_feed_is_passthrough():
    body, title = maybe_rewrite_transcript(
        body=_TRANSCRIPT,
        title="2026-04-25 - Money Stuff - Foo",
        feed_slug="levine",
        subject_raw="Money Stuff: Foo",
    )
    assert body == _TRANSCRIPT
    assert title == "2026-04-25 - Money Stuff - Foo"


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
def test_essay_is_passthrough(slug):
    body, title = maybe_rewrite_transcript(
        body="An essay on policy. No speaker turns here.",
        title="T",
        feed_slug=slug,
        subject_raw="S",
    )
    assert body == "An essay on policy. No speaker turns here."
    assert title == "T"


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
@patch("pipeline.transcript_report.generate_report")
def test_transcript_is_rewritten(mock_writer, slug):
    mock_writer.return_value = ReportOutput(script=_LONG_SCRIPT, summary="Brief.")
    body, title = maybe_rewrite_transcript(
        body=_TRANSCRIPT, title="Ep 42", feed_slug=slug, subject_raw="Subject 42"
    )
    assert body == _LONG_SCRIPT
    assert title == "Report: Ep 42"
    mock_writer.assert_called_once_with(
        body=_TRANSCRIPT, subject="Subject 42", feed_slug=slug
    )


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
@patch("pipeline.transcript_report.generate_report", side_effect=RuntimeError("boom"))
def test_writer_failure_propagates(mock_writer, slug):
    """A confirmed transcript must never silently degrade to a literal read."""
    with pytest.raises(RuntimeError):
        maybe_rewrite_transcript(
            body=_TRANSCRIPT, title="Ep 42", feed_slug=slug, subject_raw="Subject 42"
        )


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
def test_prompt_contains_subject_and_body(slug):
    prompt = build_report_prompt(
        body="BODYMARKER", subject="SUBJMARKER", feed_slug=slug
    )
    assert "BODYMARKER" in prompt
    assert "SUBJMARKER" in prompt


def test_build_report_prompt_rejects_unknown_feed():
    with pytest.raises(ValueError, match="levine"):
        build_report_prompt(body="b", subject="s", feed_slug="levine")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_report_enforces_the_length_floor(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """A placeholder script must not replace a real transcript body."""
    mock_create.return_value = "ses_x"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<script>...</script>"

    with pytest.raises(RuntimeError, match="too short to be real"):
        generate_report(body="b", subject="s", feed_slug="chinatalk")

    mock_delete.assert_called_once_with("ses_x")
