from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.rundown_writer import build_rundown_prompt, generate_rundown_script


def test_build_prompt_basic():
    prompt = build_rundown_prompt(
        themes=["Tech", "Finance"],
        articles_by_theme={
            "Tech": ["Article about tech"],
            "Finance": ["Article about finance"],
        },
        date_str="2026-03-10",
    )
    assert "2026-03-10" in prompt
    assert "Tech" in prompt
    assert "Finance" in prompt
    assert "Article about tech" in prompt
    assert "Article about finance" in prompt
    assert "The Rundown" in prompt


def test_build_prompt_with_context():
    prompt = build_rundown_prompt(
        themes=["Tech"],
        articles_by_theme={"Tech": ["New article"]},
        date_str="2026-03-10",
        context_scripts=["Yesterday's script content"],
    )
    assert "PRIOR EPISODES" in prompt
    assert "Yesterday's script content" in prompt


def test_build_prompt_without_context():
    prompt = build_rundown_prompt(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
        context_scripts=None,
    )
    assert "PRIOR EPISODES" not in prompt


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_script(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_123"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "Generated podcast script here"

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
    )

    assert result == "Generated podcast script here"
    mock_create.assert_called_once()
    mock_send.assert_called_once()
    mock_wait.assert_called_once_with("ses_123", timeout=120)
    mock_delete.assert_called_once_with("ses_123")


@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_script_timeout_raises(mock_create, mock_send, mock_wait, mock_delete):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False

    try:
        generate_rundown_script(
            themes=["Tech"],
            articles_by_theme={"Tech": ["Article"]},
            date_str="2026-03-10",
        )
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "120 seconds" in str(e)

    mock_delete.assert_called_once_with("ses_timeout")
