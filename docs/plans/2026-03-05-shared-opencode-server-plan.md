# Shared opencode-serve Refactor — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate my-podcasts' ad-hoc `opencode serve` process on port 5555 and use the shared `opencode-serve.service` daemon on port 4096 instead, for both the Things Happen agent and the summarizer.

**Architecture:** A new shared `pipeline/opencode_client.py` wraps HTTP calls to `http://127.0.0.1:4096`. The Things Happen agent drops all subprocess/PID lifecycle code and creates sessions via the client. The summarizer replaces `opencode run` subprocess with `prompt_async` + SSE polling. Session IDs are tracked in SQLite instead of PID files.

**Tech Stack:** Python 3.14, requests, SSE via line-by-line parsing, SQLite, pytest

---

### Task 1: Create `pipeline/opencode_client.py` — core HTTP client

**Files:**
- Create: `pipeline/opencode_client.py`
- Create: `pipeline/test_opencode_client.py`

This task builds the shared HTTP client that both the agent and summarizer will use. All methods talk to the shared opencode serve at `OPENCODE_URL` (default `http://127.0.0.1:4096`). The `x-opencode-directory` header scopes requests to the my-podcasts project.

**Step 1: Write failing tests for the client**

Create `pipeline/test_opencode_client.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _mock_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    if json_data is not None:
        resp.json.return_value = json_data
    resp.raise_for_status.side_effect = (
        None if resp.ok else Exception(f"HTTP {status_code}")
    )
    return resp


class TestCreateSession:
    @patch("pipeline.opencode_client.requests.post")
    def test_returns_session_id(self, mock_post: MagicMock) -> None:
        from pipeline.opencode_client import create_session

        mock_post.return_value = _mock_response(200, {"id": "sess-abc"})
        result = create_session("/home/dev/projects/my-podcasts")
        assert result == "sess-abc"
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["headers"]["x-opencode-directory"] == "/home/dev/projects/my-podcasts"

    @patch("pipeline.opencode_client.requests.post")
    def test_raises_on_failure(self, mock_post: MagicMock) -> None:
        from pipeline.opencode_client import create_session

        mock_post.return_value = _mock_response(500)
        with pytest.raises(Exception):
            create_session("/home/dev/projects/my-podcasts")


class TestSendPromptAsync:
    @patch("pipeline.opencode_client.requests.post")
    def test_sends_prompt(self, mock_post: MagicMock) -> None:
        from pipeline.opencode_client import send_prompt_async

        mock_post.return_value = _mock_response(204)
        send_prompt_async("sess-abc", "Hello agent")
        call_kwargs = mock_post.call_args
        body = call_kwargs[1]["json"]
        assert body == {"parts": [{"type": "text", "text": "Hello agent"}]}


class TestIsSessionActive:
    @patch("pipeline.opencode_client.requests.get")
    def test_returns_true_for_200(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import is_session_active

        mock_get.return_value = _mock_response(200, {"id": "sess-abc"})
        assert is_session_active("sess-abc") is True

    @patch("pipeline.opencode_client.requests.get")
    def test_returns_false_for_404(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import is_session_active

        mock_get.return_value = _mock_response(404)
        assert is_session_active("sess-abc") is False

    @patch("pipeline.opencode_client.requests.get")
    def test_returns_false_on_connection_error(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import is_session_active

        mock_get.side_effect = Exception("Connection refused")
        assert is_session_active("sess-abc") is False


class TestDeleteSession:
    @patch("pipeline.opencode_client.requests.delete")
    def test_deletes_session(self, mock_delete: MagicMock) -> None:
        from pipeline.opencode_client import delete_session

        mock_delete.return_value = _mock_response(200)
        delete_session("sess-abc")
        mock_delete.assert_called_once()

    @patch("pipeline.opencode_client.requests.delete")
    def test_ignores_404(self, mock_delete: MagicMock) -> None:
        from pipeline.opencode_client import delete_session

        mock_delete.return_value = _mock_response(404)
        delete_session("sess-abc")  # Should not raise


class TestGetMessages:
    @patch("pipeline.opencode_client.requests.get")
    def test_returns_messages(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import get_messages

        mock_get.return_value = _mock_response(200, [
            {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "parts": [{"type": "text", "text": "hello"}]},
        ])
        msgs = get_messages("sess-abc")
        assert len(msgs) == 2
        assert msgs[1]["role"] == "assistant"


class TestGetLastAssistantText:
    def test_extracts_text_from_messages(self) -> None:
        from pipeline.opencode_client import get_last_assistant_text

        messages = [
            {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "parts": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ]},
        ]
        assert get_last_assistant_text(messages) == "Hello world"

    def test_returns_empty_when_no_assistant(self) -> None:
        from pipeline.opencode_client import get_last_assistant_text

        messages = [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}]
        assert get_last_assistant_text(messages) == ""


class TestWaitForIdle:
    @patch("pipeline.opencode_client.requests.get")
    def test_returns_true_on_idle_event(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import wait_for_idle

        # Simulate SSE stream with session.status idle event
        sse_lines = [
            b'data: {"type": "session.status", "properties": {"sessionID": "sess-abc", "status": {"type": "idle"}}}\n',
            b"\n",
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.iter_lines.return_value = iter(sse_lines)
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = mock_resp

        assert wait_for_idle("sess-abc", timeout=5) is True

    @patch("pipeline.opencode_client.requests.get")
    def test_ignores_other_session_idle(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import wait_for_idle

        # Idle event for a different session, then our session
        sse_lines = [
            b'data: {"type": "session.status", "properties": {"sessionID": "sess-OTHER", "status": {"type": "idle"}}}\n',
            b"\n",
            b'data: {"type": "session.status", "properties": {"sessionID": "sess-abc", "status": {"type": "idle"}}}\n',
            b"\n",
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.iter_lines.return_value = iter(sse_lines)
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = mock_resp

        assert wait_for_idle("sess-abc", timeout=5) is True
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_opencode_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.opencode_client'`

