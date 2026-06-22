from __future__ import annotations

import os
import time
from pathlib import Path

import requests


OPENCODE_URL = os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096")
# Pigeon daemon discovery endpoint. In a K-serve pool each opencode serve runs
# its own agent loop, so a session's prompt/poll must reach the serve that OWNS
# it. After creating a session we ask pigeon GET /route?session_id which serve
# that is and cache it. Matches the opencode-launch / opencode-send convention.
PIGEON_DAEMON_URL = os.environ.get("PIGEON_DAEMON_URL", "http://127.0.0.1:4731")
PROJECT_DIR = str(Path(__file__).resolve().parent.parent)

# session_id -> owning serve base URL, resolved once at create time via /route.
_session_serve: dict[str, str] = {}


def _fallback_base() -> str:
    return OPENCODE_URL.rstrip("/")


def resolve_serve_url(session_id: str) -> str:
    """Resolve the serve that owns ``session_id`` via pigeon's ``GET /route``.

    Returns the owning serve's base URL (``.apiBase``). Degrades to
    ``OPENCODE_URL`` (serve-0) whenever pigeon is unreachable, returns non-200,
    or omits ``apiBase`` — so any routing hiccup is never worse than the
    pre-pool single-serve behavior.
    """
    try:
        resp = requests.get(
            f"{PIGEON_DAEMON_URL.rstrip('/')}/route",
            params={"session_id": session_id},
            timeout=3,
        )
        if resp.ok:
            api = (resp.json() or {}).get("apiBase")
            if api:
                return str(api).rstrip("/")
    except (requests.RequestException, ValueError):
        pass
    return _fallback_base()


def _serve_for(session_id: str) -> str:
    """Owning-serve base URL for a session (cached), else the serve-0 fallback."""
    return _session_serve.get(session_id, _fallback_base())


def _headers(directory: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if directory:
        headers["x-opencode-directory"] = directory
    return headers


def create_session(directory: str | None = None) -> str:
    """Create a new opencode session. Returns the session ID."""
    dir_value = directory or PROJECT_DIR
    resp = requests.post(
        f"{_fallback_base()}/session",
        headers=_headers(dir_value),
        timeout=10,
    )
    resp.raise_for_status()
    session_id = resp.json()["id"]
    # Pin the session to its owning serve so every subsequent prompt/poll/delete
    # call below reaches the serve that runs its agent loop (pool-aware routing).
    _session_serve[session_id] = resolve_serve_url(session_id)
    return session_id


def send_prompt_async(session_id: str, text: str) -> None:
    """Send a prompt to a session (fire-and-forget)."""
    resp = requests.post(
        f"{_serve_for(session_id)}/session/{session_id}/prompt_async",
        json={"parts": [{"type": "text", "text": text}]},
        timeout=10,
    )
    resp.raise_for_status()


def is_session_active(session_id: str) -> bool:
    """Check if a session exists and is accessible."""
    try:
        resp = requests.get(
            f"{_serve_for(session_id)}/session/{session_id}",
            timeout=5,
        )
        return resp.ok
    except Exception:
        return False


def delete_session(session_id: str) -> None:
    """Delete a session. Ignores 404 (already gone)."""
    resp = requests.delete(
        f"{_serve_for(session_id)}/session/{session_id}",
        timeout=10,
    )
    if resp.status_code != 404:
        resp.raise_for_status()


def get_messages(session_id: str) -> list[dict]:
    """Get all messages for a session."""
    resp = requests.get(
        f"{_serve_for(session_id)}/session/{session_id}/message",
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
    """Wait until the session's last assistant message has finished generating.

    Polls ``/session/{id}/message`` and returns True when the most recent
    assistant message contains a ``step-finish`` part — opencode's reliable
    completion marker. Returns False on timeout.

    Why polling, not SSE: opencode-serve 1.14.x's ``/event`` stream emits
    ``server.connected`` and ``server.heartbeat`` only — it does not emit
    ``session.status: idle`` events. ``/session/status`` returns ``{}`` for
    completed sessions, so neither stream-based detection works. The
    assistant message itself is the source of truth.

    Tolerates transient connection errors during polling.
    """
    deadline = time.time() + timeout
    poll_interval = 5.0

    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{_serve_for(session_id)}/session/{session_id}/message",
                timeout=10,
            )
            if resp.ok and _is_assistant_done(resp.json()):
                return True
        except requests.RequestException:
            pass
        time.sleep(poll_interval)
    return False


def _is_assistant_done(messages: list[dict]) -> bool:
    """Return True if the most recent assistant message has finished generating.

    The marker is a ``step-finish`` part on the latest message whose role is
    ``assistant``. User messages are ignored even if they happen to carry a
    ``step-finish`` part.
    """
    for msg in reversed(messages):
        role = msg.get("role") or msg.get("info", {}).get("role")
        if role != "assistant":
            continue
        parts = msg.get("parts", [])
        return any(p.get("type") == "step-finish" for p in parts)
    return False
