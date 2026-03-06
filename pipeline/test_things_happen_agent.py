from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.things_happen_agent import (
    build_agent_prompt,
    is_agent_running,
    launch_things_happen_agent,
    script_path_for_job,
    stop_agent,
)


def test_script_path_for_job():
    path = script_path_for_job("abc-123")
    assert path == Path("/tmp/things-happen-abc-123.txt")


@patch("pipeline.things_happen_agent.is_session_active")
def test_is_agent_running_true_when_session_active(mock_active: MagicMock) -> None:
    mock_active.return_value = True
    assert is_agent_running("sess-abc") is True
    mock_active.assert_called_once_with("sess-abc")


@patch("pipeline.things_happen_agent.is_session_active")
def test_is_agent_running_false_when_no_session_id(mock_active: MagicMock) -> None:
    assert is_agent_running(None) is False
    mock_active.assert_not_called()


@patch("pipeline.things_happen_agent.is_session_active")
def test_is_agent_running_false_when_session_gone(mock_active: MagicMock) -> None:
    mock_active.return_value = False
    assert is_agent_running("sess-gone") is False


@patch("pipeline.things_happen_agent.delete_session")
def test_stop_agent_deletes_session(mock_delete: MagicMock) -> None:
    stop_agent("sess-abc")
    mock_delete.assert_called_once_with("sess-abc")


@patch("pipeline.things_happen_agent.delete_session")
def test_stop_agent_noop_when_no_session(mock_delete: MagicMock) -> None:
    stop_agent(None)
    mock_delete.assert_not_called()


def test_build_agent_prompt_contains_job_data() -> None:
    job = {
        "id": "123-abc",
        "date_str": "2026-03-02",
        "links_json": '[{"link_text": "War", "raw_url": "http://bloomberg.com"}]',
    }
    work_dir = Path("/tmp/things-happen-123-abc")
    prompt = build_agent_prompt(job, work_dir)

    assert "123-abc" in prompt
    assert "2026-03-02" in prompt
    assert "http://bloomberg.com" in prompt
    assert "/tmp/things-happen-123-abc/articles/" in prompt


@patch("pipeline.things_happen_agent.send_prompt_async")
@patch("pipeline.things_happen_agent.create_session")
def test_launch_returns_session_id(
    mock_create: MagicMock, mock_prompt: MagicMock
) -> None:
    mock_create.return_value = "sess-new"
    job = {
        "id": "job-1",
        "date_str": "2026-03-05",
        "links_json": "[]",
    }
    result = launch_things_happen_agent(job, Path("/tmp/things-happen-job-1"))
    assert result == "sess-new"
    mock_create.assert_called_once()
    mock_prompt.assert_called_once()


@patch("pipeline.things_happen_agent.send_prompt_async")
@patch("pipeline.things_happen_agent.create_session")
def test_launch_returns_none_when_script_exists(
    mock_create: MagicMock, mock_prompt: MagicMock, tmp_path: Path
) -> None:
    script = tmp_path / "script.txt"
    script.write_text("already done")
    job = {"id": "job-1", "date_str": "2026-03-05", "links_json": "[]"}

    with patch("pipeline.things_happen_agent.script_path_for_job", return_value=script):
        result = launch_things_happen_agent(job, tmp_path)
    assert result is None
    mock_create.assert_not_called()
