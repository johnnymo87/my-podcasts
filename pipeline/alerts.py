from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Literal

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
ALERT_QUEUE_MAX = 8

AlertResult = bool | Literal["timeout", "dropped"]

# One shared worker for bounded alerts (the shape of ``tts/render.py``'s alert
# worker). A thread per call leaks one thread for every timed-out send; one
# worker plus a bounded queue caps the leak at a single thread however many
# alerts hang.
_STOP = object()  # tells the worker to exit (tests only)
_alert_lock = threading.Lock()
_alert_queue: queue.Queue = queue.Queue(maxsize=ALERT_QUEUE_MAX)
_alert_worker: threading.Thread | None = None


class _AlertJob:
    __slots__ = ("done", "result", "send", "severity", "text")

    def __init__(self, text: str, severity: str, send: Callable[..., bool]) -> None:
        self.text = text
        self.severity = severity
        self.send = send
        self.done = threading.Event()
        self.result = False


def _alert_worker_loop() -> None:
    while True:
        job = _alert_queue.get()
        if job is _STOP:
            return
        try:
            job.result = bool(job.send(job.text, severity=job.severity))
        except Exception:  # noqa: BLE001 -- an alert must never disturb its caller
            logger.exception("alert sender raised; text was: %s", job.text)
            job.result = False
        finally:
            job.done.set()


def _enqueue_alert(text: str, severity: str, send: Callable[..., bool]):
    """Queue an alert for the one worker; ``None`` if the queue is full.

    Starts the worker on first use and again if it has died -- never a second
    one alongside a live (even stuck) worker. A full queue means the worker is
    stuck behind earlier alerts: do not add a thread, drop this one.
    """
    global _alert_worker
    with _alert_lock:
        if _alert_worker is None or not _alert_worker.is_alive():
            worker = threading.Thread(
                target=_alert_worker_loop, name="bounded-alert-worker", daemon=True
            )
            worker.start()  # may raise; the global is only set on success
            _alert_worker = worker
        job = _AlertJob(text, severity, send)
        try:
            _alert_queue.put_nowait(job)
        except queue.Full:
            return None
        return job


def _stop_alert_worker(timeout: float = 5.0) -> None:
    """Drop queued alerts and stop the worker (tests; never needed in production)."""
    global _alert_worker
    with _alert_lock:
        worker, _alert_worker = _alert_worker, None
        while True:
            try:
                _alert_queue.get_nowait()
            except queue.Empty:
                break
        if worker is not None and worker.is_alive():
            _alert_queue.put_nowait(_STOP)
    if worker is not None:
        worker.join(timeout)


def send_alert_bounded(
    text: str,
    severity: str = "info",
    *,
    timeout_s: float | None = None,
    sender: Callable[..., bool] | None = None,
) -> AlertResult:
    """Send an alert without ever waiting longer than ``timeout_s``.

    ``send_alert`` has only a per-read ``requests`` timeout, which a trickling
    server defeats, and the consumer is a single thread: an alert that hangs
    would stall every feed. So the send runs on ONE shared daemon worker fed by
    a bounded queue, and this function waits on the job for at most ``timeout_s``.

    Returns:
      * ``True`` / ``False`` -- the sender's answer; ``False`` also when it
        raised (the text is logged, so journald keeps it);
      * ``"timeout"`` -- not finished in time (**delivery unknown**: it may
        still go out late, and a hung send holds the worker, so later alerts
        queue behind it);
      * ``"dropped"`` -- the queue was full (worker stuck behind earlier
        alerts); the text is logged at ERROR and never sent.

    Never raises. ``sender`` defaults to ``send_alert`` and ``timeout_s`` to
    ``BOUNDED_ALERT_TIMEOUT_S``, both looked up at call time (so tests can patch
    them). The sender is called as ``sender(text, severity=severity)``.
    """
    if timeout_s is None:
        timeout_s = BOUNDED_ALERT_TIMEOUT_S
    send = sender if sender is not None else send_alert
    try:
        job = _enqueue_alert(text, severity, send)
    except Exception:  # noqa: BLE001 -- e.g. cannot start a thread
        logger.exception("could not run the alert sender; text was: %s", text)
        return False
    if job is None:
        logger.error("alert queue full, alert dropped; text was: %s", text)
        return "dropped"
    if not job.done.wait(timeout_s):
        logger.error(
            "alert sender timed out after %.1fs; text was: %s", timeout_s, text
        )
        return "timeout"
    return job.result
