from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_build_agent_prompt_contains_job_data():
    job = {
        "id": "job-abc",
        "date_str": "2026-03-02",
        "links_json": json.dumps(
            [
                {
                    "link_text": "Blue Owl",
                    "raw_url": "https://example.com/1",
                    "headline_context": "Blue Owl battles",
                }
            ]
        ),
    }
    prompt = build_agent_prompt(job)
    assert "job-abc" in prompt
    assert "2026-03-02" in prompt
    assert "Blue Owl" in prompt
    assert "search_related" in prompt
    assert "search_twitter" in prompt
    assert "Do NOT self-terminate" in prompt
    assert "operator confirmation" in prompt
