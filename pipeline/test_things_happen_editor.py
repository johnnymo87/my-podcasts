from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.things_happen_editor import (
    RundownResearchPlan,
    RundownStoryDirective,
    generate_rundown_research_plan,
)


def test_generate_plan_returns_empty_if_no_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = generate_rundown_research_plan(["Some headline"])
    assert result.themes == []
    assert result.directives == []


def test_generate_plan_returns_empty_if_no_headlines(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    result = generate_rundown_research_plan([])
    assert result.themes == []
    assert result.directives == []


@patch("pipeline.things_happen_editor.genai.Client")
def test_generate_plan_success(mock_client_class, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    mock_response = MagicMock()
    mock_response.parsed = RundownResearchPlan(
        themes=["Tech", "Finance"],
        directives=[
            RundownStoryDirective(
                headline="Tech Company IPO",
                source="semafor",
                priority=1,
                theme="Tech",
                needs_exa=True,
                exa_query="tech company IPO 2026",
                is_foreign_policy=False,
                fp_query="",
                include_in_episode=True,
            ),
            RundownStoryDirective(
                headline="War Breaks Out",
                source="levine",
                priority=3,
                theme="Geopolitics",
                needs_exa=False,
                exa_query="",
                is_foreign_policy=True,
                fp_query="war breaks out",
                include_in_episode=False,
            ),
        ],
    )
    mock_client.models.generate_content.return_value = mock_response

    result = generate_rundown_research_plan(["Tech Company IPO", "War Breaks Out"])

    assert len(result.themes) == 2
    assert "Tech" in result.themes
    assert len(result.directives) == 2
    assert result.directives[0].include_in_episode is True
    assert result.directives[1].is_foreign_policy is True
    assert result.directives[1].include_in_episode is False

    mock_client.models.generate_content.assert_called_once()
    kwargs = mock_client.models.generate_content.call_args[1]
    assert kwargs["model"] == "gemini-3.1-flash-lite-preview"
    assert "Tech Company IPO" in kwargs["contents"]
    assert "War Breaks Out" in kwargs["contents"]


@patch("pipeline.things_happen_editor.genai.Client")
def test_generate_plan_with_context_scripts(mock_client_class, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    mock_response = MagicMock()
    mock_response.parsed = RundownResearchPlan(themes=[], directives=[])
    mock_client.models.generate_content.return_value = mock_response

    generate_rundown_research_plan(
        ["Headline"],
        context_scripts=["Prior episode script content"],
    )

    kwargs = mock_client.models.generate_content.call_args[1]
    assert "Previous episodes" in kwargs["contents"]
    assert "Prior episode script content" in kwargs["contents"]


@patch("pipeline.things_happen_editor.genai.Client")
def test_generate_plan_handles_exception(mock_client_class, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_client.models.generate_content.side_effect = Exception("API error")

    result = generate_rundown_research_plan(["Headline"])
    assert result.themes == []
    assert result.directives == []
