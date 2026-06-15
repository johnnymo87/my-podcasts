from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from pipeline.__main__ import cli
from pipeline.substack import SubstackPost
from pipeline.substack_writer import ReportOutput


def _post() -> SubstackPost:
    return SubstackPost(
        title="David Reich – Bronze Age",
        subtitle="A subtitle",
        description="desc",
        canonical_url="https://www.dwarkesh.com/p/david-reich-2",
        body_html="<p><strong>Dwarkesh Patel</strong></p><p>Hi.</p>",
        slug="david-reich-2",
        host="www.dwarkesh.com",
        audience="everyone",
        wordcount=21163,
    )


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.substack_writer.generate_report")
@patch("pipeline.substack.resolve_post")
def test_report_mode_prefixes_title_and_passes_source_url(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "pipeline.__main__._default_state_db_path", lambda: tmp_path / "s.sqlite3"
    )
    monkeypatch.setattr("pipeline.__main__.R2Client", lambda: object())
    mock_resolve.return_value = _post()
    mock_report.return_value = ReportOutput(script="The briefing.", summary="Brief.")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "substack",
            "--url", "https://www.dwarkesh.com/p/david-reich-2",
            "--mode", "report",
            "--feed-slug", "dwarkesh",
        ],
    )

    assert result.exit_code == 0, result.output
    mock_report.assert_called_once()
    assert mock_publish.call_count == 1
    kwargs = mock_publish.call_args.kwargs
    assert kwargs["title"] == "Report: David Reich – Bronze Age"
    assert kwargs["feed_slug"] == "dwarkesh"
    assert kwargs["source_url"] == "https://www.dwarkesh.com/p/david-reich-2"


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.blog_poller.adapt_for_audio")
@patch("pipeline.substack.resolve_post")
def test_read_mode_uses_adapter_and_plain_title(
    mock_resolve, mock_adapt, mock_publish, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "pipeline.__main__._default_state_db_path", lambda: tmp_path / "s.sqlite3"
    )
    monkeypatch.setattr("pipeline.__main__.R2Client", lambda: object())
    mock_resolve.return_value = _post()
    mock_adapt.return_value = "Spoken adaptation of the essay."

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "substack",
            "--url", "https://www.dwarkesh.com/p/david-reich-2",
            "--mode", "read",
            "--feed-slug", "dwarkesh",
        ],
    )

    assert result.exit_code == 0, result.output
    mock_adapt.assert_called_once()
    kwargs = mock_publish.call_args.kwargs
    assert kwargs["title"] == "David Reich – Bronze Age"
    assert kwargs["source_url"] == "https://www.dwarkesh.com/p/david-reich-2"


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.substack_writer.generate_report")
@patch("pipeline.substack.resolve_post")
def test_script_file_skips_generation_and_publishes_reviewed_text(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "pipeline.__main__._default_state_db_path", lambda: tmp_path / "s.sqlite3"
    )
    monkeypatch.setattr("pipeline.__main__.R2Client", lambda: object())
    mock_resolve.return_value = _post()

    captured = {}

    def _capture(**kwargs):
        captured["title"] = kwargs["title"]
        captured["feed_slug"] = kwargs["feed_slug"]
        captured["source_url"] = kwargs["source_url"]
        captured["script"] = Path(kwargs["script_file"]).read_text(encoding="utf-8")
        return MagicMock()

    mock_publish.side_effect = _capture

    reviewed = tmp_path / "reviewed.txt"
    reviewed.write_text("The exact reviewed briefing.", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "substack",
            "--url", "https://www.dwarkesh.com/p/david-reich-2",
            "--mode", "report",
            "--feed-slug", "dwarkesh",
            "--script-file", str(reviewed),
        ],
    )

    assert result.exit_code == 0, result.output
    # Generation is skipped entirely when a script file is supplied.
    mock_report.assert_not_called()
    mock_resolve.assert_called_once()
    assert mock_publish.call_count == 1
    assert captured["title"] == "Report: David Reich – Bronze Age"
    assert captured["feed_slug"] == "dwarkesh"
    assert captured["source_url"] == "https://www.dwarkesh.com/p/david-reich-2"
    assert captured["script"] == "The exact reviewed briefing."


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.substack_writer.generate_report")
@patch("pipeline.substack.resolve_post")
def test_dry_run_does_not_publish_and_writes_script(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    mock_resolve.return_value = _post()
    mock_report.return_value = ReportOutput(script="The briefing.", summary="Brief.")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "substack",
            "--url", "https://www.dwarkesh.com/p/david-reich-2",
            "--feed-slug", "dwarkesh",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    mock_publish.assert_not_called()
    # The dry-run script path is echoed and the file exists with the script.
    printed = result.output.strip().splitlines()[-1]
    path = Path(printed.split(": ", 1)[1]) if ": " in printed else None
    assert path is not None and path.exists()
    assert "The briefing." in path.read_text(encoding="utf-8")
