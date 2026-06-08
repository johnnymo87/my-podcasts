from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from pipeline import processor
from pipeline.chinatalk_report import maybe_rewrite_chinatalk
from pipeline.chinatalk_writer import ReportOutput


_TRANSCRIPT = "".join(f"Alice: line {i}\nBob: line {i}\n" for i in range(5))


def test_non_chinatalk_feed_is_passthrough():
    body, title = maybe_rewrite_chinatalk(
        body="essay body",
        title="2026-04-25 - Slow Boring - Foo",
        feed_slug="yglesias",
        subject_raw="Slow Boring: Foo",
    )
    assert body == "essay body"
    assert title == "2026-04-25 - Slow Boring - Foo"


def test_chinatalk_essay_is_passthrough():
    body, title = maybe_rewrite_chinatalk(
        body="An essay on policy. No speaker turns here.",
        title="2026-04-25 - ChinaTalk - Some Essay",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Some Essay",
    )
    assert body == "An essay on policy. No speaker turns here."
    assert title == "2026-04-25 - ChinaTalk - Some Essay"


@patch("pipeline.chinatalk_report.generate_report")
def test_chinatalk_transcript_is_rewritten(mock_writer):
    mock_writer.return_value = ReportOutput(
        script="Today on ChinaTalk, Jordan and a guest dug into...",
        summary="Brief.",
    )
    body, title = maybe_rewrite_chinatalk(
        body=_TRANSCRIPT,
        title="2026-04-25 - ChinaTalk - Episode 42",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Episode 42",
    )
    assert body.startswith("Today on ChinaTalk")
    assert title == "Report: 2026-04-25 - ChinaTalk - Episode 42"
    mock_writer.assert_called_once_with(
        body=_TRANSCRIPT,
        subject="ChinaTalk: Episode 42",
    )


@patch("pipeline.chinatalk_report.generate_report", side_effect=RuntimeError("boom"))
def test_chinatalk_writer_failure_propagates(mock_writer):
    # For a confirmed transcript, a generation failure must NOT silently fall
    # back to a literal read; it propagates so the queue redelivers the email.
    with pytest.raises(RuntimeError):
        maybe_rewrite_chinatalk(
            body=_TRANSCRIPT,
            title="2026-04-25 - ChinaTalk - Episode 42",
            feed_slug="chinatalk",
            subject_raw="ChinaTalk: Episode 42",
        )


def test_processor_calls_maybe_rewrite_chinatalk():
    """processor.process_email_bytes wires through maybe_rewrite_chinatalk."""
    source = inspect.getsource(processor.process_email_bytes)
    assert "maybe_rewrite_chinatalk" in source
    assert "preset.feed_slug" in source
