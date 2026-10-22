"""Run at most one due podcast-report job: (reconcile | child -> publish) ->
bookkeeping.

An episode already committed to the ``episodes`` table is reconciled (feeds
re-sent, job completed) *without* running the child.

The consumer is the only executor. ``run_one_podcast_episode`` never raises for
a job failure: whatever the child, the publish, or the stats do, the outcome is
a row transition (``completed``, or ``fail_podcast_episode``), one summary line
in the journal, and, when the retry budget is spent, one alert attempt.
``KeyboardInterrupt``/``SystemExit`` still propagate; the row is then left
``in_progress`` and ``recover_interrupted_podcast_episodes`` re-queues it on the
next consumer start.

Error strings stored on the row and sent in alerts go through ``scrub_detail``
so a signed enclosure URL can never reach ``last_error``, the journal, or
Telegram.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline import podcast_child, podcast_publish
from pipeline.alerts import send_alert, send_alert_bounded
from pipeline.podcast_child import scrub_detail
from pipeline.podcast_jobs import PODCAST_WORK_ROOT, item_from_row, work_dir_for
from pipeline.podcast_publish import validate_identity
from pipeline.podcast_report import AUDIO_NAME, EPISODE_AUDIO_NAME
from pipeline.podcast_sources import get_podcast_source
from pipeline.podcast_transcribe import (
    MANIFEST_NAME,
    duration_allowance,
    duration_bounds,
)


logger = logging.getLogger(__name__)

# Beyond this fraction of the allowed decoded-vs-RSS gap (for the side the gap
# is on) we are close enough to a refusal to say so.
HEADROOM_WARN_FRACTION = 0.75
TERMINAL_HTTP_CODE = 400  # Gemini reports an invalid request/key as 400

# Account-wide Gemini conditions: 402 (prepayment credits depleted) and 429
# (rate/quota, after the transcriber's own retries). They are not the episode's
# fault, so they are never charged to it: the row is deferred and podcast work
# pauses in-process for QUOTA_PAUSE.
ACCOUNT_QUOTA_CODES = frozenset({402, 429})
QUOTA_PAUSE = timedelta(hours=1)
QUOTA_REALERT = timedelta(hours=6)
QUOTA_ALERT_DETAIL_CHARS = 200

# In-process state; a consumer restart clears both (at most one extra probe and
# one extra alert, which is acceptable).
_quota_pause_until: datetime | None = None
_quota_alerted_at: datetime | None = None


def _num(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _sum_tokens(pieces: list[Any], key: str) -> int | None:
    """Total of ``key`` over ``pieces``; None if any piece's count is unknown."""
    total = 0
    for piece in pieces:
        value = piece.get(key) if isinstance(piece, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        total += value
    return total


def _read_manifest(work_dir: Path) -> dict[str, Any]:
    """The transcript manifest, or ``{}`` if it is missing or unreadable.
    Stats never fail a job that has already been published."""
    try:
        manifest = json.loads((work_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _blank_stats(child_elapsed_s: float, mode: str) -> dict:
    return {
        "decoded_duration_s": None,
        "rss_duration_s": None,
        "decoded_minus_rss_s": None,
        "allowed_s": None,
        "allowed_short_s": None,
        "allowed_long_s": None,
        "pieces": None,
        "input_tokens": None,
        "output_tokens": None,
        "asr_skipped_s": 0,
        "child_elapsed_s": round(child_elapsed_s, 3),
        "publish": mode,
    }


def collect_stats(work_dir: Path, *, child_elapsed_s: float, mode: str) -> dict:
    """Run statistics for ``stats_json``. Unknown values are ``None``."""
    manifest = _read_manifest(work_dir)
    decoded = _num(manifest.get("decoded_duration_s"))
    rss = _num(manifest.get("rss_duration_s"))
    pieces = manifest.get("pieces")
    if not isinstance(pieces, list):
        pieces = None

    stats = _blank_stats(child_elapsed_s, mode)
    diff = allowed = allowed_short = allowed_long = None
    if decoded is not None and rss is not None:
        diff = round(decoded - rss, 3)
        # The very functions podcast_transcribe._check_duration uses, so the
        # stats cannot drift from the refusal.
        allowed = duration_allowance(decoded, rss)
        allowed_short, allowed_long = duration_bounds(rss)
    stats.update(
        decoded_duration_s=decoded,
        rss_duration_s=rss,
        decoded_minus_rss_s=diff,
        allowed_s=allowed,
        allowed_short_s=allowed_short,
        allowed_long_s=allowed_long,
        pieces=len(pieces) if pieces is not None else None,
        input_tokens=_sum_tokens(pieces, "input_tokens") if pieces else None,
        output_tokens=_sum_tokens(pieces, "output_tokens") if pieces else None,
        # Edge-ad seconds the transcriber refused and the transcript marks as
        # untranscribed; the manifest carries the key only when it is > 0.
        asr_skipped_s=_num(manifest.get("skipped_s")) or 0,
    )
    return stats


def _headroom_used(stats: dict) -> bool:
    """True when the decoded-vs-RSS gap is past the warning fraction of the
    allowance for its side (``allowed_s``). Both directions are checked: a short
    decode is close to its (tighter) refusal in
    ``podcast_transcribe._check_duration`` too."""
    diff, allowed = stats["decoded_minus_rss_s"], stats["allowed_s"]
    if diff is None or allowed is None:
        return False
    return abs(diff) > HEADROOM_WARN_FRACTION * allowed


def run_one_podcast_episode(
    store,
    r2_client,
    *,
    now: datetime | None = None,
    run_child=podcast_child.run_episode_in_child,
    publish=podcast_publish.publish_podcast_episode,
    reconcile=podcast_publish.reconcile_committed,
    alert: Callable[..., bool] = send_alert,
    work_root: Path = PODCAST_WORK_ROOT,
) -> str | None:
    """Run at most one due podcast job. Returns ``"<source>/<guid>: <outcome>"``
    or None when nothing was run.

    Outcomes: ``published`` / ``reconciled`` (completed), ``failed (n)`` (back
    to pending), ``errored (n)`` (retry budget spent, or a terminal failure),
    ``failure not recorded`` (the database refused the failure itself; one
    error alert is attempted and the row stays ``in_progress`` until the next
    consumer start re-queues it), ``deferred (account quota)`` (the child hit
    a 402/429; back to pending in one hour with no failure charged, and no
    podcast work runs until then).

    While an account-quota pause is active this returns None before selecting
    anything. That also delays a row that would only reconcile (no child, no
    Gemini call); rare, and at most one hour.
    """
    global _quota_pause_until, _quota_alerted_at
    injected_now = now

    def clock() -> datetime:
        # A job can take 30 minutes in the child: failure backoff is measured
        # from when the failure happens, not from when the row was selected.
        return injected_now or datetime.now(UTC)

    if _quota_pause_until is not None and clock() < _quota_pause_until:
        return None

    row = store.next_due_podcast_episode(clock())
    if row is None:
        return None
    source_key, guid = row["source"], row["guid"]
    if not store.claim_podcast_episode(source_key, guid):
        return None  # someone else changed it between the select and the claim

    label = f"{source_key}/{guid}"
    title = row["display_title"]

    def fail(error: str, *, terminal: bool = False) -> str:
        error = scrub_detail(error)
        try:
            retry = store.fail_podcast_episode(
                source_key, guid, error, now=clock(), terminal=terminal
            )
        except Exception:  # noqa: BLE001 -- see the docstring
            logger.exception("%s: could not record failure %r", label, error)
            sent = send_alert_bounded(
                f"could not record failure for {label}; row stuck in_progress "
                f"until consumer restart: {error}",
                "error",
                sender=alert,
            )
            print(f"podcast-report {label}: failure not recorded, alert result={sent}")
            return f"{label}: failure not recorded"
        verdict = "errored" if retry.status == "errored" else "failed"
        verdict += f" ({retry.failure_count})"
        logger.warning("%s: %s: %s", label, verdict, error)
        print(f"podcast-report {label}: {verdict}: {error}")
        if retry.exhausted:
            sent = send_alert_bounded(
                f"Podcast report {title} gave up after {retry.failure_count} "
                f"failure(s). Last error: {error}",
                "error",
                sender=alert,
            )
            print(f"podcast-report {label}: give-up alert result={sent}")
        return f"{label}: {verdict}"

    try:
        source = get_podcast_source(source_key)
        item = item_from_row(row)
        work_dir = work_dir_for(source_key, guid, work_root)
    except Exception as exc:  # noqa: BLE001 -- a row we cannot even interpret
        return fail(f"setup: {type(exc).__name__}: {exc}")

    # Identity first: a drifted row must not cost a child run (or be reconciled
    # under a name it no longer carries).
    try:
        validate_identity(row)
    except Exception as exc:  # noqa: BLE001
        return fail(f"identity: {type(exc).__name__}: {exc}")

    # Reconcile before the child: if the episode is already committed (an
    # earlier attempt published it but could not record completion, or the feed
    # upload failed after the insert) there is nothing to generate. Re-send the
    # feeds and complete; never spend a transcription, a writer run and a render
    # on an episode that is already live.
    try:
        reconciled = reconcile(row, store=store, r2_client=r2_client)
    except Exception as exc:  # noqa: BLE001
        return fail(f"publish: {type(exc).__name__}: {exc}")

    if reconciled:
        mode, child_elapsed_s = "reconciled", 0.0
    else:
        try:
            child = run_child(source, item, work_dir)
        except Exception as exc:  # noqa: BLE001 -- never raise for a job failure
            return fail(f"child: {type(exc).__name__}: {exc}")

        if not child.ok and (
            child.stage != "claim" and child.code in ACCOUNT_QUOTA_CODES
        ):
            error = scrub_detail(f"{child.stage}: {child.reason}: {child.detail}")
            now_q = clock()
            _quota_pause_until = now_q + QUOTA_PAUSE
            try:
                store.defer_podcast_episode(
                    source_key, guid, "deferred: " + error, until=_quota_pause_until
                )
            except Exception:  # noqa: BLE001 -- fall back to a counted failure
                logger.exception("%s: could not defer; counting a failure", label)
                return fail(error)
            if _quota_alerted_at is None or now_q - _quota_alerted_at >= QUOTA_REALERT:
                detail = scrub_detail(str(child.detail))[:QUOTA_ALERT_DETAIL_CHARS]
                sent = send_alert_bounded(
                    f"Gemini account quota/billing error (HTTP {child.code}) on "
                    f"podcast report {title}: {detail}. Podcast reports paused "
                    f"until {_quota_pause_until:%H:%M}Z; episodes are not charged "
                    "a failure and will retry. The same Gemini account also "
                    "feeds the Rundown/FP editors, Semafor routing and Gemini "
                    "TTS (falls back to OpenAI). Check AI Studio billing.",
                    "error",
                    sender=alert,
                )
                _quota_alerted_at = now_q
                print(f"podcast-report {label}: quota alert result={sent}")
            logger.warning("%s: deferred (account quota): %s", label, error)
            print(
                f"podcast-report {label}: deferred (account quota) until "
                f"{_quota_pause_until.isoformat()}"
            )
            return f"{label}: deferred (account quota)"

        # Not a quota error: the streak (if any) is over, so the next one
        # alerts immediately.
        _quota_alerted_at = None

        if not child.ok:
            terminal = child.stage == "claim" or child.code == TERMINAL_HTTP_CODE
            return fail(
                f"{child.stage}: {child.reason}: {child.detail}", terminal=terminal
            )

        try:
            mode = publish(row, work_dir, store=store, r2_client=r2_client)
        except Exception as exc:  # noqa: BLE001
            return fail(f"publish: {type(exc).__name__}: {exc}")
        child_elapsed_s = child.elapsed_s

    # Published. From here on nothing may undo that. Stats are best-effort
    # and get their own guard, so a bug there cannot turn a live episode into
    # a failed job.
    try:
        stats = collect_stats(work_dir, child_elapsed_s=child_elapsed_s, mode=mode)
    except Exception:  # noqa: BLE001
        logger.exception("%s: stats collection failed; completing without", label)
        stats = _blank_stats(child_elapsed_s, mode)

    # If recording completion itself fails, the row goes back to pending so the
    # next attempt reconciles (finds the episode committed, re-sends feeds only,
    # no TTS). Rare corner: if that was already the 4th failure the row becomes
    # 'errored' although the episode is live. The give-up alert then names the
    # complete: error, and `podcast-report reset` is safe (it reconciles).
    try:
        store.complete_podcast_episode(source_key, guid, stats=stats)
    except Exception as exc:  # noqa: BLE001
        return fail(f"complete: {type(exc).__name__}: {exc}")

    for name in (AUDIO_NAME, EPISODE_AUDIO_NAME):
        try:
            (work_dir / name).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("%s: could not delete %s: %r", label, name, exc)

    if _headroom_used(stats):
        diff = stats["decoded_minus_rss_s"]
        side = "longer" if diff >= 0 else "shorter"
        sent = send_alert_bounded(
            f"Podcast report {title}: decoded audio is "
            f"{abs(diff):.0f}s {side} than the RSS duration ({diff:+.0f}s), "
            f"{abs(diff) / stats['allowed_s']:.0%} of the "
            f"{stats['allowed_s']:.0f}s the duration check allows on that "
            "side. Refusals start at 100%.",
            "info",
            sender=alert,
        )
        print(f"podcast-report {label}: headroom alert result={sent}")

    summary = (
        f"podcast-report {label}: {mode} in {stats['child_elapsed_s']}s child time, "
        f"{stats['pieces']} pieces, decoded-rss {stats['decoded_minus_rss_s']}s "
        f"of {stats['allowed_s']}s allowed"
    )
    logger.info(summary)
    print(summary)
    return f"{label}: {mode}"
