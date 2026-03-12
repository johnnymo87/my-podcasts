from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.fp_editor import (
    FPResearchPlan,
    FPStoryDirective,
    generate_fp_research_plan,
)


def test_returns_empty_if_no_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = generate_fp_research_plan(["Some headline"])
    assert result.themes == []
    assert result.directives == []


def test_returns_empty_if_no_headlines(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    result = generate_fp_research_plan([])
    assert result.themes == []
    assert result.directives == []


@patch("pipeline.fp_editor.genai.Client")
def test_success(mock_client_class, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    mock_response = MagicMock()
    mock_response.parsed = FPResearchPlan(
        themes=["Iran War", "Lebanon Escalation"],
        directives=[
            FPStoryDirective(
                headline="Iran strikes Israel",
                source="homepage/iran",
                priority=1,
                theme="Iran War",
                needs_exa=False,
                exa_query="",
                include_in_episode=True,
            )
        ],
    )
    mock_client.models.generate_content.return_value = mock_response

    result = generate_fp_research_plan(["Iran strikes Israel"])

    assert result.themes == ["Iran War", "Lebanon Escalation"]
    assert len(result.directives) == 1
    assert result.directives[0].headline == "Iran strikes Israel"
    assert result.directives[0].include_in_episode is True

    # Verify the client was called correctly
    mock_client.models.generate_content.assert_called_once()
    kwargs = mock_client.models.generate_content.call_args[1]
    assert kwargs["model"] == "gemini-3.1-flash-lite-preview"
    assert "Iran strikes Israel" in kwargs["contents"]


@patch("pipeline.fp_editor.genai.Client")
def test_handles_exception(mock_client_class, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.models.generate_content.side_effect = Exception("API error")

    result = generate_fp_research_plan(["Some headline"])
    assert result.themes == []
    assert result.directives == []


def test_fp_editor_uses_coverage_ledger_over_scripts(monkeypatch) -> None:
    """When coverage_ledger is provided, scripts are not included in prompt."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_plan = FPResearchPlan(
        themes=["Theme A"], directives=[], rotation_override=None
    )
    mock_response = MagicMock()
    mock_response.parsed = mock_plan

    with patch("pipeline.fp_editor.genai") as mock_genai:
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = mock_response

        generate_fp_research_plan(
            ["[homepage/iran] Test headline\nContext: ..."],
            context_scripts=["Old script text here"],
            coverage_ledger="## COVERAGE LEDGER\n| Theme | Days |",
        )

        prompt_used = mock_client.models.generate_content.call_args[1]["contents"]
        assert "COVERAGE LEDGER" in prompt_used
        assert "Old script text here" not in prompt_used


def test_fp_editor_falls_back_to_scripts(monkeypatch) -> None:
    """When no coverage_ledger, context_scripts are used."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_plan = FPResearchPlan(
        themes=["Theme A"], directives=[], rotation_override=None
    )
    mock_response = MagicMock()
    mock_response.parsed = mock_plan

    with patch("pipeline.fp_editor.genai") as mock_genai:
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = mock_response

        generate_fp_research_plan(
            ["[homepage/iran] Test headline\nContext: ..."],
            context_scripts=["Script text from yesterday"],
            coverage_ledger=None,
        )

        prompt_used = mock_client.models.generate_content.call_args[1]["contents"]
        assert "Script text from yesterday" in prompt_used
        assert "Previous episodes" in prompt_used


def test_fp_plan_has_rotation_override_field() -> None:
    """FPResearchPlan supports rotation_override field."""
    plan = FPResearchPlan(
        themes=["Theme A"], directives=[], rotation_override="Budget unmet"
    )
    assert plan.rotation_override == "Budget unmet"

    plan_none = FPResearchPlan(themes=["Theme A"], directives=[])
    assert plan_none.rotation_override is None