**Step 3: Write the implementation**

Create `pipeline/opencode_client.py`:

```python
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests


OPENCODE_URL = os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096")
PROJECT_DIR = str(Path(__file__).resolve().parent.parent)


def _base_url() -> str:
    return OPENCODE_URL.rstrip("/")


def _headers(directory: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if directory:
        headers["x-opencode-directory"] = directory
    return headers


def create_session(directory: str | None = None) -> str:
    """Create a new opencode session. Returns the session ID."""
    dir_value = directory or PROJECT_DIR
    resp = requests.post(
        f"{_base_url()}/session",
        headers=_headers(dir_value),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def send_prompt_async(session_id: str, text: str) -> None:
    """Send a prompt to a session (fire-and-forget)."""
    resp = requests.post(
        f"{_base_url()}/session/{session_id}/prompt_async",
        json={"parts": [{"type": "text", "text": text}]},
        timeout=10,
    )
    resp.raise_for_status()


def is_session_active(session_id: str) -> bool:
    """Check if a session exists and is accessible."""
    try:
        resp = requests.get(
            f"{_base_url()}/session/{session_id}",
            timeout=5,
        )
        return resp.ok
    except Exception:
        return False


def delete_session(session_id: str) -> None:
    """Delete a session. Ignores 404 (already gone)."""
    resp = requests.delete(
        f"{_base_url()}/session/{session_id}",
        timeout=10,
    )
    if resp.status_code != 404:
        resp.raise_for_status()


def get_messages(session_id: str) -> list[dict]:
    """Get all messages for a session."""
    resp = requests.get(
        f"{_base_url()}/session/{session_id}/message",
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_last_assistant_text(messages: list[dict]) -> str:
    """Extract concatenated text from the last assistant message."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            parts = msg.get("parts", [])
            texts = [p["text"] for p in parts if p.get("type") == "text"]
            return "".join(texts)
    return ""


def wait_for_idle(session_id: str, timeout: int = 120) -> bool:
    """Subscribe to SSE events and wait for the session to become idle.

    Returns True if idle was detected, False on timeout.
    Falls back to polling if SSE connection fails.
    """
    deadline = time.time() + timeout

    try:
        with requests.get(
            f"{_base_url()}/event",
            stream=True,
            timeout=(5, timeout + 5),
        ) as resp:
            if not resp.ok:
                return _poll_until_idle(session_id, deadline)

            for line in resp.iter_lines():
                if time.time() > deadline:
                    return False

                if not line:
                    continue

                decoded = line.decode("utf-8") if isinstance(line, bytes) else line
                if not decoded.startswith("data: "):
                    continue

                try:
                    event = json.loads(decoded[6:])
                except (json.JSONDecodeError, ValueError):
                    continue

                if (
                    event.get("type") == "session.status"
                    and event.get("properties", {}).get("sessionID") == session_id
                    and event.get("properties", {}).get("status", {}).get("type") == "idle"
                ):
                    return True

    except requests.RequestException:
        return _poll_until_idle(session_id, deadline)

    return False


def _poll_until_idle(session_id: str, deadline: float) -> bool:
    """Fallback: poll session status until idle or deadline."""
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{_base_url()}/session/status",
                timeout=5,
            )
            if resp.ok:
                statuses = resp.json()
                session_status = statuses.get(session_id, {})
                if session_status.get("type") == "idle" or session_id not in statuses:
                    return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_opencode_client.py -v`
