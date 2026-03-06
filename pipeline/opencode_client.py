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
        role = msg.get("role") or msg.get("info", {}).get("role")
        if role == "assistant":
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

                props = event.get("properties", {})
                status_type = props.get("status", {}).get("type")
                if (
                    event.get("type") == "session.status"
                    and props.get("sessionID") == session_id
                    and status_type == "idle"
                ):
                    return True

    except requests.RequestException:
        pass

    # SSE stream ended (exception or natural close) — fall back to polling.
    return _poll_until_idle(session_id, deadline)


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
