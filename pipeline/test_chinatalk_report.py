from __future__ import annotations

from unittest.mock import patch

from pipeline.chinatalk_report import maybe_rewrite_chinatalk
from pipeline.chinatalk_writer import ReportOutput


def test_non_chinatalk_feed_is_passthrough():
    body, title = maybe_rewrite_chinatalk(
        body="essay body",
        title="2026-04-25 - Slow Boring - Foo",
        feed_slug="yglesias",
        subject_raw="Slow Boring: Foo",
    )
    assert body == "essay body"
    assert title == "2026-04-25 - Slow Boring - Foo"


@patch("pipeline.chinatalk_report.is_transcript", return_value=False)
def test_chinatalk_essay_is_passthrough(mock_classify):
    body, title = maybe_rewrite_chinatalk(
        body="An essay on policy.",
        title="2026-04-25 - ChinaTalk - Some Essay",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Some Essay",
    )
    assert body == "An essay on policy."
    assert title == "2026-04-25 - ChinaTalk - Some Essay"
    mock_classify.assert_called_once()


@patch("pipeline.chinatalk_report.generate_report")
@patch("pipeline.chinatalk_report.is_transcript", return_value=True)
def test_chinatalk_transcript_is_rewritten(mock_classify, mock_writer):
    mock_writer.return_value = ReportOutput(
        script="Today on ChinaTalk, Jordan and a guest dug into...",
        summary="Brief.",
    )
    body, title = maybe_rewrite_chinatalk(
        body="Speaker A: Hi\nSpeaker B: Hello",
        title="2026-04-25 - ChinaTalk - Episode 42",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Episode 42",
    )
    assert body.startswith("Today on ChinaTalk")
    assert title == "Report: 2026-04-25 - ChinaTalk - Episode 42"
    mock_writer.assert_called_once_with(
        body="Speaker A: Hi\nSpeaker B: Hello",
        subject="ChinaTalk: Episode 42",
    )


@patch("pipeline.chinatalk_report.generate_report", side_effect=RuntimeError("boom"))
@patch("pipeline.chinatalk_report.is_transcript", return_value=True)
def test_writer_failure_falls_back_to_reading(mock_classify, mock_writer):
    body, title = maybe_rewrite_chinatalk(
        body="Speaker A: Hi",
        title="2026-04-25 - ChinaTalk - Episode 42",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Episode 42",
    )
    assert body == "Speaker A: Hi"
    assert title == "2026-04-25 - ChinaTalk - Episode 42"


@patch(
    "pipeline.chinatalk_report.is_transcript",
    side_effect=RuntimeError("classifier crashed"),
)
def test_classifier_failure_falls_back_to_reading(mock_classify):
    # The classifier itself is supposed to swallow exceptions, but the
    # orchestrator must also be defensive in case a future change leaks one.
    body, title = maybe_rewrite_chinatalk(
        body="Speaker A: Hi",
        title="2026-04-25 - ChinaTalk - Episode 42",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Episode 42",
    )
    assert body == "Speaker A: Hi"
    assert title == "2026-04-25 - ChinaTalk - Episode 42"


def test_processor_calls_maybe_rewrite_chinatalk():
    """processor.process_email_bytes wires through maybe_rewrite_chinatalk."""
    import inspect

    from pipeline import processor

    source = inspect.getsource(processor.process_email_bytes)
    assert "maybe_rewrite_chinatalk" in source
    assert "preset.feed_slug" in source