Expected: All PASS

**Step 5: Run linter and type checker**

Run: `uv run ruff check pipeline/opencode_client.py pipeline/test_opencode_client.py && uv run ruff format --check pipeline/opencode_client.py pipeline/test_opencode_client.py`
Fix any issues before proceeding.

**Step 6: Commit**

```bash
git add pipeline/opencode_client.py pipeline/test_opencode_client.py
git commit --no-gpg-sign -m "feat: add opencode_client module for shared opencode-serve API"
```

---

### Task 2: Add `opencode_session_id` to DB schema

**Files:**
- Modify: `pipeline/db.py` (schema, migration, new accessors)
- Modify: `pipeline/test_things_happen_processor.py` (verify migration works with existing test DB setup)

**Step 1: Write failing tests for the new DB methods**

Add to the bottom of `pipeline/test_things_happen_processor.py` (which already sets up the DB with pending jobs):

```python
def test_things_happen_session_id_column_and_accessors(tmp_path) -> None:
    """Verify the opencode_session_id column is added and accessors work."""
    from pipeline.db import StateStore

    store = StateStore(tmp_path / "test.sqlite3")

    # Insert a job
    job_id = store.insert_pending_things_happen(
        email_r2_key="inbox/raw/test.eml",
        date_str="2026-03-05",
        links_json="[]",
    )

    # Initially null
    assert store.get_things_happen_session_id(job_id) is None

    # Set it
    store.set_things_happen_session_id(job_id, "sess-abc-123")
    assert store.get_things_happen_session_id(job_id) == "sess-abc-123"

    # Clear it
    store.clear_things_happen_session_id(job_id)
    assert store.get_things_happen_session_id(job_id) is None

    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_processor.py::test_things_happen_session_id_column_and_accessors -v`
Expected: FAIL — `AttributeError: 'StateStore' object has no attribute 'get_things_happen_session_id'`

**Step 3: Add migration and accessors to `pipeline/db.py`**

Add to `_migrate_schema` method, after the existing episodes migrations:

```python
# pending_things_happen migrations
th_cols = {
    row["name"]
    for row in self._conn.execute(
        "PRAGMA table_info(pending_things_happen)"
    ).fetchall()
}
if "opencode_session_id" not in th_cols:
    self._conn.execute(
        "ALTER TABLE pending_things_happen ADD COLUMN opencode_session_id TEXT"
    )
```

Add these new methods to the `StateStore` class:

```python
def set_things_happen_session_id(self, job_id: str, session_id: str) -> None:
    self._conn.execute(
        "UPDATE pending_things_happen SET opencode_session_id = ? WHERE id = ?",
        (session_id, job_id),
    )
    self._conn.commit()

def get_things_happen_session_id(self, job_id: str) -> str | None:
    row = self._conn.execute(
        "SELECT opencode_session_id FROM pending_things_happen WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return row["opencode_session_id"]

def clear_things_happen_session_id(self, job_id: str) -> None:
    self._conn.execute(
        "UPDATE pending_things_happen SET opencode_session_id = NULL WHERE id = ?",
        (job_id,),
    )
    self._conn.commit()
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest pipeline/test_things_happen_processor.py::test_things_happen_session_id_column_and_accessors -v`
Expected: PASS

