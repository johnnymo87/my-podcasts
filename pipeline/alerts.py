from __future__ import annotations

import logging
import threading
from collections.abc import Callable

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


logger = logging.getLogger(__name__)

BOUNDED_ALERT_TIMEOUT_S = 15.0


def send_alert_bounded(
    text: str,
    severity: str = "info",
    *,
    timeout_s: float | None = None,
    sender: Callable[..., bool] | None = None,
) -> bool | None:
    """Send an alert without ever waiting longer than ``timeout_s``.

    ``send_alert`` has only a per-read ``requests`` timeout, which a trickling
    server defeats, and the consumer is a single thread: an alert that hangs
    would stall every feed. So the sender runs on a daemon thread and this
    function joins it with a timeout.

    Returns the sender's answer (``True``/``False``), ``False`` if the sender
    raised (the text is logged, so journald keeps it), or ``None`` if it had not
    finished in time (**delivery unknown**: it may still go out late; a
    thread stuck in a hung send lingers until the process exits). Never raises.

    ``sender`` defaults to ``send_alert`` and ``timeout_s`` to
    ``BOUNDED_ALERT_TIMEOUT_S``, both looked up at call time (so tests can patch
    them). The sender is called as ``sender(text, severity=severity)``.
    """
    if timeout_s is None:
        timeout_s = BOUNDED_ALERT_TIMEOUT_S
    send = sender if sender is not None else send_alert
    outcome: list[bool] = []

    def run() -> None:
        try:
            outcome.append(bool(send(text, severity=severity)))
        except Exception:  # noqa: BLE001 -- an alert must never disturb its caller
            logger.exception("alert sender raised; text was: %s", text)
            outcome.append(False)

    try:
        thread = threading.Thread(target=run, name="bounded-alert", daemon=True)
        thread.start()
        thread.join(timeout_s)
    except Exception:  # noqa: BLE001 -- e.g. cannot start a thread
        logger.exception("could not run the alert sender; text was: %s", text)
        return False
    if thread.is_alive():
        logger.error(
            "alert sender timed out after %.1fs; text was: %s", timeout_s, text
        )
        return None
    return outcome[0] if outcome else False
