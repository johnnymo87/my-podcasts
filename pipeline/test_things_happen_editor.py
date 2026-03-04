from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.things_happen_editor import (
    ResearchDirective,
    ResearchPlan,
    generate_research_plan,
)


def test_generate_research_plan_returns_empty_if_no_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert generate_research_plan(["Some headline"]) == []


def test_generate_research_plan_returns_empty_if_no_headlines(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    assert generate_research_plan([]) == []


@patch("pipeline.things_happen_editor.genai.Client")
def test_generate_research_plan_success(mock_client_class, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    mock_response = MagicMock()
    mock_response.parsed = ResearchPlan(
        directives=[
            ResearchDirective(
                headline="War in Middle East escalates",
                needs_exa=True,
                exa_query="Middle East war escalation",
                needs_xai=True,
                xai_query="Middle East war",
                is_foreign_policy=True,
                fp_query="Middle East war",
                is_ai=False,
                ai_query="",
            )
        ]
    )
    mock_client.models.generate_content.return_value = mock_response

    results = generate_research_plan(["War in Middle East escalates"])

    assert len(results) == 1
    assert results[0].headline == "War in Middle East escalates"
    assert results[0].is_foreign_policy is True

    # Verify the client was called correctly
    mock_client.models.generate_content.assert_called_once()
    kwargs = mock_client.models.generate_content.call_args[1]
    assert kwargs["model"] == "gemini-3.1-flash-lite-preview"
    assert "War in Middle East escalates" in kwargs["contents"]


@patch("pipeline.things_happen_editor.genai.Client")
def test_generate_research_plan_handles_exception(
    mock_client_class, monkeypatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.models.generate_content.side_effect = Exception("API error")

    results = generate_research_plan(["Headline"])
    assert results == []