**Step 5: Run full test suite to confirm no regressions**

Run: `uv run pytest -v`
Expected: All existing tests still pass.

**Step 6: Commit**

```bash
git add pipeline/db.py pipeline/test_things_happen_processor.py
git commit --no-gpg-sign -m "feat: add opencode_session_id column to pending_things_happen"
```

---

### Task 3: Refactor `things_happen_agent.py` to use shared server

**Files:**
- Modify: `pipeline/things_happen_agent.py`
- Modify: `pipeline/test_things_happen_agent.py`

This is the core refactor. We remove all process lifecycle code and replace it with calls to `opencode_client`.

**Step 1: Rewrite tests to match the new interface**

Replace `pipeline/test_things_happen_agent.py` entirely:

```python
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

    # Monkeypatch script_path_for_job to return our tmp file
    with patch(
        "pipeline.things_happen_agent.script_path_for_job", return_value=script
    ):
        result = launch_things_happen_agent(job, tmp_path)
    assert result is None
    mock_create.assert_not_called()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_things_happen_agent.py -v`
Expected: FAIL — old imports/signatures don't match

**Step 3: Rewrite `pipeline/things_happen_agent.py`**

```python
from __future__ import annotations

from pathlib import Path

from pipeline.opencode_client import (
    create_session,
    delete_session,
    is_session_active,
    send_prompt_async,
)


def script_path_for_job(job_id: str) -> Path:
    """Return the path to the script file for a given job ID."""
    return Path(f"/tmp/things-happen-{job_id}.txt")


def is_agent_running(session_id: str | None) -> bool:
    """Check if the opencode session for the agent is still active."""
    if session_id is None:
        return False
    return is_session_active(session_id)


def stop_agent(session_id: str | None) -> None:
    """Delete the opencode session for the agent."""
    if session_id is None:
        return
    delete_session(session_id)


def build_agent_prompt(job: dict, work_dir: Path) -> str:
    """Build the initial prompt for the Things Happen agent."""
    links_json = job["links_json"]

    prompt = f"""You are an AI assistant helping produce a daily podcast briefing for the "Things Happen" segment from Matt Levine's Money Stuff newsletter.

## Job Details
- Job ID: {job["id"]}
- Date: {job["date_str"]}

## Original Links
These were extracted from the newsletter:
{links_json}

## Your Task

All research for this episode has already been completed and collected for you on disk. You do NOT need to search the web or fetch articles yourself.

Your working directory is: `{work_dir}`

1. **Read the Articles:** Use your tools to read the markdown files in `{work_dir}/articles/`. These contain the full text (or headlines if paywalled) of the original newsletter links.
2. **Read the Enrichment Data:** Read the files in `{work_dir}/enrichment/`. This directory contains Exa web search results, xAI Twitter summaries, and RSS alternative perspectives (for foreign policy stories). Use this context to enrich the original articles.
3. **Check the Trailing Window:** Read the prior episode scripts in `{work_dir}/context/`. If a story is adequately covered in these prior scripts and has no new developments, **skip it**. Do not repeat background context we already explained to the listener yesterday.
4. **Draft a Plan:** Once you have read the files, write a short summary of what you found and your proposed script plan. Present this plan via Telegram and wait for the operator to reply.
5. **Write the Script:** Once approved by the operator, write your final podcast script to `{work_dir}/script.txt`. Make it conversational, engaging, and suitable for TTS.

Remember: Do not use the `bash` tool to run python scripts to fetch articles or search APIs. Just read the files that have already been gathered for you.
"""
    return prompt


def launch_things_happen_agent(job: dict, work_dir: Path) -> str | None:
    """Launch a Things Happen agent session on the shared opencode server.

    Returns the session ID on success, or None if the script already exists.
    """
    # Check if script already written
    job_id = job["id"]
    script_path = script_path_for_job(job_id)
    if script_path.exists():
        return None

    prompt = build_agent_prompt(job, work_dir)
    session_id = create_session()
    send_prompt_async(session_id, prompt)
    return session_id
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_things_happen_agent.py -v`
Expected: All PASS

**Step 5: Lint**

