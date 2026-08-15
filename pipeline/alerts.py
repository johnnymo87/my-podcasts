from __future__ import annotations

import requests

from pipeline.pigeon import PIGEON_DAEMON_URL, daemon_auth_headers


def send_alert(text: str, severity: str = "info") -> bool:
    """Post an operational message to the Telegram General topic via pigeon.

    ``POST /alert`` is the only session-free path that reaches General: it calls
    sendMessage with ``{chat_id, text}`` and no ``message_thread_id``. Pigeon's
    swarm channel broadcast deliberately skips Telegram, so it is not an option.
    Note there is no ``parse_mode``, so ``text`` renders literally.

    Never raises. A reporting failure must never be able to disturb the job that
    produced the report, so every error path returns False and prints; the
    rendered text is logged so journald retains it when pigeon is down.
    """
    if not text.strip():
        return False

    try:
        response = requests.post(
            f"{PIGEON_DAEMON_URL.rstrip('/')}/alert",
            json={"text": text, "severity": severity},
            headers=daemon_auth_headers(),
            timeout=10,
        )
    except Exception as exc:
        print(f"[alerts] send failed ({type(exc).__name__}: {exc}); text was:\n{text}")
        return False

    if not (200 <= response.status_code < 300):
        print(
            f"[alerts] send rejected (HTTP {response.status_code}); text was:\n{text}"
        )
        return False

    return True
