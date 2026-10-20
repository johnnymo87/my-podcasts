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
from pipeline.alerts import send_alert, send_alert_bounded
from pipeline.podcast_child import scrub_detail
from pipeline.podcast_feed import eligible_items, parse_podcast_feed
from pipeline.podcast_jobs import admit_item
from pipeline.podcast_sources import PODCAST_SOURCES, PodcastSource


logger = logging.getLogger(__name__)

# (source, guid) r2_key collisions already alerted in this process. A collision
# is permanent (the identity is frozen from the feed item), so without this the
# same alert would repeat every 6-hour tick. Cleared by a consumer restart,
# which re-alerts once: acceptable, and better than silence after a restart.
_ALERTED_COLLISIONS: set[tuple[str, str]] = set()


def _tick(
    store,
    source: PodcastSource,
    *,
    now: datetime,
    fetch: Callable[[str], str],
    collisions: list[tuple[str, str]],
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
            collisions.append((source.key, item.guid))
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

    Total: a source's failure never stops another, and neither does a failure
    of the health bookkeeping itself (a locked database, say). Never raises
    for a source; only ``KeyboardInterrupt``/``SystemExit`` propagate.
    """
    results: dict[str, int] = {}
    collisions: list[tuple[str, str]] = []
    for source in sources:
        admitted = 0
        error: str | None = None
        try:
            admitted = _tick(store, source, now=now, fetch=fetch, collisions=collisions)
        except Exception as exc:  # noqa: BLE001 -- a failed tick, not a crash
            error = f"{type(exc).__name__}: {scrub_detail(exc)}"
            logger.warning("podcast discovery for %s failed: %s", source.key, error)
        results[source.key] = admitted

        try:
            count, should_alert = store.record_podcast_discovery(
                source.key, ok=error is None, error=error, now=now
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "podcast discovery for %s: could not record the tick's health",
                source.key,
            )
            continue
        if error is not None and should_alert:
            sent = send_alert_bounded(
                f"Podcast discovery for {source.name} failed {count} times "
                f"in a row: {error}",
                "error",
                sender=alert,
            )
            logger.error(
                "podcast discovery for %s: health alert result=%s", source.key, sent
            )

    new = [c for c in dict.fromkeys(collisions) if c not in _ALERTED_COLLISIONS]
    for c in dict.fromkeys(collisions):
        if c in _ALERTED_COLLISIONS:
            logger.info("podcast discovery: %s/%s still collides (already alerted)", *c)
    if new:
        _ALERTED_COLLISIONS.update(new)
        sent = send_alert_bounded(
            "Podcast discovery: r2_key collision, episode(s) not admitted: "
            + ", ".join(f"{s}/{g}" for s, g in new),
            "error",
            sender=alert,
        )
        logger.error("podcast discovery: collision alert result=%s", sent)
    return results