Run: `uv run ruff check pipeline/things_happen_agent.py pipeline/test_things_happen_agent.py && uv run ruff format --check pipeline/things_happen_agent.py pipeline/test_things_happen_agent.py`

**Step 6: Commit**

```bash
git add pipeline/things_happen_agent.py pipeline/test_things_happen_agent.py
git commit --no-gpg-sign -m "refactor: replace subprocess opencode serve with shared server in agent"
```

---

### Task 4: Refactor `consumer.py` for session-ID-based tracking

**Files:**
- Modify: `pipeline/consumer.py`

The consumer currently imports `is_agent_running` (no args, PID-based) and `stop_agent` (no args, SIGTERM). Both now take a `session_id` argument. The consumer needs to read/write session IDs from the DB instead of relying on PID files.

**Step 1: Rewrite `consumer.py`**

Key changes to `consume_forever`:
- Import the new `set_things_happen_session_id`, `get_things_happen_session_id`, `clear_things_happen_session_id` from db
- In the Things Happen job loop, read `session_id` from `store.get_things_happen_session_id(job["id"])`
- Pass `session_id` to `is_agent_running(session_id)` and `stop_agent(session_id)`
- After `launch_things_happen_agent` returns a session_id, store it via `store.set_things_happen_session_id(job["id"], session_id)`
- On completion/cleanup, call `store.clear_things_happen_session_id(job["id"])`

Replace the Things Happen section of `consume_forever` (roughly lines 169-236):

```python
        # Process any due Things Happen jobs.
        try:
            due_jobs = store.list_due_things_happen()
            for job in due_jobs:
                try:
                    work_dir = Path(f"/tmp/things-happen-{job['id']}")
                    script_file = work_dir / "script.txt"
                    session_id = store.get_things_happen_session_id(job["id"])

                    if script_file.exists():
                        # Agent finished — run TTS + publish with the script.
                        try:
                            dry_run = os.environ.get(
                                "THINGS_HAPPEN_DRY_RUN", ""
                            ).strip()
                            if dry_run:
                                print(
                                    f"DRY RUN: skipping TTS for "
                                    f"{job['id']} ({job['date_str']}). "
                                    f"Script at: {script_file}"
                                )
                                store.mark_things_happen_completed(job["id"])
                            else:
                                print(
                                    f"Processing Things Happen job with "
                                    f"agent script: "
                                    f"{job['id']} ({job['date_str']})"
                                )
                                process_things_happen_job(
                                    job,
                                    store,
                                    r2_client,
                                    script_path=script_file,
                                )
                                print(f"Completed Things Happen job: {job['id']}")

                            # Copy to persistent storage for future context
                            persist_dir = Path(
                                "/persist/my-podcasts/scripts/things-happen"
                            )
                            persist_dir.mkdir(parents=True, exist_ok=True)
                            persist_path = persist_dir / f"{job['date_str']}.txt"
                            shutil.copy(script_file, persist_path)

                        finally:
                            _cleanup_old_work_dirs()
                            stop_agent(session_id)
                            store.clear_things_happen_session_id(job["id"])
                    elif not is_agent_running(session_id):
                        # No script, no agent — run collection then launch.
                        # Clear stale session ID if present.
                        if session_id:
                            store.clear_things_happen_session_id(job["id"])

                        print(
                            f"Running collection phase for job: "
                            f"{job['id']} ({job['date_str']})"
                        )
                        links_raw = json.loads(job["links_json"])
                        collect_all_artifacts(job["id"], links_raw, work_dir)

                        print(
                            f"Launching Things Happen agent for job: "
                            f"{job['id']} ({job['date_str']})"
                        )
                        new_session_id = launch_things_happen_agent(job, work_dir)
                        if new_session_id:
                            store.set_things_happen_session_id(
                                job["id"], new_session_id
                            )
                    # else: agent is running, wait for it to finish.
                except Exception as exc:
                    print(f"Failed Things Happen job {job['id']}: {exc}")
        except Exception as exc:
            print(f"Error checking Things Happen jobs: {exc}")
```

Also update the imports at the top of `consumer.py`:

```python
from pipeline.things_happen_agent import (
    is_agent_running,
    launch_things_happen_agent,
    stop_agent,
)
```

(Same import names, but the functions now have different signatures.)

