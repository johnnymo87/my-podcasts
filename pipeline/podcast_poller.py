"""Discover new podcast episodes and admit them as pending report jobs.

Runs on its own 6-hour cadence inside the consumer. A source whose feed cannot
be fetched, cannot be parsed, or parses to zero items has had a *failed tick*;
the fourth failed tick in a row sends one alert (``record_podcast_discovery``
owns the counting and the at-most-once arming). A feed that parses to zero
items is a failure because a healthy podcast feed is never empty: it means the
publisher changed shape or served an error page with a 200.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime

from pipeline import podcast_report
from pipeline.alerts import send_alert
from pipeline.podcast_child import scrub_detail
from pipeline.podcast_feed import eligible_items, parse_podcast_feed
from pipeline.podcast_jobs import admit_item
from pipeline.podcast_sources import PODCAST_SOURCES, PodcastSource


logger = logging.getLogger(__name__)


def _safe_alert(alert: Callable[..., bool], text: str, severity: str) -> bool:
    """Send an alert; an alert that cannot be sent must not abort discovery."""
    try:
        return bool(alert(text, severity=severity))
    except Exception:  # noqa: BLE001
        logger.exception("podcast discovery: alert sender raised; text was: %s", text)
        return False


def _tick(
    store,
    source: PodcastSource,
    *,
    now: datetime,
    fetch: Callable[[str], str],
    collisions: list[str],
) -> int:
    """One fetch+parse+admit pass for ``source``; returns the admitted count.

    Raises on a fetch/parse failure or a zero-item feed (a failed tick). An
    ``r2_key`` collision on one item is recorded in ``collisions`` and skipped.
    """
    items = parse_podcast_feed(fetch(source.rss_url))
    if not items:
        raise ValueError("feed parsed to zero items")
    admitted = 0
    for item in eligible_items(items, since=source.since, now=now):
        try:
            if admit_item(store, source, item, via="poll", now=now):
                admitted += 1
                logger.info(
                    "podcast discovery: admitted %s/%s (%s)",
                    source.key,
                    item.guid,
                    item.title,
                )
        except sqlite3.IntegrityError as exc:
            logger.error(
                "podcast discovery: %s/%s collides with an existing r2_key: %s",
                source.key,
                item.guid,
                exc,
            )
            collisions.append(f"{source.key}/{item.guid}")
    return admitted


def discover(
    store,
    *,
    now: datetime,
    fetch: Callable[[str], str] = podcast_report.fetch_feed_xml,
    alert: Callable[..., bool] = send_alert,
    sources: Iterable[PodcastSource] = PODCAST_SOURCES,
) -> dict[str, int]:
    """Per source: fetch + parse; admit ``eligible_items(since=source.since,
    now=now)`` via ``admit_item(via="poll")``. Returns ``{source.key:
    admitted_count}``.

    Each source is isolated: one failing never stops another. Never raises for
    a source failure.
    """
    results: dict[str, int] = {}
    collisions: list[str] = []
    for source in sources:
        admitted = 0
        try:
            admitted = _tick(store, source, now=now, fetch=fetch, collisions=collisions)
        except Exception as exc:  # noqa: BLE001 -- a failed tick, not a crash
            error = f"{type(exc).__name__}: {scrub_detail(exc)}"
            logger.warning("podcast discovery for %s failed: %s", source.key, error)
            count, should_alert = store.record_podcast_discovery(
                source.key, ok=False, error=error, now=now
            )
            if should_alert:
                sent = _safe_alert(
                    alert,
                    f"Podcast discovery for {source.name} failed {count} times "
                    f"in a row: {error}",
                    "error",
                )
                logger.error(
                    "podcast discovery for %s: health alert sent=%s", source.key, sent
                )
        else:
            store.record_podcast_discovery(source.key, ok=True, error=None, now=now)
        results[source.key] = admitted

    if collisions:
        sent = _safe_alert(
            alert,
            "Podcast discovery: r2_key collision, episode(s) not admitted: "
            + ", ".join(collisions),
            "error",
        )
        logger.error("podcast discovery: collision alert sent=%s", sent)
    return results
