from __future__ import annotations

from pathlib import Path

from pipeline.things_happen_agent import (
    AGENT_PID_FILE,
    build_agent_prompt,
    is_agent_running,
    script_path_for_job,
)


def test_script_path_for_job():
    path = script_path_for_job("abc-123")
    assert path == Path("/tmp/things-happen-abc-123.txt")


def test_is_agent_running_false_when_no_pid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pipeline.things_happen_agent.AGENT_PID_FILE", tmp_path / "nonexistent.pid"
    )
    assert is_agent_running() is False


def test_is_agent_running_false_when_pid_dead(tmp_path, monkeypatch):
    pid_file = tmp_path / "test.pid"
    pid_file.write_text("999999")
    monkeypatch.setattr("pipeline.things_happen_agent.AGENT_PID_FILE", pid_file)
    assert is_agent_running() is False


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