**Step 2: Run full test suite**

Run: `uv run pytest -v`
Expected: All pass. The consumer tests (in `test_things_happen_processor.py`) don't directly test `consume_forever`, so existing tests should still work.

**Step 3: Lint**

Run: `uv run ruff check pipeline/consumer.py && uv run ruff format --check pipeline/consumer.py`

**Step 4: Commit**

```bash
git add pipeline/consumer.py
git commit --no-gpg-sign -m "refactor: consumer uses session-ID tracking instead of PID files"
```

---

### Task 5: Refactor `summarizer.py` to use shared server

**Files:**
- Modify: `pipeline/summarizer.py`
- Modify: `pipeline/test_summarizer.py`

**Step 1: Rewrite the summarizer test**

Replace `test_generate_briefing_calls_opencode` in `pipeline/test_summarizer.py`:

```python
def test_generate_briefing_calls_opencode(monkeypatch) -> None:
    """Verify that generate_briefing_script uses the opencode client."""
    monkeypatch.setattr(
        "pipeline.summarizer.create_session",
        lambda directory=None: "sess-sum",
    )
    monkeypatch.setattr(
        "pipeline.summarizer.send_prompt_async",
        lambda session_id, text: None,
    )
    monkeypatch.setattr(
        "pipeline.summarizer.wait_for_idle",
        lambda session_id, timeout=120: True,
    )
    monkeypatch.setattr(
        "pipeline.summarizer.get_messages",
        lambda session_id: [
            {"role": "user", "parts": [{"type": "text", "text": "prompt"}]},
            {"role": "assistant", "parts": [
                {"type": "text", "text": "Here is your briefing script about today's news."},
            ]},
        ],
    )
    monkeypatch.setattr(
        "pipeline.summarizer.delete_session",
        lambda session_id: None,
    )

    articles = [
        FetchedArticle(
            url="https://example.com/1", content="Test content.", source_tier="archive"
        ),
    ]
    result = generate_briefing_script(articles, date_str="2026-02-26")
    assert "briefing script" in result
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_summarizer.py::test_generate_briefing_calls_opencode -v`
Expected: FAIL — old implementation uses subprocess, new mocks don't match

**Step 3: Rewrite `pipeline/summarizer.py`**

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.opencode_client import (
    create_session,
    delete_session,
    get_last_assistant_text,
    get_messages,
    send_prompt_async,
    wait_for_idle,
)


if TYPE_CHECKING:
    from pipeline.article_fetcher import FetchedArticle


PROMPT_TEMPLATE = """\
You are generating a podcast briefing script for a listener who reads Matt Levine's Money Stuff newsletter. Today's date is {date_str}. Below are the stories from the "Things Happen" section at the end of the newsletter. Your job is to brief the listener on each story: what happened, why it matters, and which stories are the biggest deals today.

IMPORTANT RULES:
- Be conversational and concise. This will be read aloud by a TTS engine.
- For each story, clearly state your source quality BEFORE summarizing:
  - If marked "PUBLICLY AVAILABLE PORTION": you have the article content. Summarize the key points.
  - If marked "HEADLINE ONLY": you only have the headline. Give brief context based on your knowledge, and be upfront that you're working from the headline alone.
- Start with a brief intro: "Here are today's Things Happen stories from Money Stuff."
- End with a brief sign-off.
- Flag the 2-3 biggest stories early on.
- Do NOT use any markdown formatting, bullet points, or special characters. Plain spoken English only.
- Do NOT use the word "delve" or any of its variants.

---

STORIES:

{stories_block}
"""


def build_prompt(articles: list[FetchedArticle], date_str: str) -> str:
    """Build the LLM prompt from fetched articles."""
    stories: list[str] = []
    for i, article in enumerate(articles, 1):
        tier_label = article.source_label.upper()
        stories.append(
            f"Story {i} [{tier_label}]:\n"
            f"URL: {article.url}\n"
            f"Content:\n{article.content}\n"
        )
    stories_block = "\n---\n".join(stories)
    return PROMPT_TEMPLATE.format(date_str=date_str, stories_block=stories_block)


