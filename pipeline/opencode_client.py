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


def _daemon_auth_headers() -> dict[str, str]:
    """Bearer header for pigeon, when the daemon on this host runs with auth on.

    Devbox runs the daemon with auth disabled and so needs nothing here, but a
    missing header against an auth-enabled daemon is a 401 -- which silently
    returns the Telegram noise that ``declare_quiet_origin`` exists to remove.
    Resolution order matches the ``oc-pool-attach`` convention: env var first,
    then the sops secret file.
    """
    token = os.environ.get("PIGEON_DAEMON_AUTH_TOKEN", "").strip()
    if not token:
        token_file = os.environ.get(
            "PIGEON_DAEMON_AUTH_TOKEN_FILE", "/run/secrets/pigeon_daemon_auth_token"
        )
        try:
            token = Path(token_file).read_text().strip()
        except OSError:
            token = ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def declare_quiet_origin(session_id: str) -> None:
    """Tell pigeon this session is machine-driven, so it stays out of Telegram.

    Every session this module creates belongs to an unattended pipeline run. Left
    undeclared, each one posts a Stop notification and a mirror of its own launch
    prompt; and because a Telegram forum topic is created by a session's FIRST
    notification, each run also strands a topic behind it. Measured before this
    existed: 122 sessions in 6 days, one every ~15 minutes, 103 of them dying
    before opencode could even title them -- so 103 topics all named
    ``~/projects/my-podcasts``.

    ``notify_policy='none'`` suppresses Stop, Error and Retry, and also gates the
    prompt mirror and the swarm feed, so nothing reaches Telegram and no topic is
    born. The single known gap is ``POST /question-asked``, which bypasses the
    policy matrix entirely; a non-interactive pipeline should never ask.

    Scope is deliberately this function rather than the working directory: a
    human working by hand in this repo does not go through ``create_session``,
    and so is unaffected. A directory-wide rule would have silenced them too.

    Best-effort by design. Pigeon being down, rejecting the write, or being too
    old to know the route must never fail a podcast run -- the cost is noise in
    Telegram, which is recoverable, whereas a raised exception here loses an
    episode. Declared quiet also carries a ~2h TTL on pigeon's side, which is
    ample: these sessions are created, prompted and deleted within minutes.
    """
    try:
        resp = requests.post(
            f"{PIGEON_DAEMON_URL.rstrip('/')}/session-origin",
            json={
                "session_id": session_id,
                "origin": "my-podcasts-pipeline",
                "notify_policy": "none",
            },
            headers=_daemon_auth_headers(),
            timeout=3,
        )
        if not resp.ok:
            print(
                f"warning: pigeon declined to quiet session {session_id} "
                f"(HTTP {resp.status_code}); its notifications will reach Telegram",
                flush=True,
            )
    except (requests.RequestException, OSError) as exc:
        print(
            f"warning: could not reach pigeon to quiet session {session_id} "
            f"({exc}); its notifications will reach Telegram",
            flush=True,
        )


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
    # Declare BEFORE anything can notify. pigeon accepts a declaration for a
    # session it has never seen (the plugin registers it moments later), and the
    # first thing that would post to Telegram is the mirror of the prompt the
    # caller sends once this function returns -- so declaring here is what makes
    # the suppression race-free rather than merely likely.
    declare_quiet_origin(session_id)
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
