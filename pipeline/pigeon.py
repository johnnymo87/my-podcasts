from __future__ import annotations

import os
from pathlib import Path


# Pigeon daemon discovery endpoint. In a K-serve pool each opencode serve runs
# its own agent loop, so a session's prompt/poll must reach the serve that OWNS
# it. After creating a session we ask pigeon GET /route?session_id which serve
# that is and cache it. Matches the opencode-launch / opencode-send convention.
PIGEON_DAEMON_URL = os.environ.get("PIGEON_DAEMON_URL", "http://127.0.0.1:4731")


def daemon_auth_headers() -> dict[str, str]:
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