def generate_briefing_script(
    articles: list[FetchedArticle],
    date_str: str,
) -> str:
    """Generate a TTS briefing script via the shared opencode server."""
    prompt = build_prompt(articles, date_str)

    instruction = (
        "Read the following prompt with instructions and article content. "
        "Follow the instructions exactly and generate the podcast briefing "
        "script. Output ONLY the script text, nothing else.\n\n"
        + prompt
    )

    session_id = create_session()
    try:
        send_prompt_async(session_id, instruction)

        if not wait_for_idle(session_id, timeout=120):
            raise RuntimeError(
                "opencode session did not complete within 120 seconds"
            )

        messages = get_messages(session_id)
        return get_last_assistant_text(messages).strip()
    finally:
        delete_session(session_id)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/test_summarizer.py -v`
Expected: All PASS

**Step 5: Run full test suite**

Run: `uv run pytest -v`
Expected: All pass

**Step 6: Lint**

Run: `uv run ruff check pipeline/summarizer.py pipeline/test_summarizer.py && uv run ruff format --check pipeline/summarizer.py pipeline/test_summarizer.py`

**Step 7: Commit**

```bash
git add pipeline/summarizer.py pipeline/test_summarizer.py
git commit --no-gpg-sign -m "refactor: summarizer uses shared opencode server instead of subprocess"
```

---

### Task 6: Add `opencode.json` permissions config

**Files:**
- Create: `opencode.json` (project root)

**Step 1: Create the config file**

Create `opencode.json` in the project root:

```json
{
  "permission": {
    "*": "allow",
    "external_directory": "allow",
    "doom_loop": "allow"
  }
}
```

This tells the shared opencode server to auto-approve all tool permissions when working in the my-podcasts directory, matching what was previously set via the `OPENCODE_PERMISSION` env var.

**Step 2: Verify the file is valid JSON**

Run: `python -c "import json; json.load(open('opencode.json'))"`
Expected: No output (valid JSON)

**Step 3: Commit**

```bash
git add opencode.json
git commit --no-gpg-sign -m "feat: add opencode.json with auto-approve permissions"
```

---

### Task 7: Update documentation

**Files:**
- Modify: `AGENTS.md`
- Modify: `.opencode/skills/operating-things-happen-digest/SKILL.md`
- Modify: `.opencode/skills/operating-things-happen-digest/REFERENCE.md`

**Step 1: Update AGENTS.md**

Replace the Things Happen Pipeline section (lines 55-69). Change:
- "launches an `opencode serve` agent on port 5555" → "creates a session on the shared `opencode-serve` daemon (port 4096)"
- Remove reference to `opencode serve` subprocess management
- Add `pipeline/opencode_client.py` to new modules list

**Step 2: Update SKILL.md**

Replace all references to:
- Port 5555 → shared opencode-serve on port 4096
- PID file `/tmp/things-happen-opencode.pid` → session ID in SQLite
- `fuser 5555/tcp` → `curl http://127.0.0.1:4096/session` to check active sessions
- `fuser -k 5555/tcp` → check/delete sessions via opencode API

**Step 3: Update REFERENCE.md**

Replace:
- "starts an `opencode serve` process on port 5555" → "creates a session on the shared opencode-serve daemon"
- Remove PID file references
- Update "Agent details" section to reflect new architecture
- Update "Common failures" section: no more port conflicts or stale PID files; new failure mode is shared server being down

**Step 4: Commit**

```bash
git add AGENTS.md .opencode/skills/operating-things-happen-digest/SKILL.md .opencode/skills/operating-things-happen-digest/REFERENCE.md
git commit --no-gpg-sign -m "docs: update Things Happen docs for shared opencode-serve"
```

---

### Task 8: Final verification

**Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: All pass

**Step 2: Run linter on all changed files**

Run: `uv run ruff check pipeline/ && uv run ruff format --check pipeline/`
Expected: Clean

**Step 3: Run type checker**

Run: `uv run mypy pipeline/`
Expected: Clean (or pre-existing issues only)

**Step 4: Verify no references to old patterns remain**

Run: `grep -r "AGENT_PORT\|AGENT_PID_FILE\|OPENCODE_BIN\|port 5555\|5555/tcp\|opencode.pid" pipeline/ AGENTS.md .opencode/skills/`
Expected: No matches
