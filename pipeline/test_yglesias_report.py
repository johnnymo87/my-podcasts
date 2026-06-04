from __future__ import annotations

import inspect
from unittest.mock import patch

from pipeline import processor
from pipeline.yglesias_report import maybe_rewrite_yglesias
from pipeline.yglesias_writer import ReportOutput


def test_non_yglesias_feed_is_passthrough():
    body, title = maybe_rewrite_yglesias(
        body="essay body",
        title="2026-06-04 - ChinaTalk - Foo",
        feed_slug="chinatalk",
        subject_raw="ChinaTalk: Foo",
    )
    assert body == "essay body"
    assert title == "2026-06-04 - ChinaTalk - Foo"


@patch("pipeline.yglesias_report.is_argument_transcript", return_value=False)
def test_yglesias_essay_is_passthrough(mock_detect):
    body, title = maybe_rewrite_yglesias(
        body="An essay on housing.",
        title="2026-06-04 - Slow Boring - Some Essay",
        feed_slug="yglesias",
        subject_raw="Slow Boring: Some Essay",
    )
    assert body == "An essay on housing."
    assert title == "2026-06-04 - Slow Boring - Some Essay"
    mock_detect.assert_called_once()


@patch("pipeline.yglesias_report.generate_report")
@patch("pipeline.yglesias_report.is_argument_transcript", return_value=True)
def test_yglesias_transcript_is_rewritten(mock_detect, mock_writer):
    mock_writer.return_value = ReportOutput(
        script="On The Argument, Jerusalem and Kelsey debated...",
        summary="Brief.",
    )
    body, title = maybe_rewrite_yglesias(
        body="Jerusalem Demsas: Hi\nKelsey Piper: Hello",
        title="2026-06-04 - Slow Boring - Can AI Cure Cancer?",
        feed_slug="yglesias",
        subject_raw="Can AI Cure Cancer?",
    )
    assert body.startswith("On The Argument")
    assert title == "Report: 2026-06-04 - Slow Boring - Can AI Cure Cancer?"
    mock_writer.assert_called_once_with(
        body="Jerusalem Demsas: Hi\nKelsey Piper: Hello",
        subject="Can AI Cure Cancer?",
    )


@patch(
    "pipeline.yglesias_report.generate_report",
    side_effect=RuntimeError("boom"),
)
@patch("pipeline.yglesias_report.is_argument_transcript", return_value=True)
def test_writer_failure_falls_back_to_reading(mock_detect, mock_writer):
    body, title = maybe_rewrite_yglesias(
        body="Jerusalem Demsas: Hi",
        title="2026-06-04 - Slow Boring - Episode",
        feed_slug="yglesias",
        subject_raw="Episode",
    )
    assert body == "Jerusalem Demsas: Hi"
    assert title == "2026-06-04 - Slow Boring - Episode"


@patch(
    "pipeline.yglesias_report.is_argument_transcript",
    side_effect=RuntimeError("detector crashed"),
)
def test_detector_failure_falls_back_to_reading(mock_detect):
    body, title = maybe_rewrite_yglesias(
        body="Jerusalem Demsas: Hi",
        title="2026-06-04 - Slow Boring - Episode",
        feed_slug="yglesias",
        subject_raw="Episode",
    )
    assert body == "Jerusalem Demsas: Hi"
    assert title == "2026-06-04 - Slow Boring - Episode"


def test_processor_calls_maybe_rewrite_yglesias():
    source = inspect.getsource(processor.process_email_bytes)
    assert "maybe_rewrite_yglesias" in source
    assert "preset.feed_slug" in source
