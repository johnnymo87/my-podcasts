from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from pipeline.__main__ import cli
from pipeline.document import Document
from pipeline.report_writer import ReportOutput


def _interview_doc() -> Document:
    return Document(
        title="David Reich – Bronze Age", byline="A subtitle",
        canonical_url="https://www.dwarkesh.com/p/david-reich-2",
        description="desc", report_text="Dwarkesh Patel: Hi.",
        read_html="<p>Hi.</p>", slug="david-reich-2", style="interview",
        wordcount=21163, default_category="Technology",
    )


def _paper_doc() -> Document:
    return Document(
        title="Capital as Artificial Intelligence",
        byline="Cesare Carissimo, Marcin Korecki",
        canonical_url="https://arxiv.org/abs/2407.16314v1",
        description="We gather many perspectives on Capital.",
        report_text="Abstract\n\nWe gather...", read_html=None,
        slug="2407-16314v1", style="paper", wordcount=9000,
        default_category="Science",
    )


def _patch_env(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pipeline.__main__._default_state_db_path", lambda: tmp_path / "s.sqlite3"
    )
    monkeypatch.setattr("pipeline.__main__.R2Client", lambda: object())


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_interview_report(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _interview_doc()
    mock_report.return_value = ReportOutput(script="Briefing.", summary="B.")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://www.dwarkesh.com/p/david-reich-2",
        "--mode", "report", "--feed-slug", "dwarkesh"])
    assert res.exit_code == 0, res.output
    assert mock_report.call_args.kwargs["style"] == "interview"
    assert mock_report.call_args.kwargs["byline"] == "A subtitle"
    kw = mock_publish.call_args.kwargs
    assert kw["title"] == "Report: David Reich – Bronze Age"
    assert kw["source_url"] == "https://www.dwarkesh.com/p/david-reich-2"
    assert kw["category"] == "Technology"  # from doc.default_category


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_paper_report_uses_paper_style_and_science_category(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _paper_doc()
    mock_report.return_value = ReportOutput(script="Paper briefing.", summary="B.")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://arxiv.org/html/2407.16314v1",
        "--feed-slug", "papers"])
    assert res.exit_code == 0, res.output
    assert mock_report.call_args.kwargs["style"] == "paper"
    kw = mock_publish.call_args.kwargs
    assert kw["title"] == "Report: Capital as Artificial Intelligence"
    assert kw["category"] == "Science"  # from doc.default_category


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_style_override(mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _paper_doc()
    mock_report.return_value = ReportOutput(script="x", summary="y")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://arxiv.org/html/2407.16314v1",
        "--feed-slug", "papers", "--style", "interview"])
    assert res.exit_code == 0, res.output
    assert mock_report.call_args.kwargs["style"] == "interview"


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_category_override_wins(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _paper_doc()
    mock_report.return_value = ReportOutput(script="x", summary="y")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://arxiv.org/html/2407.16314v1",
        "--feed-slug", "papers", "--category", "News"])
    assert res.exit_code == 0, res.output
    assert mock_publish.call_args.kwargs["category"] == "News"


@patch("pipeline.blog_poller.adapt_for_audio")
@patch("pipeline.script_processor.publish_script")
@patch("pipeline.sources.resolve_document")
def test_substack_read_mode_uses_adapter_and_plain_title(
    mock_resolve, mock_publish, mock_adapt, tmp_path, monkeypatch
):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _interview_doc()
    mock_adapt.return_value = "Spoken adaptation."
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://www.dwarkesh.com/p/david-reich-2",
        "--mode", "read", "--feed-slug", "dwarkesh"])
    assert res.exit_code == 0, res.output
    mock_adapt.assert_called_once()
    kw = mock_publish.call_args.kwargs
    assert kw["title"] == "David Reich – Bronze Age"
    assert kw["source_url"] == "https://www.dwarkesh.com/p/david-reich-2"


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.sources.resolve_document")
def test_arxiv_read_mode_unsupported_errors(
    mock_resolve, mock_publish, tmp_path, monkeypatch
):
    mock_resolve.return_value = _paper_doc()  # read_html is None
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://arxiv.org/html/2407.16314v1",
        "--mode", "read", "--feed-slug", "papers"])
    assert res.exit_code != 0
    assert "read" in res.output.lower()
    mock_publish.assert_not_called()


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_script_file_skips_generation(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _interview_doc()
    captured = {}

    def _cap(**kw):
        captured["title"] = kw["title"]
        captured["feed_slug"] = kw["feed_slug"]
        captured["source_url"] = kw["source_url"]
        captured["script"] = Path(kw["script_file"]).read_text("utf-8")
        return MagicMock()

    mock_publish.side_effect = _cap
    reviewed = tmp_path / "r.txt"
    reviewed.write_text("Exact reviewed text.", encoding="utf-8")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://www.dwarkesh.com/p/david-reich-2",
        "--feed-slug", "dwarkesh", "--script-file", str(reviewed)])
    assert res.exit_code == 0, res.output
    mock_report.assert_not_called()
    assert captured["script"] == "Exact reviewed text."
    assert captured["title"] == "Report: David Reich – Bronze Age"
    assert captured["feed_slug"] == "dwarkesh"
    assert captured["source_url"] == "https://www.dwarkesh.com/p/david-reich-2"


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_dry_run_writes_and_does_not_publish(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    mock_resolve.return_value = _interview_doc()
    mock_report.return_value = ReportOutput(script="The briefing.", summary="B.")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://www.dwarkesh.com/p/david-reich-2",
        "--feed-slug", "dwarkesh", "--dry-run"])
    assert res.exit_code == 0, res.output
    mock_publish.assert_not_called()
    printed = res.output.strip().splitlines()[-1]
    path = Path(printed.split(": ", 1)[1])
    assert path.exists() and "The briefing." in path.read_text("utf-8")


@patch("pipeline.script_processor.publish_script")
@patch("pipeline.report_writer.generate_report")
@patch("pipeline.sources.resolve_document")
def test_source_flag_forces_adapter(
    mock_resolve, mock_report, mock_publish, tmp_path, monkeypatch
):
    _patch_env(monkeypatch, tmp_path)
    mock_resolve.return_value = _interview_doc()
    mock_report.return_value = ReportOutput(script="x", summary="y")
    res = CliRunner().invoke(cli, [
        "episode", "--url", "https://example.com/post",
        "--feed-slug", "test", "--source", "substack"])
    assert res.exit_code == 0, res.output
    mock_resolve.assert_called_once_with("https://example.com/post", source="substack")
